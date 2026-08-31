"""
The administrator's half of the pairing story, driven through the Wagtail
admin the same way a person drives it: create a device, read the code off
the edit page, rotate it, revoke it.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from adl_agent_plugin.models import AgentDevice

from .helpers import (
    PAIR_URL,
    UNHASHED_STATICFILES,
    clear_pair_throttle,
    create_connection,
    create_device,
    create_station_link,
)


@UNHASHED_STATICFILES
class AgentDeviceAdminTests(TestCase):
    def setUp(self):
        clear_pair_throttle()
        self.addCleanup(clear_pair_throttle)

        self.admin = get_user_model().objects.create_superuser(
            username="hq", email="hq@example.com", password="hq-password",
        )
        self.client.force_login(self.admin)

    def create_device(self, name="Dar es Salaam server"):
        response = self.client.post(
            reverse("agent_devices:add"),
            {"name": name, "description": "", "check_interval_minutes": 5,
             "dated_folder_window_hours": 48},
        )
        self.assertEqual(response.status_code, 302, response.content)
        return AgentDevice.objects.get(name=name)

    def edit_page(self, device):
        return self.client.get(reverse("agent_devices:edit", args=[device.pk]))

    def test_creating_a_device_in_the_admin_shows_its_pairing_code(self):
        device = self.create_device()

        response = self.edit_page(device)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(device.pairing_code)
        self.assertContains(response, device.pairing_code)

    def test_the_listing_shows_the_device(self):
        device = self.create_device()

        response = self.client.get(reverse("agent_devices:index"))

        self.assertContains(response, device.name)

    def test_issuing_a_new_code_rotates_it_and_shows_the_new_one(self):
        device = self.create_device()
        original = device.pairing_code

        response = self.client.post(
            reverse("agent_device_issue_pairing_code", args=[device.pk]),
            follow=True,
        )

        device.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(device.pairing_code, original)
        self.assertContains(response, device.pairing_code)
        self.assertNotContains(response, original)

    def test_revoking_from_the_admin_cuts_the_device_off(self):
        device = self.create_device()
        token = self.client.post(
            PAIR_URL,
            {"pairing_code": device.pairing_code},
            content_type="application/json",
        ).json()["token"]

        response = self.client.post(
            reverse("agent_device_revoke", args=[device.pk]), follow=True,
        )

        device.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(device.status, AgentDevice.STATUS_REVOKED)
        self.assertEqual(
            self.client.get(
                reverse("plugins:adl_agent:device_me"),
                HTTP_AUTHORIZATION=f"Bearer {token}",
            ).status_code,
            401,
        )

    def test_the_confirmation_pages_render_before_acting(self):
        device = self.create_device()
        original = device.pairing_code

        for url_name in ["agent_device_issue_pairing_code", "agent_device_revoke"]:
            with self.subTest(action=url_name):
                response = self.client.get(reverse(url_name, args=[device.pk]))

                self.assertEqual(response.status_code, 200)

        device.refresh_from_db()
        self.assertEqual(device.pairing_code, original)
        self.assertIsNone(device.revoked_at)


@UNHASHED_STATICFILES
class AgentDeviceInspectTests(TestCase):
    """The device info page: what the machine last said, readable.

    A page of its own rather than panels on the edit form, reached from the
    device listing's Inspect button and from a connection row's Device info
    button -- and rendered for a person, so bytes are sizes, seconds are
    durations, and station links are station names.
    """

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="hq", email="hq@example.com", password="hq-password",
        )
        self.client.force_login(self.admin)
        self.device = create_device(name="Dodoma server")

    def inspect_page(self):
        return self.client.get(
            reverse("agent_devices:inspect", args=[self.device.pk])
        )

    def test_the_page_renders_what_the_machine_reported(self):
        link = create_station_link(
            create_connection(self.device, name="Vendor A")
        )
        self.device.heartbeat_details = {
            "uptime_seconds": 90000,  # a day and an hour
            "backlog_count": 3,
            "links": [{
                "station_link_id": link.pk,
                "scanned": 4, "offered": 2, "uploaded": 2, "failed": 0,
                "error": None,
            }],
            "volumes": [{
                "volume": "C:\\",
                "free_bytes": 5 * 1024 ** 3,
                "total_bytes": 10 * 1024 ** 3,
            }],
            "dropped_passes": None,
        }
        self.device.save(update_fields=["heartbeat_details"])

        response = self.inspect_page()

        self.assertEqual(response.status_code, 200)
        # Bytes as sizes, in one sentence per volume. The spaces inside a
        # size are non-breaking: Django's filesizeformat refuses to let "10.0"
        # and "GB" part ways at a line end.
        self.assertContains(response, "5.0\xa0GB free of 10.0\xa0GB")
        # Uptime as a duration, not a count of seconds.
        self.assertContains(response, "1\xa0day")
        self.assertNotContains(response, "90000")
        # The station by name, not by ADL's internal link id.
        self.assertContains(response, str(link.station))

    def test_a_machine_that_never_reported_still_gets_a_page(self):
        response = self.inspect_page()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Never")

    def test_the_listing_offers_the_inspect_page(self):
        response = self.client.get(reverse("agent_devices:index"))

        self.assertContains(
            response, reverse("agent_devices:inspect", args=[self.device.pk])
        )

    def test_the_edit_form_no_longer_carries_the_readout(self):
        response = self.client.get(
            reverse("agent_devices:edit", args=[self.device.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Last heartbeat")

    def test_a_connection_links_to_its_device_info(self):
        connection = create_connection(self.device, name="Vendor A")

        links = connection.get_extra_model_admin_links()

        self.assertEqual(len(links), 1)
        self.assertEqual(
            links[0]["url"],
            reverse("agent_devices:inspect", args=[self.device.pk]),
        )
        self.assertEqual(links[0]["label"], "Device info")


@UNHASHED_STATICFILES
class AgentDeviceAdminPermissionTests(TestCase):
    """A logged-in admin user is not automatically allowed to mint or
    destroy device credentials -- both actions need change permission on
    the device itself."""

    def setUp(self):
        self.device = AgentDevice.objects.create(name="Kampala server")

        user = get_user_model().objects.create_user(
            username="viewer", email="viewer@example.com", password="viewer-pass",
        )
        user.user_permissions.add(
            Permission.objects.get(codename="access_admin")
        )
        self.client.force_login(user)

    def test_actions_are_refused_without_change_permission(self):
        for url_name in ["agent_device_issue_pairing_code", "agent_device_revoke"]:
            with self.subTest(action=url_name):
                response = self.client.post(
                    reverse(url_name, args=[self.device.pk])
                )

                self.assertIn(response.status_code, (302, 403))

        self.device.refresh_from_db()
        self.assertIsNone(self.device.revoked_at)


@UNHASHED_STATICFILES
class AgentDeviceDeletionTests(TestCase):
    """Deleting a device is not how a machine is taken out of service.

    Revoking is: it cuts the machine off and leaves its connections, station
    links and folder configuration in place. Delete is for a device that was
    never wired up, so a device that *is* wired up refuses -- losing a
    country's folder configuration to a stray click is not a recoverable
    mistake.
    """

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="hq", email="hq@example.com", password="hq-password",
        )
        self.client.force_login(self.admin)
        self.device = create_device()

    def delete_url(self):
        return reverse("agent_devices:delete", args=[self.device.pk])

    def test_an_unused_device_can_be_deleted(self):
        self.assertContains(self.client.get(self.delete_url()), "Yes, delete")

        response = self.client.post(self.delete_url())

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AgentDevice.objects.filter(pk=self.device.pk).exists())

    def test_a_device_with_connections_is_not_offered_for_deletion(self):
        create_connection(self.device, name="Vendor A")

        response = self.client.get(self.delete_url())

        self.assertNotContains(response, "Yes, delete", status_code=200)

    def test_a_device_with_connections_survives_the_attempt_anyway(self):
        create_connection(self.device, name="Vendor A")

        self.client.post(self.delete_url(), follow=True)

        self.assertTrue(AgentDevice.objects.filter(pk=self.device.pk).exists())
