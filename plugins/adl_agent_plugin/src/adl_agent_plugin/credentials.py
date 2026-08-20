"""
Pure credential mechanics for agent device identity.

Two secrets exist, and they are deliberately different animals:

*Pairing code* — short, human-typeable, single-use, short-lived. Its whole
job is to be read off an ADL admin screen in one country and typed into an
agent installer in another. It is stored in the clear so the admin who has
to relay it can read it again; that is safe because it expires in 72 hours,
dies on first use, and grants nothing on its own that DB access would not
already grant.

*Device token* — long, random, opaque, non-expiring. This is the durable
credential that sits on an unattended Windows box for years, so it is stored
only as a SHA-256 digest and is shown exactly once, in the pair response.
SHA-256 rather than a slow KDF is the right choice here precisely because
the token is full-entropy random: there is no password to grind.

Nothing in this module touches Django models or the database, so the rules
about code shape, expiry and hashing can be read (and tested) on their own.
"""

import hashlib
import secrets
from datetime import timedelta

#: How long a freshly issued pairing code stays redeemable. Decision #259.
PAIRING_CODE_TTL = timedelta(hours=72)

#: Alphabet for pairing codes: upper-case letters and digits with every
#: pair a human confuses when reading a code down a phone line removed --
#: 0/O, 1/I/L, and U (which people hear as "you" and write as V). 30
#: symbols over 8 characters is about 2**39 codes, far beyond what the
#: rate-limited pair endpoint could ever be walked through.
PAIRING_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"

#: Characters per group, and the separator between the two groups.
PAIRING_CODE_GROUP_SIZE = 4
PAIRING_CODE_SEPARATOR = "-"

#: Bytes of entropy in a device token. Decision #259 says "~32 bytes".
DEVICE_TOKEN_BYTES = 32


class PairingError(Exception):
    """A pairing code was offered and could not be redeemed.

    ``code`` is the stable machine-readable reason the agent app switches
    on; the message is what a human is shown.
    """

    code = "pairing_failed"

    def __init__(self, message=None):
        super().__init__(message or self.default_message)

    default_message = "The pairing code could not be redeemed."


class InvalidPairingCode(PairingError):
    code = "invalid_pairing_code"
    default_message = (
        "That pairing code is not recognised. Ask your ADL administrator to "
        "issue a new one."
    )


class ExpiredPairingCode(PairingError):
    code = "expired_pairing_code"
    default_message = (
        "That pairing code has expired. Ask your ADL administrator to issue "
        "a new one."
    )


def generate_pairing_code():
    """Return a fresh ``XXXX-XXXX`` pairing code."""
    body = "".join(
        secrets.choice(PAIRING_CODE_ALPHABET)
        for _ in range(PAIRING_CODE_GROUP_SIZE * 2)
    )
    return format_pairing_code(body)


def format_pairing_code(body):
    """Group a bare 8-character code body into ``XXXX-XXXX``."""
    size = PAIRING_CODE_GROUP_SIZE
    return PAIRING_CODE_SEPARATOR.join([body[:size], body[size:]])


#: Folded away before a typed code is read: the separator itself, however
#: many times it was typed, plus whatever whitespace came with a copy-paste.
PAIRING_CODE_IGNORED = set(" \t\r\n-_")


def normalize_pairing_code(raw):
    """Fold what a human typed into the canonical stored form.

    Forgives the things that are formatting -- lower case, a missing or
    doubled separator, stray whitespace -- and nothing else. A character
    that is not in the alphabet is a wrong code, not a mistyped separator,
    so it is refused rather than quietly dropped: dropping it would let
    ``ABCD-EFGHI`` succeed as ``ABCD-EFGH``.

    Returns ``""`` for anything that could not be a pairing code, so callers
    can reject it without ever running the query.
    """
    if not raw:
        return ""

    body = "".join(
        ch for ch in str(raw).upper() if ch not in PAIRING_CODE_IGNORED
    )

    if len(body) != PAIRING_CODE_GROUP_SIZE * 2:
        return ""

    if any(ch not in PAIRING_CODE_ALPHABET for ch in body):
        return ""

    return format_pairing_code(body)


def generate_device_token():
    """Return a fresh opaque device token, in the clear.

    This is the only moment the token exists in readable form; the caller
    hands it straight to the agent and stores only its digest.
    """
    return secrets.token_urlsafe(DEVICE_TOKEN_BYTES)


def hash_device_token(token):
    """Return the digest a device token is stored and looked up by."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
