"""
``POST /api/agent/v1/files`` -- one file, one request.

An upload is a promise being kept: the agent said a file of this size with
this hash exists, and here it is. So most of what follows is about ADL
checking the promise before believing it -- because a file that changed
while it was being read, or a truncated transfer, must not become a
"processed" observation record later.

Failure granularity is one file. Nothing here ever rejects a whole cycle.
"""

import gzip

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from adl_agent_plugin.limits import MAX_UPLOAD_BYTES
from adl_agent_plugin.models import AgentFileStatus, AgentStationDataFile

from .helpers import (
    FILES_URL,
    AgentClient,
    TemporaryMediaRoot,
    bearer,
    create_connection,
    create_device,
    create_station_link,
    manifest_entry,
    paired_device,
    sha256_of,
)


def _part(content, name="GAR_0220.dat"):
    """The file part of a multipart upload, built by hand."""
    return SimpleUploadedFile(name, content, content_type="application/octet-stream")


class UploadTestCase(TemporaryMediaRoot, TestCase):
    def setUp(self):
        self.device, self.token = paired_device(name="Nairobi vendor server")
        self.connection = create_connection(self.device)
        self.link = create_station_link(self.connection)
        self.agent = AgentClient(self, self.token)

    def ledger_row(self, name="GAR_0220.dat"):
        return AgentStationDataFile.objects.get(
            station_link=self.link, file_name=name
        )

    def stored_bytes(self, row):
        with row.file.open("rb") as handle:
            return handle.read()


class UploadAuthTests(UploadTestCase):
    def test_uploading_needs_a_token(self):
        response = self.client.post(
            FILES_URL, data=manifest_entry(self.link, "GAR_0220.dat", b"x")
        )

        self.assertEqual(response.status_code, 401)

    def test_a_revoked_device_cannot_upload(self):
        self.device.revoke()

        response = self.agent.upload(self.link, "GAR_0220.dat", b"one,two\n")

        self.assertEqual(response.status_code, 401)

    def test_another_devices_station_link_cannot_be_written_to(self):
        theirs = create_station_link(create_connection(create_device()))

        response = self.agent.upload(theirs, "THEIRS.dat", b"one,two\n")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.assertEqual(AgentStationDataFile.objects.count(), 0)


class UploadAcceptedTests(UploadTestCase):
    def test_a_file_that_matches_its_manifest_entry_is_kept(self):
        content = b"time,temp\n2026-02-20T10:00:00,21.4\n"

        response = self.agent.upload(self.link, "GAR_0220.dat", content)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.stored_bytes(self.ledger_row()), content)

    def test_the_ledger_records_what_arrived(self):
        content = b"time,temp\n2026-02-20T10:00:00,21.4\n"

        self.agent.upload(self.link, "GAR_0220.dat", content)
        row = self.ledger_row()

        self.assertEqual(row.size, len(content))
        self.assertEqual(row.content_hash, sha256_of(content))
        self.assertEqual(row.status, AgentFileStatus.RECEIVED)
        self.assertIsNotNone(row.received_at)

    def test_the_answer_says_what_now_stands(self):
        content = b"one,two\n"

        body = self.agent.upload(self.link, "GAR_0220.dat", content).json()

        self.assertEqual(body["station_link_id"], self.link.pk)
        self.assertEqual(body["name"], "GAR_0220.dat")
        self.assertEqual(body["status"], AgentFileStatus.RECEIVED)
        self.assertEqual(body["config_version"], self.device.current_config_version())

    def test_a_gzipped_upload_is_stored_as_the_file_it_was(self):
        # The hash is always over the file on the vendor's disk, never over
        # the compressed form, so switching compression on cannot make ADL
        # re-request everything it already holds.
        content = b"time,temp\n" + b"2026-02-20T10:00:00,21.4\n" * 200

        response = self.agent.upload(
            self.link, "GAR_0220.dat", content, compress=True
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.stored_bytes(self.ledger_row()), content)

    def test_an_empty_file_is_a_file(self):
        response = self.agent.upload(self.link, "EMPTY.dat", b"")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.ledger_row("EMPTY.dat").size, 0)


