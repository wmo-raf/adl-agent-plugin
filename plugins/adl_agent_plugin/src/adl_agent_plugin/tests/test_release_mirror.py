"""
Mirroring releases from upstream into one country's instance.

ADL is one deployment per country, and an agent can only be updated from the
instance it is paired with. Without a mirror, publishing a version would mean
an operator in every NMHS uploading the same file by hand -- and a fleet that
is current only where somebody had time.

The tests drive it the way it runs: a real upstream on a loopback port
serving a real index and real bytes, and the mirror pulling from it. Two
rules are the point of the whole module and each has its own tests -- what
arrives is **staged, never published**, and a package is stored only if it
hashes to what the index promised.
"""

from django.test import TestCase, override_settings

from adl_agent_plugin.mirror import mirror_releases
from adl_agent_plugin.models import (
    AgentRelease,
    AgentReleaseArtifactKind,
    AgentReleaseSource,
)

from .helpers import (
    UPDATE_URL,
    TemporaryMediaRoot,
    UpstreamReleaseHost,
    bearer,
    create_release,
    paired_device,
    sha256_of,
)


class MirrorTestCase(TemporaryMediaRoot, TestCase):
    def setUp(self):
        self.upstream = UpstreamReleaseHost()
        self.addCleanup(self.upstream.close)

    def mirror(self, **kwargs):
        return mirror_releases(url=self.upstream.index_url, **kwargs)


class MirrorTests(MirrorTestCase):
    def test_a_new_release_is_pulled_in_with_its_package(self):
        content = b"the 0.2.0 service-tier package"
        self.upstream.publish("0.2.0", packages={"msi": content})

        result = self.mirror()

        self.assertEqual(result["mirrored"], ["0.2.0"])

        release = AgentRelease.objects.get(version="0.2.0")

        self.assertEqual(release.source, AgentReleaseSource.MIRRORED)
        self.assertEqual(release.notes, "Agent 0.2.0")

        artifact = release.artifact_for(AgentReleaseArtifactKind.MSI)

        # The hash is computed from the bytes as stored, and it agrees with
        # what upstream said -- which is the only claim being made about them.
        self.assertEqual(artifact.sha256, sha256_of(content))
        self.assertEqual(artifact.size, len(content))
        self.assertEqual(artifact.file.read(), content)

    def test_what_arrives_is_staged_rather_than_published(self):
        self.upstream.publish("0.2.0")

        self.mirror()

        # WMO decides what exists; this country decides when its machines
        # take it. A release that published itself on arrival would take that
        # decision away from every NMHS at once.
        self.assertFalse(AgentRelease.objects.get(version="0.2.0").is_published)

    def test_a_release_already_held_is_left_exactly_as_it_is(self):
        create_release("0.2.0", published=True)
        self.upstream.publish("0.2.0")

        result = self.mirror()

        self.assertEqual(result["skipped"], ["0.2.0"])

        # Neither re-fetched...
        self.assertEqual(self.upstream.hits.count("/0.2.0/msi.pkg"), 0)

        # ...nor un-published, which would undo an operator's decision every
        # night at twenty past one.
        self.assertTrue(AgentRelease.objects.get(version="0.2.0").is_published)

    def test_both_tiers_packages_come_across(self):
        self.upstream.publish("0.2.0", packages={
            "msi": b"MSI BYTES",
            "velopack_full": b"NUPKG BYTES",
            "velopack_setup": b"SETUP BYTES",
        })

        self.mirror()

        release = AgentRelease.objects.get(version="0.2.0")

        self.assertEqual(release.artifacts.count(), 3)

    def test_only_the_newest_few_are_pulled(self):
        for version in ["0.1.0", "0.2.0", "0.3.0", "0.4.0"]:
            self.upstream.publish(version)

        result = self.mirror(limit=2)

        # An instance meeting three years of history for the first time has
        # no reason to pull all of it.
        self.assertEqual(result["mirrored"], ["0.4.0", "0.3.0"])

    def test_ten_is_newer_than_nine_here_too(self):
        self.upstream.publish("0.9.0")
        self.upstream.publish("0.10.0")

        self.assertEqual(self.mirror(limit=1)["mirrored"], ["0.10.0"])


