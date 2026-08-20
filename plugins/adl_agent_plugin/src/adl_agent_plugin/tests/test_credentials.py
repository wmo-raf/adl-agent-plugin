"""
The credential rules on their own, with no database and no HTTP.

The lifecycle tests next door cover what an agent sees; these cover the two
things that are properties of the secrets themselves and awkward to observe
from outside: what a pairing code is made of, and what typing mistakes it
forgives.
"""

from django.test import SimpleTestCase

from adl_agent_plugin.credentials import (
    DEVICE_TOKEN_BYTES,
    PAIRING_CODE_ALPHABET,
    generate_device_token,
    generate_pairing_code,
    hash_device_token,
    normalize_pairing_code,
)


class PairingCodeShapeTests(SimpleTestCase):
    def test_codes_are_grouped_and_unambiguous(self):
        for _ in range(50):
            code = generate_pairing_code()

            self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
            self.assertTrue(
                set(code.replace("-", "")) <= set(PAIRING_CODE_ALPHABET)
            )

    def test_codes_do_not_repeat(self):
        codes = {generate_pairing_code() for _ in range(200)}

        self.assertEqual(len(codes), 200)


class PairingCodeNormalizationTests(SimpleTestCase):
    def test_a_canonical_code_survives_unchanged(self):
        code = generate_pairing_code()

        self.assertEqual(normalize_pairing_code(code), code)

    def test_common_typing_variants_fold_to_the_canonical_form(self):
        code = generate_pairing_code()
        bare = code.replace("-", "")

        for typed in [code.lower(), bare, bare.lower(), f"  {code}  ",
                      f"{bare[:4]} {bare[4:]}", f"{bare[:4]}--{bare[4:]}"]:
            with self.subTest(typed=typed):
                self.assertEqual(normalize_pairing_code(typed), code)

    def test_anything_that_could_not_be_a_code_folds_to_empty(self):
        for typed in [None, "", "  ", "ABC-DEF", "ABCD-EFGHI", "hello there",
                      "OOOO-1111"]:
            with self.subTest(typed=typed):
                self.assertEqual(normalize_pairing_code(typed), "")


class DeviceTokenTests(SimpleTestCase):
    def test_tokens_carry_the_intended_entropy(self):
        token = generate_device_token()

        # url-safe base64 packs 6 bits per character.
        self.assertGreaterEqual(len(token) * 6, DEVICE_TOKEN_BYTES * 8)

    def test_tokens_do_not_repeat(self):
        self.assertEqual(len({generate_device_token() for _ in range(200)}), 200)

    def test_hashing_is_stable_and_one_way(self):
        token = generate_device_token()
        digest = hash_device_token(token)

        self.assertEqual(digest, hash_device_token(token))
        self.assertEqual(len(digest), 64)
        self.assertNotIn(token, digest)
        self.assertNotEqual(digest, hash_device_token(generate_device_token()))
