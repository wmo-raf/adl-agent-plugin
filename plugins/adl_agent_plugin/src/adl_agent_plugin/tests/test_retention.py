"""
Bounded disk, unbroken memory.

Staged bytes cannot be kept forever -- a country sending a file every ten
minutes fills a disk -- but the ledger row that remembers a file must be,
because a row pruned is a file eternally new and re-uploaded forever (story
22). So retention drops one and never the other, and these tests hold that
line from both ends: the bytes really are gone from storage, and the machine
that sent them is still told not to send them again.

The seams are the ones the system really uses -- the sweep's own entry point,
the manifest endpoint over HTTP, and the drain through ``process_station``.
Nothing here reaches inside the sweep to ask what it decided; it is read off
storage, the ledger and the wire.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone as dj_timezone

from adl.core.registries import plugin_registry
from adl_agent_plugin.models import AgentFileStatus, AgentStationDataFile
from adl_agent_plugin.retention import prune_expired_files

from .helpers import (
    AgentClient,
    TemporaryMediaRoot,
    celsius,
    create_parameter,
    create_station_link,
    csv_file,
    decoding_connection,
    manifest_entry,
    map_on_connection,
    observation_time,
    paired_device,
    stage_file,
)


def age_processing(data_file, days):
    """Push when this file was processed back into the past.

    A targeted UPDATE rather than a frozen clock: what retention reads is a
    column, and moving the column is both simpler and closer to what an
    instance running for a quarter really looks like.
    """
    AgentStationDataFile.objects.filter(pk=data_file.pk).update(
        processed_at=dj_timezone.now() - timedelta(days=days),
    )


class RetentionTestCase(TemporaryMediaRoot, TestCase):
    """One station that decodes, so a file can really reach ``processed``."""

    def setUp(self):
        self.connection = decoding_connection()
        self.link = create_station_link(self.connection)
        self.link.start_date = dj_timezone.now() - timedelta(hours=6)
        self.link.save()

        self.temperature = create_parameter(name="air_temperature")
        map_on_connection(self.connection, self.temperature, celsius(), "AirTemp")

        self.plugin = plugin_registry.get("adl_agent_plugin")

    def content(self, minutes_ago=10, value=21.5):
        return csv_file((observation_time(minutes_ago), value))

    def processed_file(self, name="AWS_01.csv", content=None, link=None):
        """A file that arrived and has been turned into observations."""
        link = link or self.link
        data_file = stage_file(link, name, content or self.content())
        self.plugin.process_station(link)
        return self.reloaded(data_file)

    def reloaded(self, data_file):
        return AgentStationDataFile.objects.get(pk=data_file.pk)

    def prune(self):
        """Run the sweep the way its schedule runs it."""
        with self.captureOnCommitCallbacks(execute=True):
            return prune_expired_files()


class PruningTests(RetentionTestCase):
    def test_a_file_past_its_retention_loses_its_bytes(self):
        data_file = self.processed_file()
        stored_as, storage = data_file.file.name, data_file.file.storage
        age_processing(data_file, days=100)

        pruned = self.prune()

        self.assertEqual(pruned, 1)
        self.assertFalse(storage.exists(stored_as))
        self.assertFalse(self.reloaded(data_file).file)

    def test_the_ledger_row_outlives_its_bytes(self):
        data_file = self.processed_file()
        age_processing(data_file, days=100)

        self.prune()

        kept = self.reloaded(data_file)
        self.assertEqual(kept.content_hash, data_file.content_hash)
        self.assertEqual(kept.size, data_file.size)
        self.assertEqual(kept.mtime, data_file.mtime)
        self.assertEqual(kept.status, AgentFileStatus.PROCESSED)
        self.assertEqual(kept.values_saved, data_file.values_saved)

    def test_a_file_inside_its_retention_keeps_its_bytes(self):
        data_file = self.processed_file()
        age_processing(data_file, days=10)

        self.assertEqual(self.prune(), 0)
        self.assertTrue(self.reloaded(data_file).file)

    def test_a_failed_file_keeps_its_bytes_however_old(self):
        # A failed row carries no processing time of its own, so the test
        # gives it one: what protects it has to be its status, not the
        # absence of a date that a later refactor could supply.
        data_file = self.processed_file(content=b"nothing,useful\n1,2\n")
        self.assertEqual(data_file.status, AgentFileStatus.FAILED)
        age_processing(data_file, days=400)

        self.assertEqual(self.prune(), 0)
        self.assertTrue(self.reloaded(data_file).file)

    def test_a_file_still_waiting_to_be_processed_keeps_its_bytes(self):
        # Same reasoning, and a sharper consequence: pruning a received file
        # would strand it -- bytes gone, nothing ever made of them, and a
        # ledger row saying the machine need not send it again.
        data_file = stage_file(self.link, "AWS_01.csv", self.content())
        age_processing(data_file, days=400)

        self.assertEqual(self.prune(), 0)
        self.assertTrue(self.reloaded(data_file).file)

    def test_retention_is_each_connections_own_to_set(self):
        brief = self.connection
        brief.file_retention_days = 1
        brief.save()

        patient = decoding_connection(file_retention_days=90)
        patient_link = create_station_link(patient)
        patient_link.start_date = dj_timezone.now() - timedelta(hours=6)
        patient_link.save()
        map_on_connection(patient, self.temperature, celsius(), "AirTemp")

        brief_file = self.processed_file()
        patient_file = self.processed_file(link=patient_link)
        age_processing(brief_file, days=5)
        age_processing(patient_file, days=5)

        self.assertEqual(self.prune(), 1)
        self.assertFalse(self.reloaded(brief_file).file)
        self.assertTrue(self.reloaded(patient_file).file)

    def test_a_connection_can_be_told_to_keep_every_byte(self):
        self.connection.file_retention_days = None
        self.connection.save()

        data_file = self.processed_file()
        age_processing(data_file, days=4000)

        self.assertEqual(self.prune(), 0)
        self.assertTrue(self.reloaded(data_file).file)

    def test_a_file_whose_bytes_are_already_gone_is_still_tidied(self):
        # Storage has lost the first file's bytes without ADL being told --
        # a media folder restored short, an object deleted by hand. The row
        # still points at them, and the sweep is what stops it lying; the
        # file behind it in the same sweep is still reached.
        first = self.processed_file(name="AWS_01.csv")
        second = self.processed_file(name="AWS_02.csv", content=self.content(20, 22.0))
        age_processing(first, days=100)
        age_processing(second, days=100)
        first.file.storage.delete(first.file.name)

        self.assertEqual(self.prune(), 2)
        self.assertFalse(self.reloaded(first).file)
        self.assertFalse(self.reloaded(second).file)


class PrunedFileTests(RetentionTestCase):
    """What a pruned file looks like from the machine that sent it."""

    def setUp(self):
        super().setUp()
        self.device, token = paired_device()
        self.connection.device = self.device
        self.connection.save()
        self.agent = AgentClient(self, token)

        self.bytes_sent = self.content()
        self.data_file = self.processed_file(content=self.bytes_sent)
        age_processing(self.data_file, days=100)
        self.prune()

    def test_the_machine_is_still_told_not_to_send_it(self):
        entry = manifest_entry(
            self.link, self.data_file.file_name, self.bytes_sent,
            mtime=self.data_file.mtime,
        )

        self.assertEqual(self.agent.requested([entry]), [])

    def test_a_file_the_machine_has_changed_is_still_asked_for(self):
        grown = self.bytes_sent + b"2026-01-01 00:00:00,22.5\n"
        entry = manifest_entry(self.link, self.data_file.file_name, grown)

        self.assertEqual(
            self.agent.requested([entry]),
            [(self.link.pk, self.data_file.file_name)],
        )

    def test_a_pruned_file_is_not_drained_again(self):
        # There is nothing left to decode, and the row already says what was
        # made of it. A sweep that left work behind would be a sweep that
        # kept re-reading files it had just deleted.
        self.assertEqual(self.plugin.process_station(self.link), 0)
        self.assertEqual(self.reloaded(self.data_file).status, AgentFileStatus.PROCESSED)
