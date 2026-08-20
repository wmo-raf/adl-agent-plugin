"""
Staged files becoming observations.

The seam under test is the one the scheduler uses: ``process_station`` on the
registered plugin. Nothing here reaches inside the drain — a test arranges
files the way an upload leaves them, runs the station the way Celery Beat
does, and reads the observation records, the ledger rows and the activity log
that come out. That is also the demo the slice promises: a vendor file arrives
and observations appear.

The decoding itself is the FTP plugin's, unmodified: its ``standard_csv``
decoder, configured by its ``StandardCSVConfig``. Reusing that ecosystem
whole is the reason the agent stages raw files rather than parsed records.
"""

from datetime import timedelta

import os

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone as dj_timezone

from adl.core.models import ObservationRecord
from adl.core.registries import plugin_registry
from adl.core.tasks import ingest_station_lock_key
from adl.monitoring.models import StationLinkActivityLog
from adl_agent_plugin.models import AgentFileStatus, AgentStationDataFile

from .helpers import (
    TemporaryMediaRoot,
    celsius,
    create_parameter,
    create_station_link,
    csv_file,
    decoding_connection,
    map_on_connection,
    observation_time,
    stage_file,
)


#: A storage that stores and reads perfectly well but cannot say where a file
#: is on disk -- which is the one thing decoders need, since they open files by
#: path. Django's in-memory backend is a real one of these, so the test does
#: not have to invent a half-working fake: an instance whose media lives on
#: MinIO or S3 behaves the same way.
PATHLESS_STORAGE = override_settings(STORAGES={
    "default": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
})


class DrainTestCase(TemporaryMediaRoot, TestCase):
    """One station, wired to decode, with a window its files fall inside."""

    def setUp(self):
        self.connection = decoding_connection()
        self.link = create_station_link(self.connection)
        self.link.start_date = dj_timezone.now() - timedelta(hours=6)
        self.link.save()

        self.temperature = create_parameter(name="air_temperature")
        map_on_connection(
            self.connection, self.temperature, celsius(), "AirTemp",
        )

        self.plugin = plugin_registry.get("adl_agent_plugin")

    def drain(self, station_link=None):
        """Run the station the way the scheduler runs it."""
        return self.plugin.process_station(station_link or self.link)

    def observations(self):
        return ObservationRecord.objects.filter(
            station=self.link.station, connection_id=self.connection.pk,
        ).order_by("time")

    def reloaded(self, data_file):
        return AgentStationDataFile.objects.get(pk=data_file.pk)


