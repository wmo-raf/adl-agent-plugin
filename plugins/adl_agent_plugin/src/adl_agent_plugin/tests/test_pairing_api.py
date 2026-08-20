"""
The pairing lifecycle, driven entirely through HTTP.

Every test here talks to the same two URLs a real agent talks to, and
asserts on what the agent would see plus what the row ends up holding.
Nothing reaches inside the view or the authentication class: "pair, call,
revoke, get 401" is the behaviour being promised, so it is the behaviour
being tested.
"""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as dj_timezone

from adl_agent_plugin.credentials import PAIRING_CODE_TTL, hash_device_token
from adl_agent_plugin.models import AgentDevice

from .helpers import (
    ME_URL,
    PAIR_URL,
    bearer,
    clear_pair_throttle,
    configured_pair_attempts,
)


class AgentPairingTestCase(TestCase):
    def setUp(self):
        clear_pair_throttle()
        self.addCleanup(clear_pair_throttle)
        self.device = AgentDevice.objects.create(name="Nairobi vendor server")

    def pair(self, code):
        return self.client.post(
            PAIR_URL, {"pairing_code": code}, content_type="application/json"
        )


class PairingCodeIssueTests(AgentPairingTestCase):
    def test_new_device_is_born_with_a_valid_pairing_code(self):
        self.assertTrue(self.device.pairing_code)
        self.assertTrue(self.device.pairing_code_is_valid)
        self.assertEqual(self.device.status, AgentDevice.STATUS_AWAITING_PAIRING)

    def test_pairing_code_expires_in_72_hours(self):
        remaining = self.device.pairing_code_expires_at - dj_timezone.now()

        self.assertEqual(PAIRING_CODE_TTL, timedelta(hours=72))
        # A second of slack for the time the row took to be written.
        self.assertLess(abs(remaining - PAIRING_CODE_TTL), timedelta(seconds=5))

    def test_pairing_code_is_human_typeable(self):
        code = self.device.pairing_code

        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        for ambiguous in "01ILOU":
            self.assertNotIn(ambiguous, code)


