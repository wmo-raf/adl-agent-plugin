"""
``GET /api/agent/v1/sync`` -- one call that hands a device its whole world.

The promise being tested is the one the agent depends on every cycle: after a
single request it knows which folders to scan, how to scan them, how far back
to look, and whether any of that has changed since last time. So the tests ask
for the response an agent would get and read it the way an agent would, rather
than reaching for the models the view happens to read.
"""

from datetime import timedelta
from pathlib import Path

from adl import __version__ as adl_version
from django.db import connections
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone as dj_timezone

from adl_agent_plugin.health import DEFAULT_STATION_STALE_AFTER_MINUTES
from adl_agent_plugin.models import (
    AgentFileStatus,
    AgentListingStrategy,
    AgentStationLink,
)
from adl_agent_plugin.version import VERSION as PLUGIN_VERSION

from .helpers import (
    SYNC_URL,
    bearer,
    create_connection,
    create_device,
    create_station,
    create_station_link,
    paired_device,
    stage_file,
    wire_datetime,
)


class SyncTestCase(TestCase):
    def setUp(self):
        self.device, self.token = paired_device(name="Nairobi vendor server")

    def sync(self, token=None):
        return self.client.get(SYNC_URL, **bearer(token or self.token))


class SyncAuthTests(SyncTestCase):
    def test_sync_needs_a_token(self):
        response = self.client.get(SYNC_URL)

        self.assertEqual(response.status_code, 401)

    def test_revoked_device_is_locked_out(self):
        self.device.revoke()

        self.assertEqual(self.sync().status_code, 401)

    def test_sync_counts_as_being_seen(self):
        before = dj_timezone.now()

        self.sync()

        self.device.refresh_from_db()
        self.assertGreaterEqual(self.device.last_seen_at, before)


class SyncScopeTests(SyncTestCase):
    def test_a_device_is_told_about_its_own_connections_only(self):
        mine = create_connection(self.device, name="Vendor A")
        someone_else = create_connection(create_device(), name="Vendor B")
        create_station_link(mine)
        create_station_link(someone_else)

        body = self.sync().json()

        self.assertEqual([c["name"] for c in body["connections"]], ["Vendor A"])

    def test_one_device_can_serve_several_connections(self):
        create_connection(self.device, name="Vendor A")
        create_connection(self.device, name="Vendor B")

        body = self.sync().json()

        self.assertEqual(
            sorted(c["name"] for c in body["connections"]), ["Vendor A", "Vendor B"]
        )

    def test_a_device_with_nothing_linked_yet_gets_an_empty_world(self):
        body = self.sync().json()

        self.assertEqual(body["connections"], [])
        self.assertEqual(body["device"]["name"], "Nairobi vendor server")

    def test_a_station_link_disabled_centrally_is_shown_not_hidden(self):
        # The technician standing at the machine should be able to see that a
        # station is switched off in ADL, rather than watch it vanish.
        connection = create_connection(self.device)
        create_station_link(connection, enabled=False)

        link = self.sync().json()["connections"][0]["station_links"][0]

        self.assertFalse(link["admin"]["enabled"])


class SyncStationLinkTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.connection = create_connection(self.device, name="Vendor A")
        self.station = create_station(self.connection.network, name="Garissa")
        self.start_date = dj_timezone.now() - timedelta(days=3)
        self.link = create_station_link(
            self.connection,
            self.station,
            local_folder_path="D:\\aws\\garissa",
            file_pattern="GAR_*.dat",
            dir_structured_by_date=True,
            date_granularity="day",
            stability_window_seconds=90,
            start_date=self.start_date,
        )

    def link_payload(self):
        return self.sync().json()["connections"][0]["station_links"][0]

    def test_the_app_editable_tier_is_exactly_what_the_app_may_write(self):
        # The keys of "config" are the contract the PATCH endpoint enforces;
        # one list, read two ways, so the two can never drift apart.
        config = self.link_payload()["config"]

        self.assertEqual(
            set(config), set(AgentStationLink.APP_EDITABLE_FIELDS)
        )

    def test_the_app_editable_tier_carries_what_is_stored(self):
        config = self.link_payload()["config"]

        self.assertEqual(config["local_folder_path"], "D:\\aws\\garissa")
        self.assertEqual(config["file_pattern"], "GAR_*.dat")
        self.assertTrue(config["dir_structured_by_date"])
        self.assertEqual(config["date_granularity"], "day")
        self.assertEqual(config["stability_window_seconds"], 90)
        self.assertEqual(
            config["listing_strategy"], AgentListingStrategy.ENUMERATE
        )

    def test_the_admin_tier_travels_alongside_it(self):
        admin = self.link_payload()["admin"]

        self.assertTrue(admin["enabled"])
        self.assertEqual(admin["station"]["name"], "Garissa")
        self.assertEqual(admin["station"]["id"], self.station.pk)
        self.assertEqual(admin["station"]["wigos_id"], self.station.wigos_id)
        self.assertEqual(admin["timezone"], "Africa/Nairobi")
        self.assertEqual(admin["start_date"], wire_datetime(self.start_date))

    def test_the_admin_tier_and_the_app_tier_never_overlap(self):
        payload = self.link_payload()

        self.assertEqual(set(payload["config"]) & set(payload["admin"]), set())

    def test_each_link_carries_the_watermark_to_manifest_behind(self):
        self.assertEqual(
            self.link_payload()["watermark"], wire_datetime(self.start_date)
        )

    def test_a_link_with_no_collection_start_date_has_no_watermark(self):
        self.link.start_date = None
        self.link.save()

        self.assertIsNone(self.link_payload()["watermark"])


