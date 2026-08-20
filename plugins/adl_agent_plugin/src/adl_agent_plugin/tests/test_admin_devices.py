"""
The administrator's half of the pairing story, driven through the Wagtail
admin the same way a person drives it: create a device, read the code off
the edit page, rotate it, revoke it.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from adl_agent_plugin.models import AgentDevice

from .helpers import PAIR_URL, clear_pair_throttle

# Rendering a Wagtail admin page asks the staticfiles storage for hashed
# asset names, and the test runner never runs collectstatic -- so the
# manifest these pages resolve against does not exist. Serving static files
# unhashed is a property of the test process, not of the plugin.
UNHASHED_STATICFILES = override_settings(STORAGES={
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
})


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
            reverse("agent_devices:add"), {"name": name, "description": ""},
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
