"""
``GET /api/agent/v1/sync`` -- one call that hands a device its whole world.

The promise being tested is the one the agent depends on every cycle: after a
single request it knows which folders to scan, how to scan them, how far back
to look, and whether any of that has changed since last time. So the tests ask
for the response an agent would get and read it the way an agent would, rather
than reaching for the models the view happens to read.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone as dj_timezone

from adl_agent_plugin.models import AgentListingStrategy, AgentStationLink

from .helpers import (
    SYNC_URL,
    bearer,
    create_connection,
    create_device,
    create_station,
    create_station_link,
    paired_device,
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


class SyncConfigVersionTests(SyncTestCase):
    def test_a_settled_configuration_keeps_its_version(self):
        first = self.sync().json()["config_version"]
        second = self.sync().json()["config_version"]

        self.assertEqual(first, second)

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
