"""
Applying a fix to data that has already arrived.

A decoder gets fixed, a mapping gets added, a CSV configuration turns out to
name the wrong column -- and a country's files have already been received,
possibly months ago. Story 21 is the way back: an operator asks for those
files to be turned into observations again, and ADL works out how.

There are two ways, and which one is used is never the operator's problem.
While the bytes are still staged, ADL re-decodes them and the machine in the
field is not involved at all. Once retention has dropped them, the only copy
left is on the vendor's disk, so ADL forgets the file's hash and the machine
offers it again on its next manifest -- because no hash an agent can compute
equals nothing.

These tests drive the action from the admin, over HTTP, the way an operator
does, and read the result off the observation records, the ledger, and the
manifest the machine gets back.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_timezone

from adl.core.models import ObservationRecord
from adl.core.registries import plugin_registry
from adl_agent_plugin.models import AgentFileStatus, AgentStationDataFile
from adl_agent_plugin.retention import prune_expired_files

from .helpers import (
    AgentClient,
    TemporaryMediaRoot,
    UNHASHED_STATICFILES,
    celsius,
    clear_nudge_latch,
    create_parameter,
    create_station_link,
    csv_file,
    decoding_connection,
    manifest_entry,
    map_on_connection,
    observation_time,
    paired_device,
    stage_file,
    tasks_run_immediately,
)

BULK_ACTION_URL = reverse(
    "wagtail_bulk_action",
    args=["adl_agent_plugin", "agentstationdatafile", "reprocess"],
)


def reprocess_url(*data_files):
    """The action, addressed at these files, the way the listing addresses it."""
    query = urlencode([("id", data_file.pk) for data_file in data_files])
    return f"{BULK_ACTION_URL}?{query}"


@UNHASHED_STATICFILES
class ReprocessTestCase(TemporaryMediaRoot, TestCase):
    """One machine, one station, and an operator logged into the admin."""

    def setUp(self):
        self.device, token = paired_device()
        self.agent = AgentClient(self, token)

        self.connection = decoding_connection(device=self.device)
        self.link = create_station_link(self.connection)
        self.link.start_date = dj_timezone.now() - timedelta(days=30)
        self.link.save()

        self.temperature = create_parameter(name="air_temperature")
        self.mapping = map_on_connection(
            self.connection, self.temperature, celsius(), "AirTemp",
        )
        self.plugin = plugin_registry.get("adl_agent_plugin")

        self.operator = get_user_model().objects.create_superuser(
            username="hq", email="hq@example.com", password="hq-password",
        )
        self.client.force_login(self.operator)

        clear_nudge_latch(self.connection)
        self.addCleanup(clear_nudge_latch, self.connection)

    # -- arranging -------------------------------------------------------

    def content(self, minutes_ago=10, value=21.5):
        return csv_file((observation_time(minutes_ago), value))

    def processed_file(self, name="AWS_01.csv", content=None, mtime=None):
        data_file = stage_file(self.link, name, content or self.content(), mtime=mtime)
        self.plugin.process_station(self.link)
        return self.reloaded(data_file)

    def remap_to(self, column):
        """Point the vendor-column mapping somewhere else, as an operator would."""
        self.mapping.file_variable_name = column
        self.mapping.save()

    def prune(self):
        with self.captureOnCommitCallbacks(execute=True):
            return prune_expired_files()

    def age_and_prune(self, data_file):
        AgentStationDataFile.objects.filter(pk=data_file.pk).update(
            processed_at=dj_timezone.now() - timedelta(days=200),
        )
        self.prune()
        return self.reloaded(data_file)

    # -- acting ----------------------------------------------------------

    def reprocess(self, *data_files):
        """Press the action, and let the drain it asks for really run."""
        with tasks_run_immediately(), self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reprocess_url(*data_files))

    # -- reading ---------------------------------------------------------

    def reloaded(self, data_file):
        return AgentStationDataFile.objects.get(pk=data_file.pk)

    def observations(self):
        return ObservationRecord.objects.filter(
            station=self.link.station, connection_id=self.connection.pk,
        )


class HeldBytesTests(ReprocessTestCase):
    """The bytes are still here, so nobody in the field need hear about it."""

    def test_a_mapping_fixed_after_the_fact_reaches_a_file_already_received(self):
        # The column was named wrongly when the connection was set up, so the
        # files decoded perfectly and ADL kept nothing from any of them.
        self.remap_to("Temp")
        data_file = self.processed_file()
        self.assertEqual(data_file.values_saved, 0)
        self.assertFalse(self.observations().exists())

        self.remap_to("AirTemp")
        self.reprocess(data_file)

        self.assertEqual(self.observations().get().value, 21.5)
        self.assertEqual(self.reloaded(data_file).values_saved, 1)

    def test_a_fixed_configuration_brings_a_failed_file_back(self):
        moment = observation_time(10)
        # The vendor names its datetime column something the connection's CSV
        # configuration does not expect, so the file will not decode at all.
        data_file = self.processed_file(
            content=("when,AirTemp\n%s,21.5\n"
                     % moment.strftime("%Y-%m-%d %H:%M:%S")).encode(),
        )
        self.assertEqual(data_file.status, AgentFileStatus.FAILED)
        self.assertTrue(data_file.last_error)

        self.connection.csv_config.datetime_column = "when"
        self.connection.csv_config.save()
        self.reprocess(data_file)

        recovered = self.reloaded(data_file)
        self.assertEqual(recovered.status, AgentFileStatus.PROCESSED)
        self.assertEqual(recovered.last_error, "")
        self.assertEqual(self.observations().get().time, moment)

    def test_the_machine_is_asked_for_nothing(self):
        content = self.content()
        data_file = self.processed_file(content=content)

        self.reprocess(data_file)

        held = self.reloaded(data_file)
        self.assertEqual(held.content_hash, data_file.content_hash)
        self.assertEqual(
            self.agent.requested([manifest_entry(self.link, held.file_name, content)]),
            [],
        )


class PrunedBytesTests(ReprocessTestCase):
    """The bytes are gone, so the only copy left is the vendor's own."""

    def setUp(self):
        super().setUp()
        self.bytes_sent = self.content()
        # Written by the vendor a while ago, so that lowering the machine's
        # scan floor back to it is a real move rather than a no-op.
        self.sent_at = dj_timezone.now() - timedelta(days=20)
        self.data_file = self.age_and_prune(
            self.processed_file(content=self.bytes_sent, mtime=self.sent_at)
        )
        self.assertFalse(self.data_file.file)

    def entry(self, content=None):
        return manifest_entry(
            self.link, self.data_file.file_name, content or self.bytes_sent,
        )

    def test_the_machine_is_asked_for_the_file_again(self):
        self.assertEqual(self.agent.requested([self.entry()]), [])

        self.reprocess(self.data_file)

        self.assertEqual(
            self.agent.requested([self.entry()]),
            [(self.link.pk, self.data_file.file_name)],
        )

    def test_the_file_that_comes_back_is_processed(self):
        self.reprocess(self.data_file)

        with tasks_run_immediately(), self.captureOnCommitCallbacks(execute=True):
            response = self.agent.upload(
                self.link, self.data_file.file_name, self.bytes_sent,
            )

        self.assertEqual(response.status_code, 201)
        returned = self.reloaded(self.data_file)
        self.assertEqual(returned.status, AgentFileStatus.PROCESSED)
        self.assertEqual(returned.values_saved, 1)
        self.assertGreater(returned.processed_at, self.data_file.processed_at)
        self.assertEqual(self.observations().get().value, 21.5)

    def test_the_request_is_answered_when_the_file_lands(self):
        self.reprocess(self.data_file)
        self.assertTrue(self.reloaded(self.data_file).reoffer_request_is_live)

        with tasks_run_immediately(), self.captureOnCommitCallbacks(execute=True):
            self.agent.upload(
                self.link, self.data_file.file_name, self.bytes_sent,
            )

        answered = self.reloaded(self.data_file)
        self.assertIsNone(answered.reoffer_requested_at)
        self.assertFalse(answered.reoffer_request_is_live)

    def test_what_adl_made_of_it_stands_until_the_bytes_come_back(self):
        self.reprocess(self.data_file)

        waiting = self.reloaded(self.data_file)
        self.assertIsNone(waiting.content_hash)
        self.assertEqual(waiting.status, AgentFileStatus.PROCESSED)
        self.assertIsNotNone(waiting.processed_at)

    def test_the_file_is_inside_the_window_the_machine_scans(self):
        # A machine only offers what its watermark reaches, so asking for a
        # file older than the floor would be a request nobody could answer.
        self.link.start_date = dj_timezone.now() - timedelta(minutes=1)
        self.link.save()

        self.reprocess(self.data_file)

        watermark = self.link.manifest_watermark(
            AgentStationDataFile.reoffer_points_for([self.link]).get(self.link.pk)
        )
        self.assertLessEqual(watermark, self.data_file.mtime)


