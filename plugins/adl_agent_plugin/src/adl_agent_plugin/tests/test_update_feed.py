"""
``GET /api/agent/v1/update`` -- the feed a country's fleet updates from.

Two promises are under test here, and they are the two the whole update story
rests on.

**A machine is told what to run by its own ADL** (story 28). The instance the
agent already talks to is the only host its machines can reach, so the feed
has to be complete on its own: a version, a package, and the digest that
package must have.

**A pinned machine is not told a newer release exists** (story 29). The pin
is enforced here rather than trusted to the agent, and enforced again on the
package endpoint -- a feed that declined to mention a release while still
serving it to anyone who guessed the URL would be a pin that holds only as
long as the agent asks nicely.
"""

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.utils import IntegrityError
from django.test import TestCase

from adl_agent_plugin.models import (
    AgentRelease,
    AgentReleaseArtifact,
    AgentReleaseArtifactKind,
)

from .helpers import (
    UPDATE_URL,
    TemporaryMediaRoot,
    bearer,
    create_release,
    paired_device,
    sha256_of,
    update_package_url,
)


class UpdateFeedTestCase(TemporaryMediaRoot, TestCase):
    def setUp(self):
        self.device, self.token = paired_device(name="Nairobi vendor server")

    def offer(self, tier=None, token=None):
        url = UPDATE_URL if tier is None else f"{UPDATE_URL}?tier={tier}"

        return self.client.get(url, **bearer(token or self.token))

    def fetch(self, version, kind, token=None):
        return self.client.get(
            update_package_url(version, kind), **bearer(token or self.token),
        )


class UpdateFeedAuthTests(UpdateFeedTestCase):
    def test_the_feed_needs_a_token(self):
        self.assertEqual(self.client.get(UPDATE_URL).status_code, 401)

    def test_a_revoked_device_is_told_nothing(self):
        create_release("0.2.0")
        self.device.revoke()

        self.assertEqual(self.offer().status_code, 401)

    def test_the_package_needs_a_token_too(self):
        create_release("0.2.0")

        response = self.client.get(
            update_package_url("0.2.0", AgentReleaseArtifactKind.MSI),
        )

        self.assertEqual(response.status_code, 401)


class UpdateOfferTests(UpdateFeedTestCase):
    def test_an_instance_holding_nothing_says_so_rather_than_failing(self):
        response = self.offer()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["version"])
        self.assertIn("no published agent release", response.json()["reason"])

    def test_the_newest_published_release_is_offered(self):
        create_release("0.1.0")
        create_release("0.2.0")

        body = self.offer().json()

        self.assertEqual(body["version"], "0.2.0")
        self.assertFalse(body["pinned"])

    def test_ten_is_newer_than_nine(self):
        # Versions are ordered as numbers, not as strings. A fleet that
        # stopped updating at 0.9.0 because "0.10.0" sorts below it would be
        # a fleet nobody could explain.
        create_release("0.9.0")
        create_release("0.10.0")

        self.assertEqual(self.offer().json()["version"], "0.10.0")

    def test_a_staged_release_is_not_offered(self):
        create_release("0.1.0")
        create_release("0.2.0", published=False)

        # Mirroring a release brings it within reach; publishing it is this
        # country deciding its machines may have it.
        self.assertEqual(self.offer().json()["version"], "0.1.0")

    def test_the_offer_carries_everything_needed_to_fetch_and_check_it(self):
        content = b"a self-contained publish, notionally"
        create_release("0.2.0", packages={AgentReleaseArtifactKind.MSI: content})

        artifact = self.offer().json()["artifact"]

        self.assertEqual(artifact["kind"], "msi")
        self.assertEqual(artifact["sha256"], sha256_of(content))
        self.assertEqual(artifact["size"], len(content))

        # Relative to the agent API's own base. An absolute URL would let the
        # body of a response decide which host a country server downloads an
        # executable from.
        self.assertEqual(artifact["path"], "update/0.2.0/msi/")
        self.assertNotIn("://", artifact["path"])

    def test_each_tier_is_offered_its_own_package(self):
        create_release("0.2.0", packages={
            AgentReleaseArtifactKind.MSI: b"MSI",
            AgentReleaseArtifactKind.VELOPACK_FULL: b"NUPKG",
        })

        self.assertEqual(self.offer(tier="service").json()["artifact"]["kind"], "msi")
        self.assertEqual(self.offer(tier="user").json()["artifact"]["kind"], "velopack_full")

    def test_a_tier_with_no_package_is_named_rather_than_left_silent(self):
        create_release("0.2.0", packages={AgentReleaseArtifactKind.MSI: b"MSI"})

        body = self.offer(tier="user").json()

        # Not "up to date": a release built for one tier only leaves half a
        # fleet never updating, which is worth being loud about.
        self.assertEqual(body["version"], "0.2.0")
        self.assertIsNone(body["artifact"])
        self.assertIn("user", body["reason"])

    def test_an_install_tier_this_ADL_does_not_know_is_refused(self):
        response = self.offer(tier="mainframe")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_tier")

    def test_asking_counts_as_being_seen(self):
        self.offer()

        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_seen_at)