class MirrorVerificationTests(MirrorTestCase):
    def test_a_package_that_is_not_what_the_index_says_is_not_stored(self):
        self.upstream.publish(
            "0.2.0", packages={"msi": b"MSI BYTES"},
            states_sha256={"msi": "a" * 64},
        )

        result = self.mirror()

        self.assertEqual(result["failed"], ["0.2.0"])

        # Nothing at all: an instance that stored it would serve its whole
        # fleet something nobody has checked, and every agent would then
        # refuse it -- a fleet-wide outage discovered one country at a time.
        self.assertFalse(AgentRelease.objects.exists())

    def test_a_release_whose_second_package_is_corrupt_lands_as_nothing(self):
        self.upstream.publish(
            "0.2.0",
            packages={"msi": b"MSI BYTES", "velopack_full": b"NUPKG BYTES"},
            states_sha256={"velopack_full": "b" * 64},
        )

        self.mirror()

        # Not "a release with one package". That would be offered to the
        # service tier and be permanently unavailable to the other.
        self.assertFalse(AgentRelease.objects.exists())

    def test_a_package_from_somewhere_that_is_not_https_is_refused(self):
        self.upstream.publish("0.2.0")
        self.upstream.releases[0]["artifacts"][0]["url"] = (
            "http://releases.example.org/AdlAgent-0.2.0.msi"
        )

        result = self.mirror()

        self.assertEqual(result["failed"], ["0.2.0"])
        self.assertFalse(AgentRelease.objects.exists())

    def test_a_package_kind_this_ADL_does_not_know_is_ignored(self):
        self.upstream.publish("0.2.0", packages={"msi": b"MSI", "deb": b"DEB"})

        self.mirror()

        # A newer agent's packaging must not stop this instance mirroring the
        # tiers it does serve.
        release = AgentRelease.objects.get(version="0.2.0")

        self.assertEqual(
            [artifact.kind for artifact in release.artifacts.all()], ["msi"],
        )

    def test_a_release_with_no_package_this_ADL_can_serve_is_refused(self):
        self.upstream.publish("0.2.0", packages={"deb": b"DEB"})

        result = self.mirror()

        self.assertEqual(result["failed"], ["0.2.0"])
        self.assertFalse(AgentRelease.objects.exists())

    def test_a_version_that_is_not_three_numbers_is_refused(self):
        self.upstream.publish("2026-08-21")

        result = self.mirror()

        self.assertEqual(result["failed"], ["2026-08-21"])
        self.assertFalse(AgentRelease.objects.exists())

    @override_settings(ADL_AGENT_RELEASE_MAX_BYTES=8)
    def test_a_package_larger_than_this_instance_will_mirror_is_refused(self):
        self.upstream.publish("0.2.0", packages={"msi": b"far more than eight bytes"})

        result = self.mirror()

        self.assertEqual(result["failed"], ["0.2.0"])
        self.assertFalse(AgentRelease.objects.exists())


class MirrorFailureTests(MirrorTestCase):
    def test_an_unreachable_upstream_leaves_the_instance_serving_what_it_has(self):
        create_release("0.1.0")

        result = mirror_releases(url="http://127.0.0.1:1/index.json")

        self.assertEqual(result, {"mirrored": [], "skipped": [], "failed": []})
        self.assertTrue(AgentRelease.objects.filter(version="0.1.0").exists())

    def test_an_index_that_is_not_the_index_is_not_a_crash(self):
        self.upstream.index_body = b"<html>404 Not Found</html>"

        self.assertEqual(self.mirror()["mirrored"], [])

    @override_settings(ADL_AGENT_RELEASE_MIRROR_ENABLED=False)
    def test_an_instance_can_switch_mirroring_off_entirely(self):
        self.upstream.publish("0.2.0")

        self.assertEqual(self.mirror()["mirrored"], [])
        self.assertEqual(self.upstream.hits, [])
        self.assertFalse(AgentRelease.objects.exists())


class MirroredReleaseReachesTheFleetTests(MirrorTestCase):
    def test_a_mirrored_release_is_offered_once_an_operator_publishes_it(self):
        device, token = paired_device(name="Nairobi vendor server")

        self.upstream.publish("0.2.0", packages={"msi": b"MSI BYTES"})
        self.mirror()

        # Staged: the fleet is told nothing yet.
        self.assertIsNone(self.client.get(UPDATE_URL, **bearer(token)).json()["version"])

        release = AgentRelease.objects.get(version="0.2.0")
        release.is_published = True
        release.save()

        body = self.client.get(UPDATE_URL, **bearer(token)).json()

        self.assertEqual(body["version"], "0.2.0")
        self.assertEqual(body["artifact"]["sha256"], sha256_of(b"MSI BYTES"))