class SyncDeviceTierTests(SyncTestCase):
    def test_the_check_interval_is_per_device(self):
        self.device.check_interval_minutes = 12
        self.device.save()

        body = self.sync().json()

        self.assertEqual(body["device"]["check_interval_minutes"], 12)

    def test_the_dated_folder_window_is_per_device(self):
        self.device.dated_folder_window_hours = 240
        self.device.save()

        body = self.sync().json()

        self.assertEqual(body["device"]["dated_folder_window_hours"], 240)

    def test_the_dated_folder_window_defaults_to_two_days(self):
        # What the agent assumed before the field existed, so an instance
        # that migrates and changes nothing keeps the behaviour it had.
        self.assertEqual(self.sync().json()["device"]["dated_folder_window_hours"], 48)

    def test_a_dated_folder_window_of_nothing_survives_the_wire(self):
        # Zero is an administrator asking for the current folder alone, which
        # is a real choice for a machine on a link that cannot afford more.
        # It has to arrive as 0 rather than be clamped or dropped: the agent
        # reads a missing field as "use the default", which is the opposite
        # instruction.
        self.device.dated_folder_window_hours = 0
        self.device.save()

        self.assertEqual(self.sync().json()["device"]["dated_folder_window_hours"], 0)

    def test_the_reconciliation_interval_defaults_to_daily(self):
        # What every install in the field already does, so a deployment that
        # upgrades and configures nothing keeps the cadence it had.
        self.assertEqual(
            self.sync().json()["device"]["reconciliation_interval_hours"], 24
        )

    @override_settings(ADL_AGENT_RECONCILIATION_INTERVAL_HOURS=168)
    def test_a_deployment_can_slow_the_reconciliation_down(self):
        # A country whose link cannot carry a full folder's manifest nightly
        # is the reason this is settable at all.
        self.assertEqual(
            self.sync().json()["device"]["reconciliation_interval_hours"], 168
        )

    @override_settings(ADL_AGENT_RECONCILIATION_INTERVAL_HOURS=0)
    def test_a_reconciliation_interval_of_nothing_switches_sweeps_off(self):
        # Zero is an administrator switching reconciliation off, and has to
        # arrive as 0 rather than be clamped up to the default or dropped:
        # the agent reads a missing field as "reconcile daily", which is the
        # opposite instruction.
        self.assertEqual(
            self.sync().json()["device"]["reconciliation_interval_hours"], 0
        )

    @override_settings(ADL_AGENT_RECONCILIATION_INTERVAL_HOURS=-1)
    def test_a_negative_reconciliation_interval_also_means_off(self):
        # The contract reads zero and below alike, so an administrator who
        # typed -1 meaning "never" must not be quietly given daily sweeps.
        # It arrives as the number the contract names.
        self.assertEqual(
            self.sync().json()["device"]["reconciliation_interval_hours"], 0
        )

    @override_settings(ADL_AGENT_RECONCILIATION_INTERVAL_HOURS="nightly")
    def test_a_mistyped_reconciliation_interval_falls_back_to_daily(self):
        # Unreadable is not the same as zero: a deployment that fat-fingers
        # the setting keeps reconciling rather than silently losing the sweep
        # that catches a backfilled file.
        self.assertEqual(
            self.sync().json()["device"]["reconciliation_interval_hours"], 24
        )