class VersionPinTests(UpdateFeedTestCase):
    def setUp(self):
        super().setUp()
        create_release("0.1.0")
        create_release("0.2.0")

    def pin(self, version):
        self.device.pinned_version = version
        self.device.save()

    def test_a_pinned_device_sees_only_its_pinned_version(self):
        self.pin("0.1.0")

        body = self.offer().json()

        self.assertEqual(body["version"], "0.1.0")
        self.assertTrue(body["pinned"])

    def test_a_pinned_device_cannot_fetch_the_newer_package_either(self):
        self.pin("0.1.0")

        # The URL is guessable, and the pin has to hold anyway.
        response = self.fetch("0.2.0", AgentReleaseArtifactKind.MSI)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_offered")

    def test_a_pin_naming_a_release_this_instance_lacks_offers_nothing(self):
        self.pin("9.9.9")

        body = self.offer().json()

        self.assertIsNone(body["version"])
        self.assertTrue(body["pinned"])
        self.assertIn("9.9.9", body["reason"])

    def test_unpinning_lets_the_device_move_again(self):
        self.pin("0.1.0")
        self.assertEqual(self.offer().json()["version"], "0.1.0")

        self.pin("")

        self.assertEqual(self.offer().json()["version"], "0.2.0")

    def test_one_devices_pin_is_not_anothers(self):
        self.pin("0.1.0")

        _other, other_token = paired_device(name="Mombasa vendor server")

        self.assertEqual(self.offer(token=other_token).json()["version"], "0.2.0")


class PinValidationTests(TestCase):
    def test_a_pin_that_is_not_a_version_is_refused_where_it_is_typed(self):
        device, _token = paired_device(name="Nairobi vendor server")

        device.pinned_version = "v0.1.0"

        # A pin is matched against a release version exactly, so this one
        # would match nothing this instance will ever hold -- and the machine
        # would sit frozen while the admin showed a pin that looked
        # deliberate.
        with self.assertRaises(ValidationError) as refused:
            device.full_clean()

        self.assertIn("pinned_version", refused.exception.message_dict)

    def test_no_pin_at_all_is_fine(self):
        device, _token = paired_device(name="Mombasa vendor server")

        device.pinned_version = ""
        device.full_clean()


