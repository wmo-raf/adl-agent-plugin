"""
A fortnight of a station's collection, seen from HQ.

The fleet listing answers "which machines are in trouble" and the liveness
panel answers "what is wrong with this one". This is the next question, which
ADL could not answer at all: what has this station actually been doing, and --
the fleet-wide one nobody could ask before these rows existed -- which passes
failed this week, anywhere.

Every row below is put there by a heartbeat, through the real endpoint, for
the same reason the liveness tests post beats rather than writing states.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_timezone

from adl.monitoring.models import StationLinkActivityLog
from adl_agent_plugin.models import AgentCyclePass, AgentCyclePassOutcome

from .helpers import (
    UNHASHED_STATICFILES,
    bearer,
    create_connection,
    create_station_link,
    paired_device,
    wire_datetime,
)

HEARTBEAT_URL = reverse("plugins:adl_agent:heartbeat")
LIST_URL = reverse("wagtailsnippets_adl_agent_plugin_agentcyclepass:list")


@UNHASHED_STATICFILES
class AgentCycleAdminTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="hq", email="hq@example.com", password="hq-password",
        )
        self.client.force_login(self.admin)

        self.device, self.token = paired_device(name="Bobo-Dioulasso server")
        self.connection = create_connection(device=self.device)
        self.banfora = create_station_link(connection=self.connection)
        self.bobo = create_station_link(connection=self.connection)

        self.beat([
            self.a_pass(self.banfora, uploaded=5, failed=0),
            self.a_pass(
                self.bobo, uploaded=0, failed=3,
                missing=[{
                    "name": "BOBO_20260821.DAT",
                    "outcome": "unmatched",
                }],
            ),
        ])

    def a_pass(self, station_link, uploaded, failed, missing=None):
        return {
            "at": wire_datetime(dj_timezone.now()),
            "seconds": 2.0,
            "unit": "C:\\VendorData\\%s" % station_link.pk,
            "trigger": "scheduled",
            "completed": True,
            "folders": 1,
            "stations": [{
                "station_link_id": station_link.pk,
                "scanned": 12,
                "uploaded": uploaded,
                "failed": failed,
            }],
            "missing": missing or [],
        }

    def beat(self, passes):
        return self.client.post(
            HEARTBEAT_URL,
            data={"completed_passes": passes},
            content_type="application/json",
            **bearer(self.token),
        )

    def test_the_listing_shows_what_did_not_arrive(self):
        """The column an operator scans for the answer to "why has this
        station gone quiet"."""
        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BOBO_20260821.DAT")
        self.assertContains(response, "Files failed")
        self.assertContains(response, "Delivered")

    def test_the_listing_narrows_to_one_station(self):
        response = self.client.get(LIST_URL, {"station_link": self.bobo.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BOBO_20260821.DAT")
        self.assertEqual(
            [row.station_link_id for row in response.context["object_list"]],
            [self.bobo.pk],
        )

    def test_the_listing_narrows_to_the_passes_that_went_wrong(self):
        """The fleet-wide question: every failed pass this week, across every
        device."""
        response = self.client.get(
            LIST_URL, {"outcome": AgentCyclePassOutcome.FAILED},
        )

        self.assertEqual(
            [row.station_link_id for row in response.context["object_list"]],
            [self.bobo.pk],
        )

    def test_the_listing_narrows_to_one_machine(self):
        elsewhere, _token = paired_device(name="Ouagadougou server")

        response = self.client.get(LIST_URL, {"device": self.device.pk})

        self.assertEqual(len(response.context["object_list"]), 2)

        response = self.client.get(LIST_URL, {"device": elsewhere.pk})

        self.assertEqual(len(response.context["object_list"]), 0)

    def test_the_device_page_shows_its_recent_cycles(self):
        response = self.client.get(
            reverse("agent_devices:edit", args=[self.device.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent cycles")
        self.assertContains(response, "BOBO_20260821.DAT")
        self.assertContains(response, "%s?device=%s" % (LIST_URL, self.device.pk))

    def test_the_station_page_shows_its_own_recent_cycles(self):
        response = self.client.get(
            reverse(
                "agentstationlink:edit",
                args=[self.bobo.pk],
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent cycles")
        self.assertContains(
            response, "%s?station_link=%s" % (LIST_URL, self.bobo.pk),
        )

    def test_the_monitoring_activity_list_gains_nothing_from_this(self):
        """These rows describe the half of the ingestion path that runs in the
        country, which begins before a file ADL holds exists. Folding them
        into core's activity log would swamp it with rows carrying no
        records_count and weld this plugin's retention to core's.
        """
        self.assertEqual(AgentCyclePass.objects.count(), 2)
        self.assertEqual(StationLinkActivityLog.objects.count(), 0)
