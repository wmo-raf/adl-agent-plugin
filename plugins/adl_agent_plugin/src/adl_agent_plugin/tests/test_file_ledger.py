"""
The per-link watermark, once there is a ledger behind it.

The watermark is the oldest point a station's files are still worth offering
from, and every test here is really asking one question: can this rule ever
hide a file ADL still needs? Two promises make that question sharp. A file
backfilled into a folder weeks late must still reach ADL (story 15), and a
fresh install facing months of backlog uploads newest first *so that history
fills in behind* (story 18) -- so a floor that rose to the newest file ADL
happened to receive would close over both.

What the ledger contributes, then, is not a rise. It is the power to pull the
floor back *down*, to a file ADL has decided it wants offered again.

Read through the sync endpoint, because that is where an agent reads it.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone as dj_timezone

from adl_agent_plugin.models import AgentStationDataFile

from .helpers import (
    SYNC_URL,
    AgentClient,
    TemporaryMediaRoot,
    bearer,
    create_connection,
    create_station_link,
    manifest_entry,
    paired_device,
    wire_datetime,
)


class WatermarkTestCase(TemporaryMediaRoot, TestCase):
    def setUp(self):
        self.device, self.token = paired_device()
        self.connection = create_connection(self.device)
        self.now = dj_timezone.now()
        self.link = create_station_link(
            self.connection, start_date=self.hours_ago(48)
        )
        self.agent = AgentClient(self, self.token)

    def hours_ago(self, hours):
        return self.now - timedelta(hours=hours)

    def watermark(self):
        body = self.client.get(SYNC_URL, **bearer(self.token)).json()
        return body["connections"][0]["station_links"][0]["watermark"]

    def receive(self, name, mtime, content=None):
        response = self.agent.upload(
            self.link, name, content or name.encode(), mtime=mtime
        )
        self.assertEqual(response.status_code, 201, response.content)


class EmptyLedgerTests(WatermarkTestCase):
    def test_with_nothing_received_the_start_date_is_the_floor(self):
        self.assertEqual(self.watermark(), wire_datetime(self.hours_ago(48)))

    def test_with_no_start_date_there_is_no_floor_at_all(self):
        self.link.start_date = None
        self.link.save()

        self.assertIsNone(self.watermark())


class SettledLedgerTests(WatermarkTestCase):
    def test_receiving_a_file_does_not_raise_the_floor(self):
        # The whole of story 18: the newest file arriving first must not
        # close the window over the months of history behind it.
        self.receive("NEW.dat", self.hours_ago(2))

        self.assertEqual(self.watermark(), wire_datetime(self.hours_ago(48)))

    def test_an_older_file_is_still_asked_for_after_a_newer_one_arrives(self):
        self.receive("NEW.dat", self.hours_ago(2))

        backlog = manifest_entry(
            self.link, "OLD.dat", b"OLD.dat", self.hours_ago(30)
        )

        self.assertEqual(
            self.agent.requested([backlog]), [(self.link.pk, "OLD.dat")]
        )

    def test_a_file_backfilled_weeks_late_is_still_asked_for(self):
        # Recovered data, copied into the folder long after it was written.
        # Its contents are old; ADL has never seen it; it is wanted.
        self.receive("NEW.dat", self.hours_ago(2))

        backfilled = manifest_entry(
            self.link, "RECOVERED.dat", b"recovered\\n", self.hours_ago(40)
        )

        self.assertEqual(
            self.agent.requested([backfilled]), [(self.link.pk, "RECOVERED.dat")]
        )

    def test_a_file_renamed_on_the_machine_is_asked_for_under_its_new_name(self):
        # The ledger's identity is (station, filename). Nothing tries to
        # recognise the same bytes under another name -- re-sending is cheap,
        # and the core's upsert makes the re-ingested overlap harmless.
        content = b"time,temp\\n"
        self.receive("BEFORE.dat", self.hours_ago(2), content)

        renamed = manifest_entry(
            self.link, "AFTER.dat", content, self.hours_ago(2)
        )

        self.assertEqual(
            self.agent.requested([renamed]), [(self.link.pk, "AFTER.dat")]
        )


class ReoffereredFileTests(WatermarkTestCase):
    """A row whose hash has been cleared is a file ADL wants back.

    That is the re-process path for bytes ADL has pruned: the row stays, its
    hash goes, and the floor has to come down far enough for the agent to
    offer the file again -- otherwise clearing the hash would be a request
    nobody could hear.
    """

    def test_the_floor_comes_down_to_a_file_adl_wants_again(self):
        self.receive("OLD.dat", self.hours_ago(70))
        self.receive("NEW.dat", self.hours_ago(2))

        AgentStationDataFile.objects.filter(file_name="OLD.dat").update(
            content_hash=None
        )

        self.assertEqual(self.watermark(), wire_datetime(self.hours_ago(70)))

    def test_a_file_already_inside_the_window_leaves_the_floor_alone(self):
        self.receive("NEW.dat", self.hours_ago(2))

        AgentStationDataFile.objects.update(content_hash=None)

        self.assertEqual(self.watermark(), wire_datetime(self.hours_ago(48)))

    def test_the_file_that_comes_down_to_is_then_asked_for(self):
        self.receive("OLD.dat", self.hours_ago(70), b"OLD.dat")
        AgentStationDataFile.objects.update(content_hash=None)

        entry = manifest_entry(
            self.link, "OLD.dat", b"OLD.dat", self.hours_ago(70)
        )

        self.assertEqual(
            self.agent.requested([entry]), [(self.link.pk, "OLD.dat")]
        )

    def test_one_stations_request_does_not_move_another_stations_floor(self):
        other = create_station_link(
            self.connection, start_date=self.hours_ago(48)
        )
        self.receive("OLD.dat", self.hours_ago(70))
        AgentStationDataFile.objects.update(content_hash=None)

        body = self.client.get(SYNC_URL, **bearer(self.token)).json()
        floors = {
            link["id"]: link["watermark"]
            for link in body["connections"][0]["station_links"]
        }

        self.assertEqual(floors[self.link.pk], wire_datetime(self.hours_ago(70)))
        self.assertEqual(floors[other.pk], wire_datetime(self.hours_ago(48)))


class WatermarkCostTests(WatermarkTestCase):
    def test_a_device_with_many_stations_is_still_one_sync_call(self):
        """Adding stations must not add queries.

        The watermark is the one part of a sync response that has to ask the
        ledger something, so it is the one that could quietly become a query
        per station. A machine with forty stations on a satellite link is
        exactly the case this endpoint exists to serve.
        """
        self.receive("ONE.dat", self.hours_ago(2))

        with CaptureQueriesContext(connection) as one_station:
            self.watermark()

        for n in range(4):
            link = create_station_link(self.connection)
            self.assertEqual(
                self.agent.upload(link, f"S{n}.dat", b"one,two\\n").status_code, 201
            )

        with CaptureQueriesContext(connection) as five_stations:
            self.watermark()

        self.assertEqual(len(five_stations), len(one_station))