class UpdatePackageTests(UpdateFeedTestCase):
    def test_the_offered_package_is_served_as_its_bytes(self):
        content = b"MSI BYTES, exactly these"
        create_release("0.2.0", packages={AgentReleaseArtifactKind.MSI: content})

        response = self.fetch("0.2.0", AgentReleaseArtifactKind.MSI)

        self.assertEqual(response.status_code, 200)

        served = b"".join(response.streaming_content)

        # Byte for byte, because the agent refuses anything whose digest does
        # not match what the feed stated.
        self.assertEqual(served, content)
        self.assertEqual(sha256_of(served), sha256_of(content))

    def test_a_caller_may_ask_for_the_bytes_it_is_about_to_be_sent(self):
        create_release("0.2.0", packages={AgentReleaseArtifactKind.MSI: b"MSI"})

        # Content negotiation runs before the view does, so an endpoint that
        # serves only a file still has to recognise the media type anyone
        # downloading one would ask for.
        response = self.client.get(
            update_package_url("0.2.0", AgentReleaseArtifactKind.MSI),
            HTTP_ACCEPT="application/octet-stream",
            **bearer(self.token),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"MSI")

    def test_a_staged_release_is_not_served(self):
        create_release("0.2.0", published=False)

        self.assertEqual(
            self.fetch("0.2.0", AgentReleaseArtifactKind.MSI).status_code, 404,
        )

    def test_a_version_this_instance_does_not_hold_is_not_served(self):
        create_release("0.2.0")

        self.assertEqual(
            self.fetch("0.3.0", AgentReleaseArtifactKind.MSI).status_code, 404,
        )

    def test_a_kind_the_release_has_no_package_for_is_not_served(self):
        create_release("0.2.0", packages={AgentReleaseArtifactKind.MSI: b"MSI"})

        self.assertEqual(
            self.fetch("0.2.0", AgentReleaseArtifactKind.VELOPACK_FULL).status_code, 404,
        )


class ReleasePackageTests(TemporaryMediaRoot, TestCase):
    """What an operator uploading a build is protected from.

    The digest an agent checks is computed here, from the bytes as stored --
    a hash copied from somewhere else would verify the wrong thing. What can
    be pasted in is the digest the build published, and that turns the upload
    into the one moment a truncated or tampered package can still be caught
    by a person.
    """

    def upload(self, content, expected=""):
        release = AgentRelease.objects.create(version="0.2.0")

        artifact = AgentReleaseArtifact(
            release=release,
            kind=AgentReleaseArtifactKind.MSI,
            expected_sha256=expected,
        )
        artifact.file.save("AdlAgent-0.2.0-x64.msi", ContentFile(content), save=False)

        return artifact

    def test_a_package_is_hashed_and_measured_when_it_is_stored(self):
        content = b"a self-contained publish, notionally"

        artifact = self.upload(content)
        artifact.save()

        artifact.refresh_from_db()

        self.assertEqual(artifact.sha256, sha256_of(content))
        self.assertEqual(artifact.size, len(content))

    def test_a_package_matching_the_digest_the_build_published_is_accepted(self):
        content = b"a self-contained publish, notionally"

        artifact = self.upload(content, expected=sha256_of(content).upper())
        artifact.full_clean()

        self.assertEqual(artifact.sha256, sha256_of(content))

    def test_a_package_that_is_not_the_one_that_digest_describes_is_refused(self):
        artifact = self.upload(b"half an installer", expected="a" * 64)

        with self.assertRaises(ValidationError) as refused:
            artifact.full_clean()

        self.assertIn("expected_sha256", refused.exception.message_dict)

    def test_a_release_holds_one_package_per_kind(self):
        first = self.upload(b"MSI")
        first.save()

        second = AgentReleaseArtifact(
            release=first.release, kind=AgentReleaseArtifactKind.MSI,
        )
        second.file.save("pkg.msi", ContentFile(b"MSI AGAIN"), save=False)

        # Two service-tier packages on one release is a release whose feed
        # answer depends on which row happened to be read first.
        with self.assertRaises(IntegrityError):
            second.save()

    def test_a_package_uploaded_through_a_form_can_be_saved(self):
        """The admin's own path: validate the upload, then store it.

        Both halves read the same handle, and this is the whole of "publish
        v2 to the feed" -- the demo the feature exists for. It is worth a
        test of its own because every other route into this model hands it a
        file that is already on storage, and would never notice.
        """
        release = AgentRelease.objects.create(version="0.3.0")

        artifact = AgentReleaseArtifact(
            release=release,
            kind=AgentReleaseArtifactKind.MSI,
            file=SimpleUploadedFile("AdlAgent-0.3.0-x64.msi", b"PRETEND MSI"),
        )

        artifact.full_clean()
        artifact.save()

        artifact.refresh_from_db()

        self.assertEqual(artifact.sha256, sha256_of(b"PRETEND MSI"))
        self.assertEqual(artifact.file.read(), b"PRETEND MSI")
