"""
The heartbeat endpoint: what a machine says about itself, and what ADL makes
of it.

Every state in this module is reached the way a real fleet reaches it -- by
posting a heartbeat, or by not posting one, and then asking what ADL believes
some minutes later. Nothing here sets a state, a timestamp or a transition
directly: the point of the liveness ladder is that it is derived from
heartbeats, and a test that wrote a state would be asserting nothing.

Time is moved rather than waited for. ``now`` is an argument all the way down
from ``liveness_of``, so "fifteen minutes of silence" is a value, not a sleep.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as dj_timezone

from adl_agent_plugin.fleet import sweep_liveness
from adl_agent_plugin.health import LivenessState, liveness_of
from adl_agent_plugin.models import AgentDevice, AgentDeviceStateTransition

from .helpers import (
    at_time,
    bearer,
    create_station_link,
    paired_device,
    wire_datetime,
)

HEARTBEAT_URL = reverse("plugins:adl_agent:heartbeat")
SYNC_URL = reverse("plugins:adl_agent:sync")


class HeartbeatTestCase(TestCase):
    """A paired machine, and the two verbs these tests are written in."""

    def setUp(self):
        self.device, self.token = paired_device(name="Dodoma server")

    def heartbeat(self, body=None, version=None):
        headers = bearer(self.token)

        if version is not None:
            headers["HTTP_X_AGENT_VERSION"] = version

        return self.client.post(
            HEARTBEAT_URL,
            data=body if body is not None else {},
            content_type="application/json",
            **headers,
        )

    def believed(self, **offset):
        """What ADL believes about the machine ``offset`` from now.

        The clock is an argument rather than something to wait for, which is
        the only concession these tests make to not being a real fleet. The
        input is still only what was posted -- or not posted -- above.
        """
        self.device.refresh_from_db()
        return liveness_of(self.device, dj_timezone.now() + timedelta(**offset))


class HeartbeatSnapshotTests(HeartbeatTestCase):
    def test_a_heartbeat_records_the_report_on_the_device(self):
        response = self.heartbeat({
            "app_version": "1.4.0",
            "os_version": "Windows Server 2019 (10.0.17763)",
            "uptime_seconds": 93_600,
            "backlog_count": 7,
            "last_cycle": {
                "completed_at": wire_datetime(dj_timezone.now()),
                "links": [{"station_link_id": 12, "scanned": 40, "offered": 2,
                           "uploaded": 2, "failed": 0}],
            },
            "disk": [{"volume": "C:", "free_bytes": 100, "total_bytes": 500}],
        })

        self.assertEqual(response.status_code, 200, response.content)
        self.device.refresh_from_db()
        self.assertEqual(self.device.agent_version, "1.4.0")
        self.assertEqual(self.device.os_version,
                         "Windows Server 2019 (10.0.17763)")
        self.assertIsNotNone(self.device.last_heartbeat_at)
        self.assertIsNotNone(self.device.last_cycle_completed_at)
        self.assertEqual(self.device.heartbeat_details["backlog_count"], 7)
        self.assertEqual(self.device.heartbeat_details["uptime_seconds"], 93_600)
        self.assertEqual(
            self.device.heartbeat_details["links"][0]["station_link_id"], 12,
        )
        self.assertEqual(
            self.device.heartbeat_details["volumes"][0]["volume"], "C:",
        )

    def test_the_server_computes_the_clock_skew_from_the_device_clock(self):
        ahead = dj_timezone.now() + timedelta(minutes=8)

        response = self.heartbeat({"device_time": wire_datetime(ahead)})

        self.device.refresh_from_db()
        # Signed, and positive for a machine running ahead of ADL. Compared
        # loosely because the two clocks are read a few milliseconds apart.
        self.assertAlmostEqual(self.device.clock_skew_seconds, 8 * 60, delta=5)
        self.assertAlmostEqual(
            response.json()["clock_skew_seconds"], 8 * 60, delta=5,
        )

    def test_a_machine_behind_adl_reports_a_negative_skew(self):
        behind = dj_timezone.now() - timedelta(minutes=12)

        self.heartbeat({"device_time": wire_datetime(behind)})

        self.device.refresh_from_db()
        self.assertAlmostEqual(self.device.clock_skew_seconds, -12 * 60, delta=5)

    def test_a_heartbeat_without_a_clock_leaves_the_last_skew_alone(self):
        self.heartbeat({"device_time": wire_datetime(
            dj_timezone.now() + timedelta(minutes=8)
        )})

        self.heartbeat({"app_version": "1.4.1"})

        self.device.refresh_from_db()
        self.assertAlmostEqual(self.device.clock_skew_seconds, 8 * 60, delta=5)

    def test_an_empty_heartbeat_is_enough_to_say_the_machine_is_alive(self):
        response = self.heartbeat({})

        self.assertEqual(response.status_code, 200, response.content)
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_heartbeat_at)

    def test_the_response_carries_the_cadence_and_the_config_version(self):
        body = self.heartbeat({}).json()

        self.assertEqual(body["heartbeat_interval_minutes"], 5)
        self.assertEqual(body["check_interval_minutes"],
                         self.device.check_interval_minutes)
        self.assertEqual(body["config_version"],
                         self.device.current_config_version())
        self.assertEqual(body["status"], LivenessState.ONLINE)

    def test_the_cadence_is_handed_out_in_sync_as_well(self):
        body = self.client.get(SYNC_URL, **bearer(self.token)).json()

        self.assertEqual(body["device"]["heartbeat_interval_minutes"], 5)

    def test_the_reconciliation_cadence_rides_the_response_too(self):
        # The third cadence a machine runs on, beside the scan loop and the
        # heartbeat loop. It travels on the call made most often for the same
        # reason the other two do: a fleet follows a change to it without
        # anybody being sent to a machine.
        body = self.heartbeat({}).json()

        self.assertEqual(body["reconciliation_interval_hours"], 24)

    @override_settings(ADL_AGENT_RECONCILIATION_INTERVAL_HOURS=0)
    def test_a_deployment_can_switch_reconciliation_off_over_the_heartbeat(self):
        self.assertEqual(
            self.heartbeat({}).json()["reconciliation_interval_hours"], 0
        )

    def test_an_unauthenticated_heartbeat_is_refused(self):
        response = self.client.post(
            HEARTBEAT_URL, data={}, content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    def test_a_revoked_device_cannot_heartbeat(self):
        self.device.revoke()

        self.assertEqual(self.heartbeat({}).status_code, 401)


class AgentVersionHeaderTests(HeartbeatTestCase):
    """The version travels on every call, not only on heartbeats."""

    def test_the_header_is_recorded_from_any_authenticated_call(self):
        self.client.get(
            SYNC_URL, HTTP_X_AGENT_VERSION="2.0.1", **bearer(self.token),
        )

        self.device.refresh_from_db()
        self.assertEqual(self.device.agent_version, "2.0.1")

    def test_a_later_call_without_the_header_keeps_the_known_version(self):
        self.client.get(
            SYNC_URL, HTTP_X_AGENT_VERSION="2.0.1", **bearer(self.token),
        )

        self.client.get(SYNC_URL, **bearer(self.token))

        self.device.refresh_from_db()
        self.assertEqual(self.device.agent_version, "2.0.1")

    def test_an_upgrade_is_visible_from_the_first_call_after_it(self):
        self.client.get(
            SYNC_URL, HTTP_X_AGENT_VERSION="2.0.1", **bearer(self.token),
        )

        self.client.get(
            SYNC_URL, HTTP_X_AGENT_VERSION="2.1.0", **bearer(self.token),
        )

        self.device.refresh_from_db()
        self.assertEqual(self.device.agent_version, "2.1.0")

    def test_the_body_wins_on_a_heartbeat_that_carries_both(self):
        # The heartbeat's own field is the machine's considered statement
        # about itself; the header is a convenience on every other call.
        self.heartbeat({"app_version": "3.0.0"}, version="2.9.9")

        self.device.refresh_from_db()
        self.assertEqual(self.device.agent_version, "3.0.0")


class MalformedHeartbeatTests(HeartbeatTestCase):
    """Optional is not the same as unread: a field that is there and wrong is
    refused, so an agent shipping the wrong shape learns at once instead of
    looking healthy while every number ADL shows is missing."""

    def assertRefused(self, body):
        response = self.heartbeat(body)

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["code"], "invalid_heartbeat")

        self.device.refresh_from_db()
        self.assertIsNone(self.device.last_heartbeat_at)

    def test_a_body_that_is_not_an_object(self):
        self.assertRefused(["alive"])

    def test_a_clock_that_is_not_a_time(self):
        self.assertRefused({"device_time": "yesterday"})

    def test_a_count_that_is_not_a_number(self):
        self.assertRefused({"backlog_count": "lots"})

    def test_a_negative_count(self):
        self.assertRefused({"uptime_seconds": -1})

    def test_a_cycle_link_without_a_station(self):
        self.assertRefused({"last_cycle": {"links": [{"scanned": 3}]}})

    def test_more_links_than_a_heartbeat_may_describe(self):
        self.assertRefused({"last_cycle": {"links": [
            {"station_link_id": index + 1} for index in range(501)
        ]}})


class LivenessLadderTests(HeartbeatTestCase):
    """The four states, each reached by posting or withholding heartbeats.

    The cadence is five minutes, so the thresholds land at ten and fifteen.
    """

    def test_a_machine_that_has_just_reported_is_online(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        self.assertEqual(self.believed(minutes=1).state, LivenessState.ONLINE)

    def test_two_missed_heartbeats_are_degraded(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        self.assertEqual(self.believed(minutes=11).state,
                         LivenessState.DEGRADED)

    def test_three_missed_heartbeats_are_offline(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        self.assertEqual(self.believed(minutes=16).state,
                         LivenessState.OFFLINE)

    def test_a_later_heartbeat_brings_a_silent_machine_back(self):
        self.heartbeat({})
        self.assertEqual(self.believed(minutes=16).state,
                         LivenessState.OFFLINE)

        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        self.assertEqual(self.believed(minutes=1).state, LivenessState.ONLINE)

    def test_fresh_heartbeats_with_a_stale_cycle_are_cycle_stuck(self):
        # The machine keeps saying it is alive, and keeps reporting the same
        # completed cycle. That is the fault a heartbeat exists to separate
        # from an outage: the service lives, its work does not.
        stale = dj_timezone.now() - timedelta(minutes=30)

        self.heartbeat({"last_cycle": {"completed_at": wire_datetime(stale)}})

        self.assertEqual(self.believed(minutes=1).state,
                         LivenessState.CYCLE_STUCK)

    def test_a_machine_reporting_no_cycle_at_all_is_stuck_once_overdue(self):
        # A freshly installed machine that heartbeats and never finishes a
        # scan: nothing is wrong with the server, and nothing is arriving.
        self.heartbeat({})
        self.assertEqual(self.believed(minutes=1).state, LivenessState.ONLINE)

        # It keeps saying it is alive, and keeps reporting no cycle. Its
        # check interval is five minutes, so two of them is the bound.
        with at_time(dj_timezone.now() + timedelta(minutes=11)):
            self.heartbeat({})

        self.assertEqual(self.believed(minutes=11).state,
                         LivenessState.CYCLE_STUCK)

    def test_silence_outranks_a_stale_cycle(self):
        # A machine that has stopped talking cannot be observed to be
        # cycling, so "offline" is the honest answer, not "cycle stuck".
        stale = dj_timezone.now() - timedelta(minutes=30)

        self.heartbeat({"last_cycle": {"completed_at": wire_datetime(stale)}})

        self.assertEqual(self.believed(minutes=16).state,
                         LivenessState.OFFLINE)

    def test_a_paired_machine_that_never_reports_goes_offline_on_its_own(self):
        self.assertEqual(self.believed(minutes=1).state, LivenessState.ONLINE)
        self.assertEqual(self.believed(minutes=16).state,
                         LivenessState.OFFLINE)

    def test_an_unpaired_machine_has_no_liveness_to_report(self):
        self.device.revoke()

        self.assertEqual(self.believed(minutes=1).state, LivenessState.UNKNOWN)


class ClockSkewAdvisoryTests(HeartbeatTestCase):
    """Skew is a finding about a machine that is otherwise fine, so it never
    becomes the machine's state -- it travels beside it."""

    def test_a_large_skew_does_not_change_the_state(self):
        self.heartbeat({
            "device_time": wire_datetime(
                dj_timezone.now() + timedelta(minutes=20)
            ),
            "last_cycle": {"completed_at": wire_datetime(dj_timezone.now())},
        })

        liveness = self.believed(minutes=1)

        self.assertEqual(liveness.state, LivenessState.ONLINE)
        self.assertTrue(liveness.skew_is_advisory)
        self.assertIn("clock", liveness.skew_note.lower())

    def test_a_small_skew_is_not_worth_saying(self):
        self.heartbeat({
            "device_time": wire_datetime(
                dj_timezone.now() + timedelta(seconds=30)
            ),
            "last_cycle": {"completed_at": wire_datetime(dj_timezone.now())},
        })

        liveness = self.believed(minutes=1)

        self.assertFalse(liveness.skew_is_advisory)
        self.assertEqual(liveness.skew_note, "")