class ReprocessAdminTests(ReprocessTestCase):
    def test_the_file_listing_offers_the_action(self):
        # Where the operator actually is when they need it: the listing they
        # have just filtered down to a station's failures.
        self.processed_file()

        response = self.client.get(
            reverse("wagtailsnippets_adl_agent_plugin_agentstationdatafile:list")
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Re-process", response.content.decode())

    def test_the_confirmation_page_names_the_files(self):
        data_file = self.processed_file()

        response = self.client.get(reprocess_url(data_file))

        self.assertEqual(response.status_code, 200)
        self.assertIn("AWS_01.csv", response.content.decode())

    def test_one_press_covers_files_on_both_paths(self):
        held = self.processed_file(name="AWS_01.csv")
        pruned = self.age_and_prune(
            self.processed_file(name="AWS_02.csv", content=self.content(20, 22.0))
        )

        self.reprocess(held, pruned)

        self.assertEqual(self.reloaded(held).values_saved, 1)
        self.assertIsNone(self.reloaded(pruned).content_hash)

    def test_reading_the_listing_is_not_enough_to_press_it(self):
        # Re-processing writes: it resets a file ADL has already accounted
        # for, or asks a machine in the field for bytes again. Seeing the
        # listing is not permission to do either.
        self.remap_to("Temp")
        data_file = self.processed_file()

        reader = get_user_model().objects.create_user(
            username="reader", email="reader@example.com", password="reader-password",
        )
        reader.groups.add(Group.objects.get(name="Editors"))
        self.client.force_login(reader)
        self.remap_to("AirTemp")

        # They reach the page -- they are an admin user with the listing --
        # and are told the file is not theirs to re-process.
        page = self.client.get(reprocess_url(data_file))
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            "permission to re-process these files", page.content.decode(),
        )

        self.reprocess(data_file)

        self.assertFalse(self.observations().exists())
        self.assertEqual(self.reloaded(data_file).values_saved, 0)

    def test_the_bytes_a_file_still_has_are_shown_in_the_listing(self):
        held = self.processed_file(name="AWS_01.csv")
        pruned = self.age_and_prune(
            self.processed_file(name="AWS_02.csv", content=self.content(20, 22.0))
        )

        self.assertEqual(str(held.bytes_state()), "Held")
        self.assertEqual(str(pruned.bytes_state()), "Pruned")

        self.reprocess(pruned)
        self.assertEqual(str(self.reloaded(pruned).bytes_state()), "Awaiting re-send")
