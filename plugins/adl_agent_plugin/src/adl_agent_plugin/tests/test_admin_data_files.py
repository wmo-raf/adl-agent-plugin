"""
A failed file, seen from HQ.

A bad file has to be a diagnosable event rather than silent loss (story 20),
and "diagnosable" means an operator in another country can open the admin,
see which file failed, and read why -- without a shell, a log search or a
phone call. These tests drive the listing the way that operator does.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_timezone

from adl.core.registries import plugin_registry
from adl_agent_plugin.models import AgentFileStatus

from .helpers import (
    TemporaryMediaRoot,
    UNHASHED_STATICFILES,
    celsius,
    create_parameter,
    create_station_link,
    csv_file,
    decoding_connection,
    map_on_connection,
    observation_time,
    stage_file,
)

LIST_URL = reverse("wagtailsnippets_adl_agent_plugin_agentstationdatafile:list")


def inspect_url(data_file):
    return reverse(
        "wagtailsnippets_adl_agent_plugin_agentstationdatafile:inspect",
        args=[data_file.pk],
    )


@UNHASHED_STATICFILES
class AgentDataFileAdminTests(TemporaryMediaRoot, TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="hq", email="hq@example.com", password="hq-password",
        )
        self.client.force_login(self.admin)

        self.connection = decoding_connection()
        self.link = create_station_link(self.connection)
        self.link.start_date = dj_timezone.now() - timedelta(hours=6)
        self.link.save()
        map_on_connection(
            self.connection, create_parameter(name="air_temperature"),
            celsius(), "AirTemp",
        )

        self.good = stage_file(
            self.link, "AWS_01.csv", csv_file((observation_time(10), 21.5)),
        )
        self.bad = stage_file(self.link, "broken.csv", b"nothing,useful\n1,2\n")

        plugin_registry.get("adl_agent_plugin").process_station(self.link)

    def test_the_listing_shows_which_files_failed_and_why(self):
        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        self.assertIn("broken.csv", body)
        self.assertIn("Failed to process", body)
        self.assertIn("timestamp", body)

    def test_an_operator_can_ask_for_only_the_failures(self):
        response = self.client.get(LIST_URL, {"status": AgentFileStatus.FAILED})

        body = response.content.decode()
        self.assertIn("broken.csv", body)
        self.assertNotIn("AWS_01.csv", body)

    def test_the_whole_error_is_on_the_file_s_own_page(self):
        response = self.client.get(inspect_url(self.bad))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Datetime column &#x27;timestamp&#x27; not found",
            response.content.decode(),
        )