class HappyPathTests(DrainTestCase):
    def test_a_staged_file_becomes_observation_records(self):
        moment = observation_time(10)
        data_file = stage_file(self.link, "AWS_01.csv", csv_file((moment, 21.5)))

        saved = self.drain()

        self.assertEqual(saved, 1)
        record = self.observations().get()
        self.assertEqual(record.parameter, self.temperature)
        self.assertEqual(record.value, 21.5)
        self.assertEqual(record.time, moment)
        self.assertEqual(self.reloaded(data_file).status, AgentFileStatus.PROCESSED)

    def test_a_processed_file_says_what_it_contributed(self):
        data_file = stage_file(
            self.link,
            "AWS_01.csv",
            csv_file((observation_time(20), 21.5), (observation_time(10), 22.0)),
        )

        self.drain()

        processed = self.reloaded(data_file)
        self.assertEqual(processed.values_saved, 2)
        self.assertIsNotNone(processed.processed_at)
        self.assertEqual(processed.last_error, "")

    def test_every_waiting_file_is_drained_in_one_pass(self):
        stage_file(self.link, "AWS_01.csv", csv_file((observation_time(30), 20.0)))
        stage_file(self.link, "AWS_02.csv", csv_file((observation_time(20), 21.0)))
        stage_file(self.link, "AWS_03.csv", csv_file((observation_time(10), 22.0)))

        self.assertEqual(self.drain(), 3)
        self.assertEqual(
            list(self.observations().values_list("value", flat=True)),
            [20.0, 21.0, 22.0],
        )

    def test_the_run_reports_how_many_files_it_had_to_work_with(self):
        stage_file(self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)))
        stage_file(self.link, "AWS_02.csv", csv_file((observation_time(9), 21.6)))

        self.drain()

        log = StationLinkActivityLog.objects.filter(
            station_link=self.link, direction="pull",
        ).latest("time")
        self.assertEqual(log.sources_count, 2)

    def test_a_station_with_nothing_waiting_looked_and_found_nothing(self):
        self.assertEqual(self.drain(), 0)

        log = StationLinkActivityLog.objects.filter(
            station_link=self.link, direction="pull",
        ).latest("time")
        self.assertEqual(log.sources_count, 0)
        self.assertTrue(log.success)

    def test_a_file_that_decodes_to_nothing_ADL_maps_is_still_processed(self):
        """Zero saved is an outcome, not a failure.

        The file decoded; ADL simply had no mapping for what it held. That is
        a variable-mapping problem an operator fixes centrally, and re-running
        the file after fixing it is a re-process, not a retry.
        """
        data_file = stage_file(
            self.link,
            "AWS_01.csv",
            csv_file((observation_time(10), 21.5), column="Unmapped"),
        )

        self.assertEqual(self.drain(), 0)

        processed = self.reloaded(data_file)
        self.assertEqual(processed.status, AgentFileStatus.PROCESSED)
        self.assertEqual(processed.values_saved, 0)


class FailureIsolationTests(DrainTestCase):
    def test_a_file_that_will_not_decode_is_marked_failed_with_the_reason(self):
        data_file = stage_file(self.link, "broken.csv", b"nothing,useful\n1,2\n")

        self.drain()

        failed = self.reloaded(data_file)
        self.assertEqual(failed.status, AgentFileStatus.FAILED)
        self.assertIn("timestamp", failed.last_error)
        self.assertIsNone(failed.processed_at)

    def test_one_bad_file_does_not_cost_the_station_its_good_ones(self):
        bad = stage_file(self.link, "broken.csv", b"nothing,useful\n1,2\n")
        good = stage_file(
            self.link, "AWS_02.csv", csv_file((observation_time(10), 21.5)),
        )

        self.assertEqual(self.drain(), 1)

        self.assertEqual(self.reloaded(bad).status, AgentFileStatus.FAILED)
        self.assertEqual(self.reloaded(good).status, AgentFileStatus.PROCESSED)
        self.assertEqual(self.observations().count(), 1)

    def test_a_failed_file_is_not_tried_again_by_the_next_run(self):
        """A decode that failed will fail the same way on the same bytes.

        Retrying it every fifteen minutes for ever would bury the log and
        teach an operator to ignore it. Getting it decoded again is a
        deliberate re-process, or a new upload of changed bytes.
        """
        data_file = stage_file(self.link, "broken.csv", b"nothing,useful\n1,2\n")
        self.drain()
        first_error = self.reloaded(data_file).last_error

        self.drain()

        again = self.reloaded(data_file)
        self.assertEqual(again.status, AgentFileStatus.FAILED)
        self.assertEqual(again.last_error, first_error)

    def test_a_connection_with_no_decoder_leaves_its_files_alone(self):
        """Not a failure of the file: nobody has said what it means yet.

        Marking these failed would hide the real fault -- an unconfigured
        connection -- behind a hundred file-level errors, and would need every
        one of them reset once the decoder was chosen.
        """
        self.connection.decoder = ""
        self.connection.csv_config = None
        self.connection.save()

        data_file = stage_file(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )

        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.reloaded(data_file).status, AgentFileStatus.RECEIVED)


