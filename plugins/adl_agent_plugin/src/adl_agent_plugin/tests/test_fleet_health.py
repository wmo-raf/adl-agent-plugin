"""
The agent-shaped source layer, and the fleet listing above it.

Layer 5 of ADL's ingestion diagnostic asks every plugin the same question --
*is the source accepting us and offering data?* -- and for an agent
connection the source is a machine in a country that cannot be dialed. So the
answer comes from what that machine last said about itself, and these tests
drive it the way the fleet does: by posting a heartbeat, or by not posting
one (decision #264).

These are ``TestCase`` rather than the ``SimpleTestCase`` of
``test_source_checks``, and deliberately so: the whole subject here is stored
heartbeat state, which is the one thing a DB-free test cannot arrange.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_timezone

from django_celery_beat.models import PeriodicTask

from adl.core.models import NetworkConnectionHeartbeat
from adl.core.broker import IngestionQueueHealth
from adl.core.source_checks import SourceCheckStatus
from adl.core.tasks import INGESTION_TASK_NAME
from adl.monitoring.constants import LAYER_SOURCE, CheckState
from adl.monitoring.health import evaluate_connection_health
from adl.monitoring.models import SourceProbeResult, StationLinkActivityLog

from adl_agent_plugin.fleet import publish_source_evidence, sweep_liveness
from adl_agent_plugin.health import LivenessState, source_check_result
from adl_agent_plugin.models import AgentConnection

from .helpers import (
    UNHASHED_STATICFILES,
    at_time,
    bearer,
    create_connection,
    create_device,
    create_station_link,
    decoding_connection,
    paired_device,
    wire_datetime,
)

#: A broker that is doing its job, so the layers above the source pass and
#: the checklist reports what the machine is doing rather than what the test
#: environment is not.
HEALTHY_BROKER = IngestionQueueHealth(
    queue_depth=0, worker_consuming=True, running_tasks=(),
)

HEARTBEAT_URL = reverse("plugins:adl_agent:heartbeat")


def reloaded(connection):
    """The same connection, read fresh, with its device beside it."""
    return (AgentConnection.objects.select_related("device")
            .get(pk=connection.pk))


class HeartbeatDrivenTestCase(TestCase):
    """A paired machine, and the one verb these tests are written in.

    Shared rather than repeated per class: every state below is reached by
    posting a heartbeat or withholding one, so the posting belongs in one
    place and each class differs only in what it then reads.
    """

    device_name = "Machine"

    def setUp(self):
        self.device, self.token = paired_device(name=self.device_name)

    def heartbeat(self, body=None, ago=None):
        """Post a heartbeat, optionally as if it had arrived ``ago`` ago."""
        def post():
            return self.client.post(
                HEARTBEAT_URL,
                data=body if body is not None else {},
                content_type="application/json",
                **bearer(self.token),
            )

        if ago is None:
            return post()

        with at_time(dj_timezone.now() - ago):
            return post()


class AgentSourceCheckTests(HeartbeatDrivenTestCase):
    device_name = "Mwanza server"

    def setUp(self):
        super().setUp()
        self.connection = create_connection(self.device, name="Vendor A")

    def checked(self, **offset):
        """Layer 5's verdict for this connection ``offset`` from now.

        Re-read rather than reused: the heartbeat above was written by the
        endpoint, in its own instance of this device, and a check run against
        the copy this test happens to be holding would be reading a machine
        that never reported.
        """
        return source_check_result(
            reloaded(self.connection), dj_timezone.now() + timedelta(**offset),
        )

    def test_an_agent_connection_claims_the_source_layer(self):
        # Layer 5 has a subject here -- a machine, with an OS and a disk --
        # even though layer 4 does not, because ADL never dials it.
        self.assertTrue(self.connection.has_external_source)
        self.assertTrue(self.connection.source_probe_supported)
        self.assertIsNone(self.connection.get_source_endpoint())

    def test_a_reporting_machine_reads_ok(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        result = self.checked(minutes=1)

        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("Mwanza server", result.message)

    def test_a_degraded_machine_fails_the_layer_and_says_why(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        result = self.checked(minutes=11)

        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertIn("missed 2 heartbeats", result.message)

    def test_an_offline_machine_fails_the_layer(self):
        self.heartbeat({})

        result = self.checked(minutes=16)

        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertIn("missed 3 heartbeats", result.message)

    def test_a_stuck_cycle_fails_the_layer_and_is_worded_apart(self):
        stale = dj_timezone.now() - timedelta(minutes=30)

        self.heartbeat({"last_cycle": {"completed_at": wire_datetime(stale)}})

        result = self.checked(minutes=1)

        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertIn("heartbeating but", result.message)

    def test_an_unpaired_machine_fails_the_layer(self):
        connection = create_connection(create_device(), name="Never installed")

        result = source_check_result(connection)

        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertIn("not paired", result.message)

    def test_the_clock_advisory_rides_along_without_becoming_the_verdict(self):
        self.heartbeat({
            "device_time": wire_datetime(
                dj_timezone.now() + timedelta(minutes=20)
            ),
            "last_cycle": {"completed_at": wire_datetime(dj_timezone.now())},
        })

        result = self.checked(minutes=1)

        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("clock", result.message.lower())

    def test_the_check_never_claims_a_failure_category(self):
        # The closed vocabulary is one of things a server *said* -- codes,
        # refusals, handshakes. A machine that has gone quiet has said
        # nothing, so claiming one would invent a cause.
        self.heartbeat({})

        self.assertIsNone(self.checked(minutes=16).category)


class HealthChecklistTests(HeartbeatDrivenTestCase):
    """The verdict as the monitoring UI reaches it.

    Not the plugin's own function but core's evaluator, over a probe result
    stored exactly as pressing *Probe source* stores one -- because the claim
    this ticket makes is about the checklist an operator already reads, not
    about a function only this plugin calls.

    The connection is arranged healthy at every layer above the source, the
    way core's own evaluator tests arrange one, so that what the checklist
    reports is the machine's silence and not the test's scaffolding.
    """

    device_name = "Kigoma server"

    def setUp(self):
        super().setUp()
        # Fully configured, because core stops the ladder at a connection
        # whose own validation no longer passes -- and a connection with no
        # decoder is exactly that.
        self.connection = decoding_connection(self.device, name="Vendor A")
        self.make_healthy()

    def make_healthy(self):
        """Everything above layer 5, passing.

        Core made the schedule entry when the connection was saved, so this
        stamps that one rather than adding a second -- two entries for one
        connection is itself a layer-1 fault, and the test would then be
        reporting its own scaffolding.
        """
        PeriodicTask.objects.filter(
            task=INGESTION_TASK_NAME, args=f"[{self.connection.id}]",
        ).update(enabled=True, last_run_at=dj_timezone.now())
        NetworkConnectionHeartbeat.objects.create(
            connection=self.connection, last_run_at=dj_timezone.now(),
        )

    def swept_checklist(self, at=None):
        """The checklist as a real deployment reaches it.

        Not by pressing *Probe source* -- an operator opening the page has
        pressed nothing -- but by letting the plugin's own minute sweep
        publish the standing verdict and then reading what core makes of it.
        """
        at = at or dj_timezone.now()

        with at_time(at):
            sweep_liveness(at)
            publish_source_evidence(at)

        return evaluate_connection_health(
            reloaded(self.connection), queue_health=HEALTHY_BROKER, now=at,
        )

    def check(self, checklist, check_id):
        return next(check for check in checklist.checks
                    if check.id == check_id)

    def test_a_reporting_machine_shows_a_green_source_layer(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        check = self.check(self.swept_checklist(), "source_check")

        self.assertEqual(check.state, CheckState.OK)
        self.assertIn("Kigoma server", check.message)

    def test_a_silent_machine_becomes_the_first_failing_layer(self):
        self.heartbeat(ago=timedelta(minutes=16))

        checklist = self.swept_checklist()

        self.assertEqual(self.check(checklist, "source_check").state,
                         CheckState.FAILED)
        self.assertEqual(checklist.first_failing_layer, LAYER_SOURCE)
        self.assertIn("missed 3 heartbeats", checklist.headline_message)

    def test_a_degraded_machine_is_named_as_such_in_the_checklist(self):
        self.heartbeat(ago=timedelta(minutes=11))

        checklist = self.swept_checklist()

        self.assertEqual(checklist.first_failing_layer, LAYER_SOURCE)
        self.assertIn("missed 2 heartbeats", checklist.headline_message)

    def test_a_stuck_cycle_is_named_apart_from_an_outage(self):
        stale = dj_timezone.now() - timedelta(minutes=30)

        self.heartbeat({"last_cycle": {"completed_at": wire_datetime(stale)}})

        checklist = self.swept_checklist()

        self.assertEqual(checklist.first_failing_layer, LAYER_SOURCE)
        self.assertIn("heartbeating but", checklist.headline_message)

    def test_the_layer_adl_cannot_answer_reports_itself_inapplicable(self):
        # ADL never dials the machine, so the network path has no subject at
        # all. It says so, and being advisory it never displaces the source
        # layer's finding.
        self.heartbeat(ago=timedelta(minutes=16))

        network = self.check(self.swept_checklist(), "network_path")

        self.assertEqual(network.state, CheckState.NOT_APPLICABLE)
        self.assertFalse(network.blocking)

    def test_a_local_drain_never_speaks_for_the_machine(self):
        """The regression this layer exists to avoid.

        A machine uploads a few files and dies. The drain of what it left
        behind completes minutes later, and on a dial-out connection that
        completed run would be strong evidence -- real DNS, real auth, real
        bytes. Here it is a sweep of a staging store, and if it were allowed
        to speak, the connection would read green while the country is dark.
        """
        link = create_station_link(self.connection)
        self.heartbeat(ago=timedelta(minutes=16))
        StationLinkActivityLog.objects.create(
            station_link=link, direction="pull", success=True,
            status=StationLinkActivityLog.ActivityStatus.COMPLETED,
            sources_count=4, records_count=10,
            time=dj_timezone.now() - timedelta(minutes=3),
        )

        checklist = self.swept_checklist()

        self.assertEqual(self.check(checklist, "source_check").state,
                         CheckState.FAILED)
        self.assertEqual(checklist.first_failing_layer, LAYER_SOURCE)
        self.assertEqual(self.check(checklist, "network_path").state,
                         CheckState.NOT_APPLICABLE)

    def test_nobody_has_to_press_anything(self):
        self.heartbeat(ago=timedelta(minutes=16))

        # Nothing has probed this connection; the sweep is the only writer.
        self.assertFalse(SourceProbeResult.objects.exists())

        self.assertEqual(self.swept_checklist().first_failing_layer,
                         LAYER_SOURCE)


class PublishedVerdictTests(HeartbeatDrivenTestCase):
    """The sweep publishes a verdict; it does not keep a time series."""

    device_name = "Tabora server"

    def setUp(self):
        super().setUp()
        self.connection = decoding_connection(self.device, name="Vendor A")

    def rows(self):
        return SourceProbeResult.objects.filter(
            connection=self.connection, station_link__isnull=True,
        ).count()

    def publish(self, **offset):
        """One whole sweep, in the order the scheduled task runs it.

        Both halves, because the second reads what the first has just
        written: the states move, then the verdicts that describe them are
        published.
        """
        at = dj_timezone.now() + timedelta(**offset)
        with at_time(at):
            sweep_liveness(at)
            return publish_source_evidence(at)

    def test_the_first_sweep_publishes_the_standing_verdict(self):
        self.heartbeat({})

        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.rows(), 1)

    def test_a_verdict_that_has_not_changed_is_not_written_again(self):
        self.heartbeat({})
        self.publish()

        self.assertEqual(self.publish(minutes=1), 0)
        self.assertEqual(self.publish(minutes=2), 0)
        self.assertEqual(self.rows(), 1)

    def test_a_changed_verdict_is_written_at_once(self):
        self.heartbeat({})
        self.publish()

        self.assertEqual(self.publish(minutes=16), 1)
        self.assertEqual(self.rows(), 2)

    def test_an_unchanged_verdict_is_refreshed_before_core_distrusts_it(self):
        # Core discards a probe result after fifteen minutes, so a standing
        # verdict has to be restated well inside that.
        self.heartbeat({})
        self.publish()

        self.assertEqual(self.publish(minutes=5), 1)

    def test_a_paused_connection_publishes_nothing(self):
        self.heartbeat({})
        self.connection.plugin_processing_enabled = False
        self.connection.save()

        self.assertEqual(self.publish(), 0)


@UNHASHED_STATICFILES
class FleetListingTests(HeartbeatDrivenTestCase):
    """The listing an operator scans down when a country goes quiet."""

    device_name = "Songea server"

    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_superuser(
            username="hq", email="hq@example.com", password="hq-password",
        )
        self.client.force_login(self.admin)

    def listing(self):
        return self.client.get(reverse("agent_devices:index"))

    def test_the_listing_shows_what_the_machine_reported(self):
        self.heartbeat({
            "app_version": "1.4.0",
            "device_time": wire_datetime(
                dj_timezone.now() + timedelta(minutes=9)
            ),
            "last_cycle": {"completed_at": wire_datetime(dj_timezone.now())},
        })
        self.device.refresh_from_db()
        self.device.pinned_version = "1.3.9"
        self.device.save()

        response = self.listing()

        self.assertContains(response, "Songea server")
        self.assertContains(response, "1.4.0")
        self.assertContains(response, "1.3.9")
        self.assertContains(response, str(LivenessState.LABELS[
            LivenessState.ONLINE
        ]))
        self.assertContains(response, self.device.clock_skew_display)

    def test_a_machine_that_has_never_reported_shows_no_skew(self):
        response = self.listing()

        self.assertContains(response, "—")

    def test_the_device_page_shows_the_reading_behind_the_state(self):
        self.heartbeat({
            "app_version": "1.4.0",
            "os_version": "Windows Server 2019",
            "backlog_count": 7,
            "last_cycle": {
                "completed_at": wire_datetime(dj_timezone.now()),
                "links": [{"station_link_id": 12, "scanned": 40, "offered": 2,
                           "uploaded": 2, "failed": 0}],
            },
            "disk": [{"volume": "C:", "free_bytes": 100, "total_bytes": 500}],
        })

        response = self.client.get(
            reverse("agent_devices:edit", args=[self.device.pk])
        )

        self.assertContains(response, "Windows Server 2019")
        self.assertContains(response, "C:")
        self.assertContains(response, "Files waiting to be sent")

    def test_the_pin_is_editable_from_the_device_form(self):
        response = self.client.post(
            reverse("agent_devices:edit", args=[self.device.pk]),
            {"name": self.device.name, "description": "",
             "check_interval_minutes": 5, "dated_folder_window_hours": 48,
             "pinned_version": "1.3.9"},
        )

        self.assertEqual(response.status_code, 302, response.content)
        self.device.refresh_from_db()
        self.assertEqual(self.device.pinned_version, "1.3.9")
