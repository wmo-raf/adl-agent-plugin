"""
The collection history: what each machine has been doing, kept as rows.

ADL held exactly one cycle's worth of this and overwrote it every five
minutes. Every state below is reached the way a real fleet reaches it -- by
posting a heartbeat -- and then asked of the database, because the whole point
of wmo-raf/adl#307 is that the answer survives the next beat.
"""

import json
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as dj_timezone

from adl_agent_plugin.cycles import (
    DEFAULT_COMPRESS_AFTER_DAYS,
    DEFAULT_RETENTION_DAYS,
    apply_policies,
    compress_after_days,
    retention_days,
)
from adl_agent_plugin.models import (
    AgentCyclePass,
    AgentCyclePassOutcome,
    AgentCyclePassTrigger,
)

from .helpers import (
    bearer,
    create_connection,
    create_station_link,
    paired_device,
    wire_datetime,
)

HEARTBEAT_URL = reverse("plugins:adl_agent:heartbeat")
SYNC_URL = reverse("plugins:adl_agent:sync")


class CyclePassTestCase(TestCase):
    """A paired machine with one station, and the verb these are written in."""

    def setUp(self):
        self.device, self.token = paired_device(name="Bobo-Dioulasso server")
        self.connection = create_connection(device=self.device)
        self.station_link = create_station_link(connection=self.connection)

    def heartbeat(self, body):
        return self.client.post(
            HEARTBEAT_URL,
            data=body,
            content_type="application/json",
            **bearer(self.token),
        )

    def one_pass(self, **overrides):
        """A finished unit pass, as an agent sends it."""
        unit_pass = {
            "at": wire_datetime(dj_timezone.now()),
            "seconds": 2.5,
            "unit": "C:\\VendorData\\Bobo",
            "trigger": "scheduled",
            "completed": True,
            "folders": 3,
            "stations": [{
                "station_link_id": self.station_link.pk,
                "scanned": 40,
                "held": 1,
                "offered": 6,
                "wanted": 6,
                "uploaded": 5,
                "failed": 1,
                "backlog": 2,
            }],
            "missing": [],
        }
        unit_pass.update(overrides)

        return unit_pass