class UploadVerificationTests(UploadTestCase):
    def test_bytes_that_do_not_match_the_declared_hash_are_refused(self):
        # The file changed under the agent while it was being read. Sending
        # it on would stage a file whose ledger hash describes something else.
        response = self.agent.upload(
            self.link, "GAR_0220.dat", b"one,two\n", hash=sha256_of(b"something else")
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "hash_mismatch")

    def test_a_refused_file_leaves_no_trace(self):
        self.agent.upload(
            self.link, "GAR_0220.dat", b"one,two\n", hash=sha256_of(b"other")
        )

        self.assertEqual(AgentStationDataFile.objects.count(), 0)

    def test_a_refused_file_is_simply_offered_again_next_cycle(self):
        content = b"one,two\n"
        self.agent.upload(self.link, "GAR_0220.dat", content, hash=sha256_of(b"o"))

        entry = manifest_entry(self.link, "GAR_0220.dat", content)

        self.assertEqual(
            self.agent.requested([entry]), [(self.link.pk, "GAR_0220.dat")]
        )

    def test_bytes_that_do_not_match_the_declared_size_are_refused(self):
        response = self.agent.upload(
            self.link, "GAR_0220.dat", b"one,two\n", size=999
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "size_mismatch")

    def test_a_file_over_the_cap_is_refused_before_it_is_read(self):
        response = self.agent.upload(
            self.link, "HUGE.dat", b"x" * 2048, size=MAX_UPLOAD_BYTES + 1
        )
        body = response.json()

        self.assertEqual(response.status_code, 413)
        self.assertEqual(body["code"], "file_too_large")
        self.assertEqual(body["limit"], MAX_UPLOAD_BYTES)

    def test_a_request_with_no_file_part_is_refused(self):
        response = self.client.post(
            FILES_URL,
            data=manifest_entry(self.link, "GAR_0220.dat", b"one,two\n"),
            **bearer(self.token),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "file_missing")

    def test_bytes_that_are_not_gzip_but_claim_to_be_are_refused(self):
        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")
        response = self.client.post(
            FILES_URL,
            data={**entry, "encoding": "gzip", "file": _part(b"one,two\n")},
            **bearer(self.token),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_encoding")

    def test_an_unknown_encoding_is_refused(self):
        entry = manifest_entry(self.link, "GAR_0220.dat", b"one,two\n")
        response = self.client.post(
            FILES_URL,
            data={**entry, "encoding": "brotli", "file": _part(b"one,two\n")},
            **bearer(self.token),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_encoding")

    def test_a_compressed_file_that_expands_past_the_cap_is_refused(self):
        # A small upload that unpacks to gigabytes must not be unpacked into
        # memory first and measured afterwards.
        content = b"\0" * (MAX_UPLOAD_BYTES + 1024)
        entry = manifest_entry(self.link, "BOMB.dat", content)
        response = self.client.post(
            FILES_URL,
            data={**entry, "encoding": "gzip", "file": _part(gzip.compress(content))},
            **bearer(self.token),
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "file_too_large")

    def test_a_filename_that_reaches_out_of_its_folder_is_refused(self):
        entry = manifest_entry(self.link, "GAR.dat", b"one\n")
        entry["name"] = "../x.dat"

        response = self.client.post(
            FILES_URL, data={**entry, "file": _part(b"one\n")},
            **bearer(self.token),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_entry")

    def test_a_station_switched_off_centrally_takes_no_files(self):
        self.link.enabled = False
        self.link.save()

        response = self.agent.upload(self.link, "GAR_0220.dat", b"one,two\n")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "station_link_disabled")


class ReUploadTests(UploadTestCase):
    def setUp(self):
        super().setUp()
        self.agent.upload(self.link, "GAR_0220.dat", b"one,two\n")
        self.first = self.ledger_row()

    def grow(self):
        # The bytes a re-upload replaces are deleted once the row that
        # replaced them is safely committed, so a test that wants to see the
        # deletion has to let the commit happen.
        with self.captureOnCommitCallbacks(execute=True):
            return self.agent.upload(
                self.link, "GAR_0220.dat", b"one,two\nthree,four\n"
            )

    def test_a_changed_file_updates_its_row_rather_than_adding_one(self):
        self.grow()

        self.assertEqual(AgentStationDataFile.objects.count(), 1)
        self.assertEqual(self.ledger_row().pk, self.first.pk)

    def test_a_changed_file_brings_new_bytes_and_a_new_hash(self):
        self.grow()
        row = self.ledger_row()

        self.assertEqual(self.stored_bytes(row), b"one,two\nthree,four\n")
        self.assertEqual(row.content_hash, sha256_of(b"one,two\nthree,four\n"))

    def test_a_changed_file_goes_back_to_waiting_to_be_processed(self):
        # It is the whole file that comes again, so what ADL decided about
        # the shorter version -- processed, or failed with an error -- says
        # nothing about this one.
        AgentStationDataFile.objects.update(
            status=AgentFileStatus.FAILED,
            values_saved=4,
            last_error="could not decode row 2",
        )

        self.grow()
        row = self.ledger_row()

        self.assertEqual(row.status, AgentFileStatus.RECEIVED)
        self.assertIsNone(row.processed_at)
        self.assertIsNone(row.values_saved)
        self.assertEqual(row.last_error, "")

    def test_the_bytes_it_replaces_are_not_left_behind(self):
        superseded = self.first.file.name

        self.grow()

        self.assertNotEqual(self.ledger_row().file.name, superseded)
        self.assertFalse(self.first.file.storage.exists(superseded))
