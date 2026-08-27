"""
``POST /api/agent/v1/manifest`` -- propose and ack, once per cycle.

The agent is stateless by design: it never records what it has sent, it
simply offers what it can see and is told what to send. Everything below is
therefore written from the agent's side of that conversation -- offer a set
of candidate files, read back the list ADL asks for -- because that list is
the entire contract. What the ledger looks like underneath is ADL's business.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone as dj_timezone

from adl_agent_plugin.limits import (
    DEFAULT_CONCURRENT_UPLOADS,
    MANIFEST_PAGE_LIMIT,
    MOST_CONCURRENT_UPLOADS,
)
from adl_agent_plugin.models import AgentStationDataFile

from .helpers import (
    MANIFEST_URL,
    SYNC_URL,
    AgentClient,
    TemporaryMediaRoot,
    bearer,
    create_connection,
    create_device,
    create_station_link,
    manifest_entry,
    paired_device,
)


class ManifestTestCase(TemporaryMediaRoot, TestCase):
    def setUp(self):
        self.device, self.token = paired_device(name="Nairobi vendor server")
        self.connection = create_connection(self.device)
        self.link = create_station_link(self.connection)
        self.agent = AgentClient(self, self.token)


class ManifestAuthTests(ManifestTestCase):
    def test_manifest_needs_a_token(self):
        response = self.client.post(
            MANIFEST_URL, data={"files": []}, content_type="application/json"
        )

        self.assertEqual(response.status_code, 401)

    def test_revoked_device_is_locked_out(self):
        self.device.revoke()

        self.assertEqual(self.agent.manifest([]).status_code, 401)

    def test_offering_a_manifest_counts_as_being_seen(self):
        before = dj_timezone.now()

        self.agent.manifest([])

        self.device.refresh_from_db()
        self.assertGreaterEqual(self.device.last_seen_at, before)


class ManifestDiffTests(ManifestTestCase):
    def test_a_file_adl_has_never_seen_is_asked_for(self):
        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")

        self.assertEqual(
            self.agent.requested([entry]), [(self.link.pk, "GAR_0220.dat")]
        )

    def test_a_file_already_held_is_not_asked_for_again(self):
        content = b"one,two\n"
        self.agent.upload(self.link, "GAR_0220.dat", content)

        entry = manifest_entry(self.link, "GAR_0220.dat", content)

        self.assertEqual(self.agent.requested([entry]), [])

    def test_a_file_that_has_grown_is_asked_for_again(self):
        # The daily CSV a logger appends to: same name, more rows, so a
        # different hash -- and the whole file comes again.
        self.agent.upload(self.link, "GAR_0220.dat", b"one,two\n")

        grown = manifest_entry(self.link, "GAR_0220.dat", b"one,two\nthree,four\n")

        self.assertEqual(
            self.agent.requested([grown]), [(self.link.pk, "GAR_0220.dat")]
        )

    def test_a_file_rewritten_to_the_same_bytes_is_not_asked_for(self):
        # Vendor software that rewrites a file in place every cycle moves its
        # mtime and size stays put. Nothing changed, so nothing is sent.
        content = b"one,two\n"
        self.agent.upload(self.link, "GAR_0220.dat", content)

        touched = manifest_entry(
            self.link, "GAR_0220.dat", content,
            mtime=dj_timezone.now() + timedelta(minutes=5),
        )

        self.assertEqual(self.agent.requested([touched]), [])

    def test_a_file_whose_hash_was_cleared_is_offered_again(self):
        # How a re-process reaches a file whose bytes ADL has pruned: clear
        # the ledger hash and the next manifest asks for it back.
        content = b"one,two\n"
        self.agent.upload(self.link, "GAR_0220.dat", content)
        AgentStationDataFile.objects.update(content_hash=None)

        entry = manifest_entry(self.link, "GAR_0220.dat", content)

        self.assertEqual(
            self.agent.requested([entry]), [(self.link.pk, "GAR_0220.dat")]
        )

    def test_a_file_renamed_on_the_machine_is_a_new_file(self):
        # Identity in the ledger is (station, filename), and nothing tries to
        # recognise the same bytes under another name. Re-sending is cheap,
        # and the core's upsert makes the re-ingested overlap harmless.
        content = b"one,two\n"
        self.agent.upload(self.link, "BEFORE.dat", content)

        renamed = manifest_entry(self.link, "AFTER.dat", content)

        self.assertEqual(
            self.agent.requested([renamed]), [(self.link.pk, "AFTER.dat")]
        )

    def test_the_same_name_under_two_stations_is_two_files(self):
        other = create_station_link(self.connection)
        content = b"one,two\n"
        self.agent.upload(self.link, "DATA.dat", content)

        entries = [
            manifest_entry(self.link, "DATA.dat", content),
            manifest_entry(other, "DATA.dat", content),
        ]

        self.assertEqual(self.agent.requested(entries), [(other.pk, "DATA.dat")])

    def test_a_manifest_is_answered_file_by_file(self):
        held = b"one,two\n"
        self.agent.upload(self.link, "HELD.dat", held)

        entries = [
            manifest_entry(self.link, "HELD.dat", held),
            manifest_entry(self.link, "NEW.dat", b"three,four\n"),
        ]

        self.assertEqual(self.agent.requested(entries), [(self.link.pk, "NEW.dat")])

    def test_an_empty_manifest_asks_for_nothing(self):
        self.assertEqual(self.agent.requested([]), [])

    def test_each_requested_file_carries_the_hash_it_was_offered_under(self):
        # So the agent can match the answer to the candidate it offered,
        # rather than re-reading the file to work out which one this is.
        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")

        requested = self.agent.manifest([entry]).json()["requested"][0]

        self.assertEqual(requested["hash"], entry["hash"])

    def test_offering_a_manifest_writes_nothing_to_the_ledger(self):
        # A proposal is not an arrival. Only the upload endpoint may create a
        # ledger row, or an interrupted cycle would leave ADL believing it
        # holds files it never received.
        self.agent.manifest([manifest_entry(self.link, "GAR_0220.dat", b"x")])

        self.assertEqual(AgentStationDataFile.objects.count(), 0)


class ManifestScopeTests(ManifestTestCase):
    def test_another_devices_station_link_is_not_served(self):
        theirs = create_station_link(create_connection(create_device()))

        entry = manifest_entry(theirs, "THEIRS.dat", b"one,two\n")
        body = self.agent.manifest([entry]).json()

        self.assertEqual(body["requested"], [])
        self.assertEqual(body["unknown_station_links"], [theirs.pk])

    def test_a_station_link_that_has_been_deleted_is_reported_not_fatal(self):
        # A machine works from a cached configuration, so it can offer files
        # for a link an administrator has just deleted. Refusing the whole
        # batch would stall every other station on the machine.
        stale_id = self.link.pk + 1000

        entries = [
            manifest_entry(stale_id, "STALE.dat", b"one\n"),
            manifest_entry(self.link, "GOOD.dat", b"two\n"),
        ]
        body = self.agent.manifest(entries).json()

        self.assertEqual(
            [f["name"] for f in body["requested"]], ["GOOD.dat"]
        )
        self.assertEqual(body["unknown_station_links"], [stale_id])

    def test_a_station_switched_off_centrally_is_not_asked_for_files(self):
        self.link.enabled = False
        self.link.save()

        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")
        body = self.agent.manifest([entry]).json()

        self.assertEqual(body["requested"], [])
        self.assertEqual(body["disabled_station_links"], [self.link.pk])


class ManifestPagingTests(ManifestTestCase):
    def entries(self, count):
        return [
            manifest_entry(self.link, f"FILE_{n:05d}.dat", b"one,two\n")
            for n in range(count)
        ]

    def test_a_full_page_is_answered(self):
        response = self.agent.manifest(self.entries(MANIFEST_PAGE_LIMIT))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["requested"]), MANIFEST_PAGE_LIMIT)

    def test_more_than_a_page_is_refused_with_the_limit(self):
        # Refused rather than truncated: an agent silently told about half its
        # files would believe the rest were already held.
        response = self.agent.manifest(self.entries(MANIFEST_PAGE_LIMIT + 1))
        body = response.json()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["code"], "manifest_too_large")
        self.assertEqual(body["limit"], MANIFEST_PAGE_LIMIT)

    def test_the_page_size_is_advertised_so_an_agent_need_not_guess(self):
        limits = self.client.get(SYNC_URL, **bearer(self.token)).json()["limits"]

        self.assertEqual(limits["manifest_entries"], MANIFEST_PAGE_LIMIT)


class ConcurrentUploadLimitTests(ManifestTestCase):
    """How many files one machine may have on the wire at once.

    ADL's to set and not the agent's to assume, because the scarce thing is
    the country's link and this instance's capacity -- neither of which a
    machine in a vendor's server room can see (wmo-raf/adl#304).
    """

    def limits(self):
        return self.client.get(SYNC_URL, **bearer(self.token)).json()["limits"]

    def test_the_bound_is_served_so_an_agent_need_not_guess(self):
        self.assertEqual(
            self.limits()["concurrent_uploads"], DEFAULT_CONCURRENT_UPLOADS,
        )

    @override_settings(ADL_AGENT_CONCURRENT_UPLOADS=8)
    def test_a_deployment_may_set_it(self):
        # The whole reason it is served rather than compiled into the agent: a
        # country on a link that cannot carry four at once turns it down here,
        # and its fleet follows without anything being reinstalled.
        self.assertEqual(self.limits()["concurrent_uploads"], 8)

    @override_settings(ADL_AGENT_CONCURRENT_UPLOADS=1000)
    def test_more_than_this_instance_will_serve_is_clamped(self):
        self.assertEqual(
            self.limits()["concurrent_uploads"], MOST_CONCURRENT_UPLOADS,
        )

    @override_settings(ADL_AGENT_CONCURRENT_UPLOADS=0)
    def test_zero_is_nonsense_rather_than_a_choice(self):
        # Unlike the reconciliation interval, where zero is a deployment
        # saying the sweep costs more than it is worth. A machine that may
        # upload no files at once is a machine that is not doing anything, so
        # this is a mistyped number and takes the default.
        self.assertEqual(
            self.limits()["concurrent_uploads"], DEFAULT_CONCURRENT_UPLOADS,
        )

    @override_settings(ADL_AGENT_CONCURRENT_UPLOADS="lots")
    def test_an_unreadable_number_leaves_the_fleet_uploading(self):
        self.assertEqual(
            self.limits()["concurrent_uploads"], DEFAULT_CONCURRENT_UPLOADS,
        )


class ManifestBadRequestTests(ManifestTestCase):
    def post(self, payload):
        return self.client.post(
            MANIFEST_URL, data=payload, content_type="application/json",
            **bearer(self.token),
        )

    def test_a_body_that_is_not_an_object_is_refused(self):
        response = self.post([])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_body")

    def test_files_must_be_a_list(self):
        response = self.post({"files": "GAR_0220.dat"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_body")

    def test_an_entry_missing_its_hash_is_refused_by_position(self):
        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")
        del entry["hash"]

        response = self.post({"files": [entry]})
        body = response.json()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["code"], "invalid_entry")
        self.assertEqual(body["errors"][0]["index"], 0)

    def test_a_hash_that_is_not_a_hash_is_refused(self):
        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")
        entry["hash"] = "not-a-sha256"

        self.assertEqual(self.post({"files": [entry]}).json()["code"], "invalid_entry")

    def test_a_filename_that_reaches_out_of_its_folder_is_refused(self):
        entry = manifest_entry(self.link, "../../etc/passwd", b"one,two\n")

        self.assertEqual(self.post({"files": [entry]}).json()["code"], "invalid_entry")

    def test_an_unreadable_mtime_is_refused(self):
        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")
        entry["mtime"] = "the day before yesterday"

        self.assertEqual(self.post({"files": [entry]}).json()["code"], "invalid_entry")

    def test_a_negative_size_is_refused(self):
        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")
        entry["size"] = -1

        self.assertEqual(self.post({"files": [entry]}).json()["code"], "invalid_entry")


class ManifestVersionTests(ManifestTestCase):
    def test_the_answer_carries_the_configuration_version(self):
        # Every response does, so an agent notices a central change on any
        # call and not only on its next sync.
        body = self.agent.manifest([]).json()

        self.assertEqual(body["config_version"], self.device.current_config_version())