class IdempotencyTests(DrainTestCase):
    def test_a_processed_file_is_not_processed_again(self):
        data_file = stage_file(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )
        self.drain()
        first_processed_at = self.reloaded(data_file).processed_at

        self.assertEqual(self.drain(), 0)

        self.assertEqual(self.observations().count(), 1)
        self.assertEqual(self.reloaded(data_file).processed_at, first_processed_at)

    def test_a_run_that_collides_with_another_processes_nothing(self):
        """The nudge and the schedule can land together; only one may drain.

        Core holds a per-station lock for exactly this, and a collision is
        recorded as SKIPPED rather than lost. The file it did not touch is
        still waiting for whichever run holds the lock, or for the next one.
        """
        data_file = stage_file(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )

        lock_key = ingest_station_lock_key(self.link.pk)
        cache.add(lock_key, "locked", timeout=60)
        self.addCleanup(cache.delete, lock_key)

        self.assertEqual(self.drain(), 0)

        self.assertEqual(self.observations().count(), 0)
        self.assertEqual(self.reloaded(data_file).status, AgentFileStatus.RECEIVED)

        log = StationLinkActivityLog.objects.filter(station_link=self.link).latest("time")
        self.assertEqual(log.status, StationLinkActivityLog.ActivityStatus.SKIPPED)

    def test_a_file_that_grew_is_decoded_again_and_the_overlap_is_harmless(self):
        first = csv_file((observation_time(30), 20.0))
        data_file = stage_file(self.link, "today.csv", first)
        self.drain()

        grown = csv_file((observation_time(30), 20.0), (observation_time(10), 22.0))
        stage_file(self.link, "today.csv", grown)

        self.assertEqual(self.reloaded(data_file).status, AgentFileStatus.RECEIVED)

        self.drain()

        self.assertEqual(
            list(self.observations().values_list("value", flat=True)),
            [20.0, 22.0],
        )
        self.assertEqual(self.reloaded(data_file).status, AgentFileStatus.PROCESSED)


class UnreadableBytesTests(DrainTestCase):
    """Getting at the bytes failing is the instance's fault, not the file's."""

    def test_a_file_whose_bytes_have_gone_is_left_to_be_tried_again(self):
        """A storage fault must not permanently sideline a good file.

        ``failed`` is never retried and nothing in this slice can bring a
        failed row back, so one bad minute from the object store would cost a
        country its data. The row stays ``received`` and the next run -- once
        storage is healthy -- drains it.
        """
        data_file = stage_file(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )
        os.unlink(data_file.file.path)

        self.assertEqual(self.drain(), 0)

        still_waiting = self.reloaded(data_file)
        self.assertEqual(still_waiting.status, AgentFileStatus.RECEIVED)
        self.assertEqual(still_waiting.last_error, "")

    def test_an_unreadable_file_does_not_cost_the_station_its_readable_ones(self):
        gone = stage_file(
            self.link, "AWS_01.csv", csv_file((observation_time(20), 20.0)),
        )
        os.unlink(gone.file.path)
        stage_file(self.link, "AWS_02.csv", csv_file((observation_time(10), 21.5)))

        self.assertEqual(self.drain(), 1)


@PATHLESS_STORAGE
class ObjectStorageTests(DrainTestCase):
    """Bytes that live where there are no paths still get decoded."""

    def test_a_file_on_a_pathless_storage_is_decoded_from_a_local_copy(self):
        data_file = stage_file(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )

        self.assertEqual(self.drain(), 1)

        self.assertEqual(self.observations().get().value, 21.5)
        self.assertEqual(self.reloaded(data_file).status, AgentFileStatus.PROCESSED)


class RedactionTests(DrainTestCase):
    """A decoder's exception text is stored, listed and served. Bound it here."""

    def test_a_credential_in_a_decoder_error_is_not_stored_in_the_clear(self):
        data_file = stage_file(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )

        data_file.mark_failed(
            "Could not reach sftp://vendor:hunter2@files.example.org/AWS_01.csv"
        )

        stored = self.reloaded(data_file).last_error
        self.assertNotIn("hunter2", stored)
        self.assertIn("files.example.org", stored)