class StateTransitionTests(HeartbeatTestCase):
    """Only change is persisted. There is no heartbeat history table, and a
    machine that has been offline all weekend has one row saying so."""

    def transitions(self):
        return list(
            AgentDeviceStateTransition.objects
            .filter(device=self.device).order_by("at", "pk")
            .values_list("from_state", "to_state")
        )

    def sweep(self, **offset):
        return sweep_liveness(dj_timezone.now() + timedelta(**offset))

    def test_the_first_heartbeat_logs_the_machine_coming_online(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        self.assertEqual(
            self.transitions(),
            [(LivenessState.UNKNOWN, LivenessState.ONLINE)],
        )

    def test_repeated_heartbeats_saying_the_same_thing_log_nothing_more(self):
        for _beat in range(4):
            self.heartbeat({"last_cycle": {
                "completed_at": wire_datetime(dj_timezone.now()),
            }})

        self.assertEqual(len(self.transitions()), 1)

    def test_going_quiet_is_logged_by_the_sweep_alone(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        self.assertEqual(self.sweep(minutes=11), 1)
        self.assertEqual(self.sweep(minutes=16), 1)

        self.assertEqual(self.transitions(), [
            (LivenessState.UNKNOWN, LivenessState.ONLINE),
            (LivenessState.ONLINE, LivenessState.DEGRADED),
            (LivenessState.DEGRADED, LivenessState.OFFLINE),
        ])

    def test_a_fleet_that_has_not_moved_costs_no_rows(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})
        before = len(self.transitions())

        self.assertEqual(self.sweep(minutes=1), 0)
        self.assertEqual(self.sweep(minutes=2), 0)

        self.assertEqual(len(self.transitions()), before)

    def test_coming_back_is_logged_too(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})
        self.sweep(minutes=16)

        back = dj_timezone.now() + timedelta(minutes=17)
        with at_time(back):
            self.heartbeat({"last_cycle": {"completed_at": wire_datetime(back)}})

        self.assertEqual(self.transitions()[-1],
                         (LivenessState.OFFLINE, LivenessState.ONLINE))

    def test_revoking_a_machine_closes_its_history(self):
        self.heartbeat({"last_cycle": {
            "completed_at": wire_datetime(dj_timezone.now()),
        }})

        self.device.revoke()

        self.assertEqual(self.transitions()[-1],
                         (LivenessState.ONLINE, LivenessState.UNKNOWN))

    def test_the_sweep_leaves_unpaired_machines_alone(self):
        AgentDevice.objects.create(name="Never installed")

        self.sweep(minutes=30)

        self.assertFalse(
            AgentDeviceStateTransition.objects
            .exclude(device=self.device).exists()
        )


class HeartbeatBumpsLastSeenTests(HeartbeatTestCase):
    def test_a_heartbeat_counts_as_the_device_calling_in(self):
        create_station_link()

        self.heartbeat({})

        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_seen_at)
