"""
``PATCH /api/agent/v1/station-links/<id>/config`` -- the app writing its tier.

Two promises are under test. The first is the tier split: the person looking
at the real files says where they are and how they are named, and nobody at
that end can move what a station's data *means*. The second is the conflict
rule -- last write wins, no 409s, and a ``config_version`` on every response
so the agent knows when to re-read.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone as dj_timezone

from adl_agent_plugin.models import AgentListingStrategy, AgentStationLink

from .helpers import (
    SYNC_URL,
    bearer,
    create_connection,
    create_device,
    create_station_link,
    paired_device,
    station_link_config_url,
)


class StationLinkConfigTestCase(TestCase):
    def setUp(self):
        self.device, self.token = paired_device()
        self.connection = create_connection(self.device)
        self.link = create_station_link(
            self.connection,
            local_folder_path="C:\\vendor\\data",
            file_pattern="*.dat",
        )

    def patch(self, payload, link=None, token=None):
        return self.client.patch(
            station_link_config_url(link or self.link),
            payload,
            content_type="application/json",
            **bearer(token or self.token),
        )

    def sync(self):
        return self.client.get(SYNC_URL, **bearer(self.token)).json()


class ConfigAuthTests(StationLinkConfigTestCase):
    def test_writing_needs_a_token(self):
        response = self.client.patch(
            station_link_config_url(self.link),
            {"file_pattern": "*.csv"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.link.refresh_from_db()
        self.assertEqual(self.link.file_pattern, "*.dat")

    def test_a_revoked_device_cannot_write(self):
        self.device.revoke()

        self.assertEqual(self.patch({"file_pattern": "*.csv"}).status_code, 401)

    def test_another_devices_station_link_is_not_found(self):
        theirs = create_station_link(create_connection(create_device()))

        response = self.patch({"file_pattern": "*.csv"}, link=theirs)

        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.file_pattern, "*.dat")

    def test_writing_counts_as_being_seen(self):
        before = dj_timezone.now()

        self.patch({"file_pattern": "*.csv"})

        self.device.refresh_from_db()
        self.assertGreaterEqual(self.device.last_seen_at, before)


class ConfigWriteTests(StationLinkConfigTestCase):
    def test_the_app_can_move_a_folder(self):
        response = self.patch({"local_folder_path": "E:\\vendor\\moved"})

        self.assertEqual(response.status_code, 200)
        self.link.refresh_from_db()
        self.assertEqual(self.link.local_folder_path, "E:\\vendor\\moved")

    def test_the_response_shows_the_configuration_that_now_stands(self):
        body = self.patch({"file_pattern": "GAR_*.csv"}).json()

        self.assertEqual(body["config"]["file_pattern"], "GAR_*.csv")
        self.assertEqual(
            set(body["config"]), set(AgentStationLink.APP_EDITABLE_FIELDS)
        )

    def test_a_partial_write_leaves_the_rest_of_the_tier_alone(self):
        self.patch({"stability_window_seconds": 120})

        self.link.refresh_from_db()
        self.assertEqual(self.link.stability_window_seconds, 120)
        self.assertEqual(self.link.file_pattern, "*.dat")
        self.assertEqual(self.link.local_folder_path, "C:\\vendor\\data")

    def test_the_whole_folder_story_can_be_written_in_one_call(self):
        response = self.patch({
            "local_folder_path": "D:\\aws",
            "file_pattern": "AWS_*.txt",
            "dir_structured_by_date": True,
            "date_granularity": "day",
            "month_dir_format": "n",
        })

        self.assertEqual(response.status_code, 200)
        self.link.refresh_from_db()
        self.assertTrue(self.link.dir_structured_by_date)
        self.assertEqual(self.link.date_granularity, "day")
        self.assertEqual(self.link.month_dir_format, "n")

    def test_a_link_can_be_switched_to_direct_fetch(self):
        response = self.patch({
            "listing_strategy": AgentListingStrategy.DIRECT_FETCH,
            "direct_fetch_prefix": "STATION_001_",
            "direct_fetch_interval_minutes": 10,
            "direct_fetch_datetime_format": "yyyyMMddHHmmss",
            "direct_fetch_file_extension": ".txt",
        })

        self.assertEqual(response.status_code, 200)
        self.link.refresh_from_db()
        self.assertEqual(
            self.link.listing_strategy, AgentListingStrategy.DIRECT_FETCH
        )
        self.assertEqual(self.link.direct_fetch_prefix, "STATION_001_")


class ConfigTierTests(StationLinkConfigTestCase):
    def assert_refused(self, response, field):
        self.assertEqual(response.status_code, 400)
        self.assertIn(field, response.json()["fields"])

    def test_the_collection_start_date_is_not_the_apps_to_move(self):
        start_date = dj_timezone.now() - timedelta(days=5)
        self.link.start_date = start_date
        self.link.save()

        response = self.patch({"start_date": dj_timezone.now().isoformat()})

        self.assert_refused(response, "start_date")
        self.link.refresh_from_db()
        self.assertEqual(self.link.start_date, start_date)

    def test_the_app_cannot_switch_a_station_off(self):
        response = self.patch({"enabled": False})

        self.assert_refused(response, "enabled")
        self.link.refresh_from_db()
        self.assertTrue(self.link.enabled)

    def test_the_app_cannot_relink_a_station(self):
        self.assert_refused(self.patch({"station": 1}), "station")

    def test_the_app_cannot_move_a_link_to_another_connection(self):
        self.assert_refused(
            self.patch({"network_connection": 1}), "network_connection"
        )

    def test_a_field_that_does_not_exist_is_refused_rather_than_ignored(self):
        # Silently dropping a typo is how a machine ends up configured with
        # something nobody wrote.
        self.assert_refused(self.patch({"flie_pattern": "*.csv"}), "flie_pattern")

    def test_one_bad_field_refuses_the_whole_write(self):
        response = self.patch({"file_pattern": "*.csv", "enabled": False})

        self.assertEqual(response.status_code, 400)
        self.link.refresh_from_db()
        self.assertEqual(self.link.file_pattern, "*.dat")

    def test_a_body_that_is_not_an_object_is_refused(self):
        response = self.patch(["file_pattern"])

        self.assertEqual(response.status_code, 400)


class ConfigValidationTests(StationLinkConfigTestCase):
    def test_a_folder_path_may_not_be_emptied(self):
        response = self.patch({"local_folder_path": ""})

        self.assertEqual(response.status_code, 400)
        self.assertIn("local_folder_path", response.json()["errors"])
        self.link.refresh_from_db()
        self.assertEqual(self.link.local_folder_path, "C:\\vendor\\data")

    def test_direct_fetch_without_the_means_to_build_a_name_is_refused(self):
        response = self.patch(
            {"listing_strategy": AgentListingStrategy.DIRECT_FETCH}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("direct_fetch_prefix", response.json()["errors"])

    def test_enumerating_without_a_pattern_is_refused(self):
        response = self.patch({"file_pattern": ""})

        self.assertEqual(response.status_code, 400)
        self.assertIn("file_pattern", response.json()["errors"])

    def test_a_stability_window_must_be_a_number(self):
        response = self.patch({"stability_window_seconds": "soon"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("stability_window_seconds", response.json()["errors"])


class ConfigVersionTests(StationLinkConfigTestCase):
    def test_every_write_answers_with_the_version_that_now_stands(self):
        before = self.sync()["config_version"]

        body = self.patch({"file_pattern": "*.csv"}).json()

        self.assertGreater(body["config_version"], before)
        self.assertEqual(self.sync()["config_version"], body["config_version"])

    def test_a_refused_write_does_not_move_the_version(self):
        before = self.sync()["config_version"]

        self.patch({"enabled": False})

        self.assertEqual(self.sync()["config_version"], before)

    def test_the_last_write_wins_and_nothing_is_ever_a_conflict(self):
        first = self.patch({"file_pattern": "*.csv"})
        second = self.patch({"file_pattern": "*.txt"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.link.refresh_from_db()
        self.assertEqual(self.link.file_pattern, "*.txt")

    def test_an_admin_edit_between_two_writes_is_simply_overwritten(self):
        # Shared tier, last-write-wins: the admin moved the folder, then the
        # technician moved it again. No 409, no merge -- the technician's
        # value stands, and the version tells the agent to re-read.
        self.link.local_folder_path = "F:\\admin\\choice"
        self.link.save()

        body = self.patch({"local_folder_path": "G:\\technician\\choice"}).json()

        self.link.refresh_from_db()
        self.assertEqual(self.link.local_folder_path, "G:\\technician\\choice")
        self.assertEqual(body["config"]["local_folder_path"], "G:\\technician\\choice")