class PairExchangeTests(AgentPairingTestCase):
    def test_code_is_exchanged_for_a_token(self):
        response = self.pair(self.device.pairing_code)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["token"])
        self.assertEqual(body["device"]["name"], "Nairobi vendor server")
        self.assertEqual(body["device"]["id"], self.device.pk)

    def test_token_is_stored_only_as_a_digest(self):
        token = self.pair(self.device.pairing_code).json()["token"]

        self.device.refresh_from_db()
        self.assertEqual(self.device.token_hash, hash_device_token(token))
        # The clear-text token appears nowhere on the row.
        stored = str(AgentDevice.objects.filter(pk=self.device.pk).values()[0])
        self.assertNotIn(token, stored)

    def test_pairing_clears_the_code_and_stamps_the_device(self):
        self.pair(self.device.pairing_code)

        self.device.refresh_from_db()
        self.assertIsNone(self.device.pairing_code)
        self.assertIsNone(self.device.pairing_code_expires_at)
        self.assertIsNotNone(self.device.paired_at)
        self.assertEqual(self.device.status, AgentDevice.STATUS_PAIRED)

    def test_code_is_accepted_however_a_human_types_it(self):
        """What a technician types is rarely what the screen showed."""
        shapes = {
            "lower case": lambda code: code.lower(),
            "no separator": lambda code: code.replace("-", ""),
            "spaces for the separator": lambda code: " {} {} ".format(
                code[:4], code[5:]
            ),
            "lower case and no separator": lambda code: code.replace("-", "").lower(),
        }

        for label, shape in shapes.items():
            with self.subTest(case=label):
                device = AgentDevice.objects.create(name=f"device typed {label}")

                response = self.pair(shape(device.pairing_code))

                self.assertEqual(response.status_code, 200, response.content)

    def test_code_works_exactly_once(self):
        code = self.device.pairing_code
        self.assertEqual(self.pair(code).status_code, 200)

        response = self.pair(code)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_pairing_code")

    def test_expired_code_is_refused_and_stays_refused(self):
        AgentDevice.objects.filter(pk=self.device.pk).update(
            pairing_code_expires_at=dj_timezone.now() - timedelta(minutes=1)
        )

        response = self.pair(self.device.pairing_code)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "expired_pairing_code")
        self.assertEqual(self.pair(self.device.pairing_code).status_code, 400)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.token_hash)
        self.assertFalse(self.device.pairing_code_is_valid)
        self.assertEqual(self.device.status, AgentDevice.STATUS_UNPAIRED)

    def test_unknown_and_malformed_codes_are_refused(self):
        for payload in [
            {"pairing_code": "ZZZZ-ZZZZ"},
            {"pairing_code": "not a code"},
            {"pairing_code": ""},
            {},
            {"pairing_code": None},
        ]:
            with self.subTest(payload=payload):
                response = self.client.post(
                    PAIR_URL, payload, content_type="application/json"
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid_pairing_code")

    def test_pair_endpoint_needs_no_credential(self):
        response = self.pair(self.device.pairing_code)

        self.assertEqual(response.status_code, 200)

    def test_pair_endpoint_is_rate_limited(self):
        allowed = configured_pair_attempts()

        for _ in range(allowed):
            self.assertEqual(self.pair("ZZZZ-ZZZZ").status_code, 400)

        response = self.pair("ZZZZ-ZZZZ")

        self.assertEqual(response.status_code, 429)


class AuthenticatedCallTests(AgentPairingTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.pair(self.device.pairing_code).json()["token"]

    def test_token_authenticates_an_agent_endpoint(self):
        response = self.client.get(ME_URL, **bearer(self.token))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Nairobi vendor server")

    def test_authenticated_call_records_the_device_as_seen(self):
        AgentDevice.objects.filter(pk=self.device.pk).update(last_seen_at=None)

        self.client.get(ME_URL, **bearer(self.token))

        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_seen_at)

    def test_missing_and_bad_credentials_are_401(self):
        cases = {
            "no header": {},
            "empty bearer": {"HTTP_AUTHORIZATION": "Bearer"},
            "wrong token": bearer("not-a-real-token"),
            "wrong scheme": {"HTTP_AUTHORIZATION": f"Token {self.token}"},
            "spaces in token": {"HTTP_AUTHORIZATION": f"Bearer {self.token} extra"},
        }

        for label, headers in cases.items():
            with self.subTest(case=label):
                response = self.client.get(ME_URL, **headers)

                self.assertEqual(response.status_code, 401)

    def test_device_token_is_refused_by_the_core_api(self):
        """The authorization boundary from decision #259.

        A device token is a credential on this plugin's endpoints and
        nowhere else -- the core API does not know the scheme exists.
        """
        response = self.client.get(
            reverse("data_parameters"), **bearer(self.token)
        )

        self.assertIn(response.status_code, (401, 403))


class RevocationTests(AgentPairingTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.pair(self.device.pairing_code).json()["token"]
        self.device.refresh_from_db()

    def test_revoked_device_is_401_everywhere(self):
        self.device.revoke()

        response = self.client.get(ME_URL, **bearer(self.token))

        self.assertEqual(response.status_code, 401)

    def test_revocation_destroys_the_token_and_any_pairing_code(self):
        self.device.issue_pairing_code()
        outstanding_code = self.device.pairing_code

        self.device.revoke()

        self.device.refresh_from_db()
        self.assertIsNone(self.device.token_hash)
        self.assertIsNone(self.device.pairing_code)
        self.assertEqual(self.device.status, AgentDevice.STATUS_REVOKED)
        self.assertEqual(self.pair(outstanding_code).status_code, 400)

    def test_a_revoked_device_comes_back_by_re_pairing(self):
        self.device.revoke()
        code = self.device.issue_pairing_code()

        token = self.pair(code).json()["token"]

        self.device.refresh_from_db()
        self.assertIsNone(self.device.revoked_at)
        self.assertEqual(
            self.client.get(ME_URL, **bearer(token)).status_code, 200
        )


class RotationTests(AgentPairingTestCase):
    def setUp(self):
        super().setUp()
        self.old_token = self.pair(self.device.pairing_code).json()["token"]
        self.device.refresh_from_db()

    def test_issuing_a_code_leaves_the_current_token_working(self):
        self.device.issue_pairing_code()

        response = self.client.get(ME_URL, **bearer(self.old_token))

        self.assertEqual(response.status_code, 200)

    def test_redeeming_the_new_code_replaces_the_old_token(self):
        code = self.device.issue_pairing_code()

        new_token = self.pair(code).json()["token"]

        self.assertNotEqual(new_token, self.old_token)
        self.assertEqual(
            self.client.get(ME_URL, **bearer(new_token)).status_code, 200
        )
        self.assertEqual(
            self.client.get(ME_URL, **bearer(self.old_token)).status_code, 401
        )

    def test_issuing_a_code_replaces_an_unused_one(self):
        first = self.device.issue_pairing_code()
        second = self.device.issue_pairing_code()

        self.assertNotEqual(first, second)
        self.assertEqual(self.pair(first).status_code, 400)
        self.assertEqual(self.pair(second).status_code, 200)
