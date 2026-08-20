"""
A file arriving, and observations appearing.

This is the promise the whole inversion is for: not "ADL will collect this
eventually" but "you pushed it, and it is in". The scheduled pass would get
there on its own interval; the nudge is what makes the interval stop
mattering.

The tests drive it the way an agent does -- an HTTP upload -- and read the
observation records at the far end, with Celery running tasks in-process so
the nudge really runs. What they deliberately do not do is assert that some
task was enqueued: enqueuing is not the promise, arriving is.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone as dj_timezone

from adl.core.models import ObservationRecord

from .helpers import (
    AgentClient,
    TemporaryMediaRoot,
    celsius,
    clear_nudge_latch,
    create_parameter,
    create_station_link,
    csv_file,
    decoding_connection,
    map_on_connection,
    observation_time,
    paired_device,
    tasks_run_immediately,
)


class NudgeTestCase(TemporaryMediaRoot, TestCase):
    def setUp(self):
        self.device, token = paired_device()
        self.agent = AgentClient(self, token)

        self.connection = decoding_connection(device=self.device)
        self.link = create_station_link(self.connection)
        self.link.start_date = dj_timezone.now() - timedelta(hours=6)
        self.link.save()

        self.temperature = create_parameter(name="air_temperature")
        map_on_connection(self.connection, self.temperature, celsius(), "AirTemp")

        clear_nudge_latch(self.connection)
        self.addCleanup(clear_nudge_latch, self.connection)

    def observations(self, station_link=None):
        link = station_link or self.link
        return ObservationRecord.objects.filter(
            station=link.station, connection_id=link.network_connection_id,
        )

    def upload(self, *args, **kwargs):
        """Upload the way an agent does, and let the drain it asks for run.

        Two things stand between an upload in a test and the observations it
        should produce, and neither is about the plugin. The nudge is queued
        on commit, and a test's transaction never commits; and it is a Celery
        task, and no worker is listening. Both are arranged here so that each
        test below is about the data.
        """
        with tasks_run_immediately(), self.captureOnCommitCallbacks(execute=True):
            return self.agent.upload(*args, **kwargs)


class UploadDrainsImmediatelyTests(NudgeTestCase):
    def test_a_file_pushed_in_becomes_observations_without_waiting_for_the_clock(self):
        response = self.upload(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.observations().get().value, 21.5)

    def test_a_paused_connection_takes_the_file_but_does_not_process_it(self):
        """Pausing stops files being processed, not files arriving.

        A country whose mappings are being reworked should not also lose the
        data that arrived while the work was going on.
        """
        self.connection.plugin_processing_enabled = False
        self.connection.save()

        response = self.upload(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.observations().count(), 0)

    def test_a_refused_upload_asks_for_nothing(self):
        """Nothing arrived, so there is nothing to drain.

        The hash here does not describe the bytes, so the file is refused
        before it is stored -- and a drain scheduled off a refusal would be a
        machine able to spend ADL's workers by sending it rubbish.
        """
        response = self.upload(
            self.link,
            "AWS_01.csv",
            csv_file((observation_time(10), 21.5)),
            hash="0" * 64,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.observations().count(), 0)


class OneNudgePerBurstTests(NudgeTestCase):
    """A cycle uploads many files; each one is not a separate reason to drain."""

    def test_the_first_upload_of_a_burst_asks_for_the_drain(self):
        from adl_agent_plugin.tasks import nudge

        self.assertTrue(nudge(self.connection))

    def test_the_rest_of_the_burst_lets_that_drain_cover_them(self):
        from adl_agent_plugin.tasks import nudge

        nudge(self.connection)

        self.assertFalse(nudge(self.connection))
        self.assertFalse(nudge(self.connection))

    def test_another_connection_is_not_silenced_by_the_first(self):
        from adl_agent_plugin.tasks import nudge

        other = decoding_connection(device=self.device)
        self.addCleanup(clear_nudge_latch, other)

        nudge(self.connection)

        self.assertTrue(nudge(other))