class SyncConfigVersionTests(SyncTestCase):
    def test_a_settled_configuration_keeps_its_version(self):
        first = self.sync().json()["config_version"]
        second = self.sync().json()["config_version"]

        self.assertEqual(first, second)

    def test_changing_the_dated_folder_window_moves_the_version(self):
        # A device-tier setting an administrator can change and no machine
        # ever sees is the failure mode this assertion exists for: the agent
        # re-reads its configuration when the version moves, and on nothing
        # else.
        before = self.sync().json()["config_version"]

        self.device.dated_folder_window_hours = 12
        self.device.save(update_fields=["dated_folder_window_hours"])

        self.assertGreater(self.sync().json()["config_version"], before)

    def test_creating_a_connection_moves_the_version(self):
        before = self.sync().json()["config_version"]

        create_connection(self.device)

        self.assertGreater(self.sync().json()["config_version"], before)

    def test_editing_a_station_link_in_the_admin_moves_the_version(self):
        link = create_station_link(create_connection(self.device))
        before = self.sync().json()["config_version"]

        link.local_folder_path = "E:\\moved"
        link.save()

        self.assertGreater(self.sync().json()["config_version"], before)

    def test_deleting_a_station_link_moves_the_version(self):
        link = create_station_link(create_connection(self.device))
        before = self.sync().json()["config_version"]

        link.delete()

        self.assertGreater(self.sync().json()["config_version"], before)

    def test_deleting_the_station_behind_a_link_moves_the_version(self):
        # The link goes with the station, by cascade rather than by anyone
        # deleting it -- and the agent still has to be told.
        link = create_station_link(create_connection(self.device))
        before = self.sync().json()["config_version"]

        link.station.delete()

        self.assertGreater(self.sync().json()["config_version"], before)

    def test_deleting_a_connections_network_moves_the_version(self):
        connection = create_connection(self.device)
        create_station_link(connection)
        before = self.sync().json()["config_version"]

        connection.network.delete()

        self.assertGreater(self.sync().json()["config_version"], before)

    def test_changing_the_check_interval_moves_the_version(self):
        before = self.sync().json()["config_version"]

        self.device.check_interval_minutes = 20
        self.device.save()

        self.assertGreater(self.sync().json()["config_version"], before)

    def test_rotating_the_pairing_code_leaves_the_version_alone(self):
        # Nothing an agent caches changed, and its current token still
        # works, so it should not be sent back to re-read its whole world.
        before = self.sync().json()["config_version"]

        self.device.issue_pairing_code()

        self.assertEqual(self.sync().json()["config_version"], before)

    def test_another_devices_edit_leaves_this_version_alone(self):
        before = self.sync().json()["config_version"]

        create_station_link(create_connection(create_device()))

        self.assertEqual(self.sync().json()["config_version"], before)


class SyncLastReceivedTests(SyncTestCase):
    """What the machine's own station list colours its rows from.

    ADL's record rather than the agent's, which is the whole point: the agent
    keeps no history of what it delivered, so after a restart its own memory
    of a station is empty while this number is not.
    """

    def setUp(self):
        super().setUp()
        self.connection = create_connection(self.device, name="Vendor A")
        self.link = create_station_link(self.connection)

    def link_payload(self, index=0):
        return self.sync().json()["connections"][0]["station_links"][index]

    def received(self, moment, name="GAR_01.dat", link=None):
        """A file ADL holds, received at a moment of this test's choosing."""
        staged = stage_file(link or self.link, name, b"payload")
        type(staged).objects.filter(pk=staged.pk).update(received_at=moment)
        return staged

    def test_a_station_nothing_has_ever_arrived_for_says_so(self):
        # Not an error and not an omission: it is a station that has not
        # started yet, and the app draws it differently from one that stopped.
        self.assertIsNone(self.link_payload()["last_received_at"])

    def test_a_link_carries_when_anything_last_arrived_for_it(self):
        moment = dj_timezone.now() - timedelta(hours=2)

        self.received(moment)

        self.assertEqual(
            self.link_payload()["last_received_at"], wire_datetime(moment)
        )

    def test_the_newest_arrival_wins_however_the_files_are_ordered(self):
        newest = dj_timezone.now() - timedelta(minutes=10)

        self.received(dj_timezone.now() - timedelta(days=2), name="old.dat")
        self.received(newest, name="new.dat")
        self.received(dj_timezone.now() - timedelta(days=1), name="middle.dat")

        self.assertEqual(
            self.link_payload()["last_received_at"], wire_datetime(newest)
        )

    def test_a_file_that_failed_to_decode_still_counts_as_arrived(self):
        # Delivery is what this number is about. A decode failure is fixed in
        # the ADL admin by an administrator, and painting the machine's row
        # for it would hand the technician standing at it a fault they cannot
        # act on.
        moment = dj_timezone.now() - timedelta(minutes=5)
        staged = self.received(moment)
        type(staged).objects.filter(pk=staged.pk).update(
            status=AgentFileStatus.FAILED, last_error="no such column",
        )

        self.assertEqual(
            self.link_payload()["last_received_at"], wire_datetime(moment)
        )

    def test_the_query_count_does_not_grow_with_the_stations(self):
        """The promise the grouped query exists to keep.

        A machine with two vendors and forty stations syncs on every cycle,
        and the moment this becomes a query per link it becomes a query per
        link for every machine in the fleet. Compared against itself rather
        than asserted at a number, so an unrelated query added elsewhere in
        ``sync`` does not fail this test for the wrong reason.
        """
        self.received(dj_timezone.now())
        with_one = self.queries_to_sync()

        for index in range(4):
            link = create_station_link(
                self.connection,
                create_station(self.connection.network, name=f"Station {index}"),
            )
            self.received(dj_timezone.now(), name=f"f{index}.dat", link=link)

        self.assertEqual(self.queries_to_sync(), with_one)

    def queries_to_sync(self):
        """How many queries one sync takes as things stand."""
        with CaptureQueriesContext(connections["default"]) as captured:
            self.sync()

        return len(captured)

    def test_one_stations_files_never_speak_for_another(self):
        quiet = create_station_link(
            self.connection, create_station(self.connection.network, name="Wajir")
        )

        self.received(dj_timezone.now())

        payloads = {
            link["id"]: link["last_received_at"]
            for link in self.sync().json()["connections"][0]["station_links"]
        }

        self.assertIsNotNone(payloads[self.link.pk])
        self.assertIsNone(payloads[quiet.pk])


