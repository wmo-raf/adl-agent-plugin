"""
How much an agent may send in one go, and how much of it at once.

All three numbers are the server's to state, not the agent's to assume: they
are handed out in the sync response so that a fleet already in the field
follows a change here without being reinstalled.

The first two are not tuning knobs — they exist so that one machine having a
bad day cannot take the instance with it. The third is, and deliberately: how
many files a machine may have on the wire at once is a question about the
country's link and this instance's capacity, and neither is visible from a
vendor's server room. So it is the one number here an operator may set.
"""

import os

from django.conf import settings

#: Candidate files per manifest call. A cycle with more candidates than this
#: pages (decision #267): the extra round trip is cheap, and an unbounded
#: batch from a folder nobody has looked at in a year is not.
MANIFEST_PAGE_LIMIT = 500

#: Bytes per uploaded file, after decompression. Vendor files are kilobytes
#: to a few megabytes; anything near this is a misconfiguration worth
#: refusing loudly rather than absorbing (decision #266).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: Files one machine may upload at once, across every station it serves.
#: Four, which is enough to stop three thousand files of backfill going up
#: one round trip at a time -- the round trip is nearly all waiting -- and
#: small enough not to bury the link a country's whole met service shares.
DEFAULT_CONCURRENT_UPLOADS = 4

#: Settable per deployment, from Django settings for tests and from the
#: environment for operators, exactly as the pair throttle and the fleet
#: health thresholds are.
CONCURRENT_UPLOADS_SETTING = "ADL_AGENT_CONCURRENT_UPLOADS"

#: What this instance will serve however large a number it is given. A
#: deployment cannot ask a machine for more sockets than an ADL could
#: usefully answer, and the agent clamps again on its own side -- because a
#: number that arrives from a newer, differently-configured ADL is still a
#: number a machine in the field has to survive.
MOST_CONCURRENT_UPLOADS = 32


def concurrent_uploads():
    """How many files a machine may have on the wire at once.

    Read the way every other operator-settable number in this plugin is:
    Django settings first, the environment second, and the default when it is
    absent or nonsense. A deployment that mistypes it keeps its fleet
    uploading at the default rather than getting an instance that will not
    start, or one that quietly stops every machine it serves.
    """
    raw = getattr(settings, CONCURRENT_UPLOADS_SETTING, None)

    if raw is None:
        raw = os.environ.get(CONCURRENT_UPLOADS_SETTING)

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENT_UPLOADS

    if value < 1:
        # Zero is not a deployment switching uploads off -- a machine that
        # cannot upload is a machine that is not doing anything -- so unlike
        # the reconciliation interval, it is nonsense rather than a choice.
        return DEFAULT_CONCURRENT_UPLOADS

    return min(value, MOST_CONCURRENT_UPLOADS)