class CyclePassStorageTests(CyclePassTestCase):
    def test_a_beat_stores_a_row_per_station_per_pass(self):
        response = self.heartbeat({"completed_passes": [self.one_pass()]})

        self.assertEqual(response.status_code, 200)

        stored = AgentCyclePass.objects.get()

        self.assertEqual(stored.device, self.device)
        self.assertEqual(stored.station_link, self.station_link)
        self.assertEqual(stored.unit, "C:\\VendorData\\Bobo")
        self.assertEqual(stored.trigger, AgentCyclePassTrigger.SCHEDULED)
        self.assertTrue(stored.completed)
        self.assertEqual(stored.duration_ms, 2_500)
        self.assertEqual(stored.folders_walked, 3)
        self.assertEqual(stored.scanned, 40)
        self.assertEqual(stored.held, 1)
        self.assertEqual(stored.offered, 6)
        self.assertEqual(stored.wanted, 6)
        self.assertEqual(stored.uploaded, 5)
        self.assertEqual(stored.failed, 1)
        self.assertEqual(stored.backlog, 2)

    def test_the_beat_after_it_does_not_overwrite_it(self):
        """The whole point. A snapshot is overwritten; a pass is a row."""
        self.heartbeat({"completed_passes": [self.one_pass()]})
        self.heartbeat({"completed_passes": [
            self.one_pass(unit="C:\\VendorData\\Banfora"),
        ]})

        self.assertEqual(AgentCyclePass.objects.count(), 2)
        self.assertEqual(
            set(AgentCyclePass.objects.values_list("unit", flat=True)),
            {"C:\\VendorData\\Bobo", "C:\\VendorData\\Banfora"},
        )

    def test_a_pass_names_the_files_that_did_not_arrive(self):
        """The point of the whole field.

        ADL already stores the name of every file it received. This is the
        negative space -- and the difference between "this station is quiet"
        and "this station is quiet because the files are now called something
        else".
        """
        self.heartbeat({"completed_passes": [self.one_pass(missing=[
            {
                "name": "BOBO_20260819.dat",
                "outcome": "failed",
                "reason": "The share stopped answering.",
                "station_link_id": self.station_link.pk,
            },
            {"name": "BOBO_20260821.DAT", "outcome": "unmatched"},
        ])]})

        stored = AgentCyclePass.objects.get()

        self.assertEqual(
            [file["name"] for file in stored.missing_files],
            ["BOBO_20260819.dat", "BOBO_20260821.DAT"],
        )
        self.assertEqual(
            stored.missing_summary(),
            "BOBO_20260819.dat, BOBO_20260821.DAT",
        )

        # The unmatched one belongs to no station, which is exactly what makes
        # it invisible to every other number in this product -- so it travels
        # on every station of the unit rather than none.
        self.assertIsNone(stored.missing_files[1]["station_link_id"])

    def test_another_stations_failure_does_not_appear_on_this_ones_row(self):
        other = create_station_link(connection=self.connection)

        self.heartbeat({"completed_passes": [self.one_pass(
            stations=[
                {"station_link_id": self.station_link.pk, "scanned": 1},
                {"station_link_id": other.pk, "scanned": 1},
            ],
            missing=[{
                "name": "BANFORA_20260819.dat",
                "outcome": "failed",
                "reason": "The share stopped answering.",
                "station_link_id": other.pk,
            }],
        )]})

        mine = AgentCyclePass.objects.get(station_link=self.station_link)
        theirs = AgentCyclePass.objects.get(station_link=other)

        self.assertEqual(mine.missing_files, [])
        self.assertEqual(len(theirs.missing_files), 1)

    def test_a_pass_cut_short_is_stored_with_the_reason(self):
        """The record whose absence is hardest to explain."""
        self.heartbeat({"completed_passes": [self.one_pass(
            completed=False,
            stopped="ADL stopped answering before this station finished.",
            uploaded=None,
        )]})

        stored = AgentCyclePass.objects.get()

        self.assertFalse(stored.completed)
        self.assertIn("stopped answering", stored.stopped)
        self.assertEqual(stored.outcome, AgentCyclePassOutcome.CUT_SHORT)

    def test_a_station_this_ADL_does_not_know_is_dropped_not_refused(self):
        """A machine collecting a station HQ unlinked an hour ago is a
        machine doing what it was told, not one to argue with."""
        response = self.heartbeat({"completed_passes": [self.one_pass(
            stations=[
                {"station_link_id": self.station_link.pk, "scanned": 1},
                {"station_link_id": 999_999, "scanned": 1},
            ],
        )]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AgentCyclePass.objects.count(), 1)

    def test_a_quiet_pass_is_stored_too(self):
        """Filtering saves rows only on quiet stations -- which are precisely
        the ones where "the agent looked and there was nothing" is the fact
        worth having."""
        self.heartbeat({"completed_passes": [self.one_pass(stations=[{
            "station_link_id": self.station_link.pk,
            "scanned": 0, "offered": 0, "uploaded": 0, "failed": 0,
        }])]})

        stored = AgentCyclePass.objects.get()

        self.assertEqual(stored.scanned, 0)
        self.assertEqual(stored.outcome, AgentCyclePassOutcome.QUIET)

    def test_a_machine_that_said_nothing_is_told_apart_from_one_that_looked(self):
        """NULL is "the machine did not say"; zero is "it looked and there was
        nothing". Different faults."""
        self.heartbeat({"completed_passes": [self.one_pass(stations=[
            {"station_link_id": self.station_link.pk},
        ])]})

        stored = AgentCyclePass.objects.get()

        self.assertIsNone(stored.scanned)
        self.assertIsNone(stored.uploaded)

    def test_a_beat_carrying_no_passes_stores_none(self):
        """An agent new enough to have the field and with nothing to say."""
        self.heartbeat({
            "app_version": "1.5.0",
            "completed_passes": [],
            "last_cycle": {
                "completed_at": wire_datetime(dj_timezone.now()),
                "links": [{"station_link_id": self.station_link.pk,
                           "scanned": 4}],
            },
        })

        self.assertEqual(AgentCyclePass.objects.count(), 0)

    def test_how_many_passes_were_shed_is_recorded(self):
        """A gap in the history that nothing accounts for is a gap somebody
        reads as a machine that stopped."""
        self.heartbeat({
            "completed_passes": [self.one_pass()],
            "dropped_passes": 41,
        })

        self.device.refresh_from_db()

        self.assertEqual(self.device.heartbeat_details["dropped_passes"], 41)

    def test_a_table_that_will_not_take_a_write_does_not_cost_liveness(self):
        """A beat's first job is to say the machine is alive.

        History is worth having; it is not worth a country reading as offline
        because a diagnostic table would not take a write.
        """
        from unittest.mock import patch

        with patch.object(
            AgentCyclePass.objects, "bulk_create", side_effect=OSError("disk"),
        ):
            response = self.heartbeat({"completed_passes": [self.one_pass()]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AgentCyclePass.objects.count(), 0)

        self.device.refresh_from_db()

        self.assertIsNotNone(self.device.last_heartbeat_at)


class OldAgentTests(CyclePassTestCase):
    """An agent that predates completed_passes, which is a normal, long-lived
    state: agents auto-update and ADL instances are upgraded by a person, one
    country at a time."""

    def test_an_old_agent_stores_one_pass_per_beat(self):
        completed_at = dj_timezone.now() - timedelta(minutes=3)

        self.heartbeat({
            "app_version": "1.2.0",
            "last_cycle": {
                "completed_at": wire_datetime(completed_at),
                "links": [{
                    "station_link_id": self.station_link.pk,
                    "scanned": 40, "offered": 6, "uploaded": 5, "failed": 1,
                    "error": "The share stopped answering.",
                }],
            },
        })

        stored = AgentCyclePass.objects.get()

        self.assertEqual(stored.station_link, self.station_link)
        self.assertEqual(stored.scanned, 40)
        self.assertEqual(stored.uploaded, 5)
        self.assertEqual(stored.failed, 1)
        self.assertIn("stopped answering", stored.error)

        # Coarser, and honestly so: an old agent does not say which folder
        # its counts came from, and inventing one would be inventing a fact.
        self.assertEqual(stored.unit, "")
        self.assertEqual(stored.trigger, "")
        self.assertEqual(stored.time, completed_at)

    def test_an_old_agent_with_nothing_to_say_stores_nothing(self):
        response = self.heartbeat({"app_version": "1.2.0"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AgentCyclePass.objects.count(), 0)


class UnchangedSnapshotTests(CyclePassTestCase):
    """The regression this issue is most at risk of causing.

    ``last_cycle`` drives ``AgentDevice.last_cycle_completed_at``, which drives
    the cycle-stuck check. An agent -- or a plugin -- that stopped honouring it
    would make every auto-updated machine report as stuck to every ADL not yet
    upgraded.
    """

    def test_the_rolling_snapshot_still_lands_beside_the_passes(self):
        completed_at = dj_timezone.now()

        self.heartbeat({
            "last_cycle": {
                "completed_at": wire_datetime(completed_at),
                "links": [{"station_link_id": self.station_link.pk,
                           "scanned": 40, "uploaded": 5}],
            },
            "completed_passes": [self.one_pass()],
        })

        self.device.refresh_from_db()

        self.assertEqual(self.device.last_cycle_completed_at, completed_at)
        self.assertEqual(
            self.device.heartbeat_details["links"][0]["scanned"], 40,
        )
        self.assertEqual(AgentCyclePass.objects.count(), 1)


class RefusalTests(CyclePassTestCase):
    """Nothing is guessed. A field that is present and unreadable is refused,
    so an agent shipping a wrong shape learns it at once."""

    def test_a_pass_list_that_is_not_a_list_is_refused(self):
        response = self.heartbeat({"completed_passes": {"unit": "C:\\x"}})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_heartbeat")

    def test_a_station_with_no_id_is_refused(self):
        response = self.heartbeat({"completed_passes": [
            self.one_pass(stations=[{"scanned": 4}]),
        ]})

        self.assertEqual(response.status_code, 400)

    def test_more_passes_than_one_beat_may_carry_are_refused(self):
        response = self.heartbeat({
            "completed_passes": [self.one_pass()] * 201,
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(AgentCyclePass.objects.count(), 0)

    def test_a_trigger_this_ADL_cannot_label_is_stored_blank(self):
        """A newer agent is not a broken one, and refusing its beat would
        cost the liveness signal to save a label."""
        response = self.heartbeat({"completed_passes": [
            self.one_pass(trigger="whatever-comes-next"),
        ]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AgentCyclePass.objects.get().trigger, "")


class PolicyTests(TestCase):
    """How long a country keeps its diagnostic history is a deployment
    decision, read the way every other one in this plugin is."""

    def test_the_defaults_are_a_week_and_a_quarter(self):
        self.assertEqual(compress_after_days(), DEFAULT_COMPRESS_AFTER_DAYS)
        self.assertEqual(retention_days(), DEFAULT_RETENTION_DAYS)

    @override_settings(
        ADL_AGENT_CYCLE_COMPRESS_AFTER_DAYS=3,
        ADL_AGENT_CYCLE_RETENTION_DAYS=30,
    )
    def test_a_deployment_may_say_otherwise(self):
        self.assertEqual(compress_after_days(), 3)
        self.assertEqual(retention_days(), 30)

    @override_settings(
        ADL_AGENT_CYCLE_COMPRESS_AFTER_DAYS="a fortnight",
        ADL_AGENT_CYCLE_RETENTION_DAYS=0,
    )
    def test_nonsense_keeps_the_default_rather_than_stopping_the_instance(self):
        self.assertEqual(compress_after_days(), DEFAULT_COMPRESS_AFTER_DAYS)
        self.assertEqual(retention_days(), DEFAULT_RETENTION_DAYS)

    @override_settings(ADL_AGENT_CYCLE_RETENTION_DAYS=30)
    def test_applying_them_puts_the_configured_numbers_on_the_hypertable(self):
        from django.db import connection as db

        self.assertTrue(apply_policies())

        with db.cursor() as cursor:
            cursor.execute(
                "SELECT proc_name, config FROM timescaledb_information.jobs "
                "WHERE hypertable_name = %s",
                [AgentCyclePass._meta.db_table],
            )
            # The config column is jsonb, and psycopg hands it back as text
            # here rather than as a dict -- read it rather than assume either.
            jobs = {
                name: json.loads(config) if isinstance(config, str) else config
                for name, config in cursor.fetchall()
            }

        self.assertEqual(jobs["policy_retention"]["drop_after"], "30 days")
        self.assertEqual(
            jobs["policy_compression"]["compress_after"],
            f"{DEFAULT_COMPRESS_AFTER_DAYS} days",
        )


class LogLevelTests(CyclePassTestCase):
    """Raising a country server to Debug today means reaching the machine,
    which is the exact problem this product exists to solve."""

    def sync(self):
        return self.client.get(SYNC_URL, **bearer(self.token)).json()

    def test_a_device_with_no_opinion_sends_no_log_level(self):
        """Absent means "use the local setting" -- so a cleared field and an
        ADL that predates this look identical to the agent, which is what
        they are."""
        self.assertNotIn("log_level", self.sync()["device"])

    def test_ADL_may_raise_a_machines_log_level(self):
        self.device.log_level = "Debug"
        self.device.save()

        self.assertEqual(self.sync()["device"]["log_level"], "Debug")

    def test_changing_it_moves_the_configuration_version(self):
        """Otherwise it is a setting an administrator can change that no
        machine in the fleet will ever notice."""
        before = self.device.config_version

        self.device.log_level = "Debug"
        self.device.save()
        self.device.refresh_from_db()

        self.assertGreater(self.device.config_version, before)