class SyncQuietWindowTests(SyncTestCase):
    """How long a vendor's stations may say nothing before the app marks them.

    Resolved to a number here rather than sent as a blank for the machine to
    fill in, so a deployment that changes its default is followed by every
    machine on the next cycle instead of only by freshly installed ones.
    """

    def admin_tier(self):
        return self.sync().json()["connections"][0]["admin"]

    def test_a_connection_that_states_nothing_gets_this_instances_number(self):
        create_connection(self.device)

        self.assertEqual(
            self.admin_tier()["stale_after_minutes"],
            DEFAULT_STATION_STALE_AFTER_MINUTES,
        )

    def test_a_vendor_that_writes_slowly_can_raise_it(self):
        create_connection(self.device, stale_after_minutes=1500)

        self.assertEqual(self.admin_tier()["stale_after_minutes"], 1500)

    @override_settings(ADL_AGENT_STATION_STALE_AFTER_MINUTES=90)
    def test_a_deployment_can_move_the_default_for_its_whole_fleet(self):
        create_connection(self.device)

        self.assertEqual(self.admin_tier()["stale_after_minutes"], 90)

    @override_settings(ADL_AGENT_STATION_STALE_AFTER_MINUTES=90)
    def test_a_connections_own_number_outranks_the_deployments(self):
        create_connection(self.device, stale_after_minutes=1500)

        self.assertEqual(self.admin_tier()["stale_after_minutes"], 1500)

    def test_changing_the_window_reaches_the_machine_as_a_new_version(self):
        connection = create_connection(self.device)
        before = self.sync().json()["config_version"]

        connection.stale_after_minutes = 720
        connection.save()

        self.assertGreater(self.sync().json()["config_version"], before)


class SyncServerTierTests(SyncTestCase):
    """What the instance says about itself.

    Read by nobody but a person: the agent shows these two strings on its
    Status tab and changes no behaviour on them. So what these tests pin is
    where the strings live and that they are the real numbers -- because the
    failure mode is not a crash, it is a technician reading a version that
    was true at some other time.
    """

    def server_tier(self):
        return self.sync().json()["server"]

    def test_the_instance_says_which_adl_it_is(self):
        self.assertEqual(self.server_tier()["adl_version"], adl_version)

    def test_the_instance_says_which_plugin_it_is(self):
        self.assertEqual(self.server_tier()["plugin_version"], PLUGIN_VERSION)

    def test_a_device_with_nothing_linked_yet_is_still_told(self):
        # The block describes the instance, not the machine asking, so a
        # device on its first morning gets it in full. This is also when
        # somebody is most likely to be looking: half of "why is this field
        # missing?" is answered by knowing which ADL is on the other end.
        device, token = paired_device(name="Fresh install")

        body = self.client.get(SYNC_URL, **bearer(token)).json()

        self.assertEqual(body["server"]["plugin_version"], PLUGIN_VERSION)

    def test_the_versions_are_top_level_and_not_in_the_device_block(self):
        # Every device on this instance gets the same two strings, so they do
        # not belong under a block whose whole meaning is "yours". Pinned
        # because the tidier-looking arrangement is the wrong one, and the
        # agent reads them from the top level.
        body = self.sync().json()

        self.assertNotIn("adl_version", body["device"])
        self.assertNotIn("plugin_version", body["device"])

    def test_the_packaged_version_is_not_stated_a_second_time(self):
        # setup.py reads version.py. A literal re-appearing there is how the
        # two come to disagree -- and they disagree silently, since nothing
        # compares them at build time.
        setup_py = (
            Path(__file__).resolve().parents[3] / "setup.py"
        ).read_text()

        self.assertNotIn(f'"{PLUGIN_VERSION}"', setup_py)
        self.assertIn("version.py", setup_py)
