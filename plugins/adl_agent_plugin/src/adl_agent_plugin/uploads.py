"""
Receiving one file: check the promise before believing it.

The agent has already told ADL, in its manifest entry, exactly what this
file is -- how many bytes, and what they hash to. This module reads the bytes
off the wire and checks them against that. A file that changed while it was
being read, or a transfer that was cut short, fails here and is simply
offered again next cycle (decision #266); what must never happen is a file
being staged under a hash that describes something else, because the ledger
would then believe ADL holds data it does not.

The bytes are streamed to the caller's file rather than gathered in memory,
and the size cap is enforced as they arrive: a fifty-megabyte limit checked
after reading is not a limit.
"""

import hashlib
import zlib

from django.utils.translation import gettext as _
from rest_framework import status

from .errors import AgentRequestRejected
from .limits import MAX_UPLOAD_BYTES

#: What ``encoding`` may say. ``identity`` is the default and means "these
#: are the file's own bytes"; ``gzip`` is the optional compression of
#: decision #266 -- worth having on a satellite link, pointless on a LAN.
#:
#: It travels as a form field beside the file rather than in the
#: ``Content-Encoding`` header that decision named. That header describes the
#: whole request body, and gzipping a whole multipart body would leave the
#: server unable to find the parts at all; compression here is a property of
#: one part, so it is one part's field to carry.
IDENTITY = "identity"
GZIP = "gzip"
ENCODINGS = (IDENTITY, GZIP)

READ_CHUNK = 64 * 1024


def receive(uploaded, encoding, entry, destination):
    """Read the file ``entry`` promised into ``destination``.

    Returns nothing: either ``destination`` holds exactly the bytes the entry
    describes, positioned at its start and ready for storage, or this raises
    :class:`~adl_agent_plugin.errors.AgentRequestRejected` and the caller has
    nothing to keep.

    The hash checked is always over the file as it sits on the vendor's disk,
    never over the compressed form -- otherwise an agent switching gzip on
    would make every file it holds look changed.
    """
    if uploaded is None:
        raise AgentRequestRejected(
            "file_missing", _("Attach the file itself as \"file\"."),
        )

    decompress = _decompressor(encoding)

    # Both sizes are checked before a byte is read: what the agent says the
    # file is, and what it has actually attached. A machine offering
    # something absurd is turned away rather than allowed to spend a link
    # that could not spare it on an upload that ends in a refusal.
    _guard_size(entry.size)
    _guard_size(uploaded.size)

    digest = hashlib.sha256()
    size = 0

    for chunk in _chunks(uploaded, decompress):
        size += len(chunk)
        # Reached on the gzip path in particular: a small upload that unpacks
        # to gigabytes stops here, part-way, rather than after.
        _guard_size(size)

        digest.update(chunk)
        destination.write(chunk)

    destination.flush()
    destination.seek(0)

    _verify(entry, size, digest.hexdigest())


def _chunks(uploaded, decompress):
    """The file's own bytes, chunk by chunk, whatever it arrived encoded as."""
    for chunk in uploaded.chunks(READ_CHUNK):
        yield decompress(chunk)

    yield decompress(None)


def _verify(entry, size, content_hash):
    """Check what arrived against what was promised.

    Size is checked as well as the hash, though a wrong size nearly always
    means a wrong hash too. It is worth its own answer because it is the one
    an agent can get wrong *honestly* -- a file that grew between being
    stat'ed and being read -- and saying so plainly beats reporting a digest
    mismatch a technician has no way to interpret.
    """
    if size != entry.size:
        raise AgentRequestRejected(
            "size_mismatch",
            _("The file is %(actual)s bytes, not the %(declared)s it was "
              "offered as. It was probably still being written.")
            % {"actual": size, "declared": entry.size},
            declared=entry.size,
            actual=size,
        )

    if content_hash != entry.content_hash:
        raise AgentRequestRejected(
            "hash_mismatch",
            _("The file does not hash to what it was offered as. It probably "
              "changed while it was being read; offer it again next cycle."),
            declared=entry.content_hash,
            actual=content_hash,
        )


def _decompressor(encoding):
    """A callable turning one chunk of the upload into file bytes.

    Called once more with ``None`` at the end of the stream, which is where a
    truncated gzip member is caught: a decompressor that never reached the end
    of its stream was handed a partial file.

    ``zlib`` rather than ``gzip``, whose reader cannot work through a stream
    without a seekable whole to work on -- and the whole is what the cap
    exists to avoid holding.
    """
    encoding = (encoding or IDENTITY).strip().lower()

    if encoding == IDENTITY:
        return lambda chunk: b"" if chunk is None else chunk

    if encoding != GZIP:
        raise AgentRequestRejected(
            "invalid_encoding",
            _("encoding must be one of: %(encodings)s.")
            % {"encodings": ", ".join(ENCODINGS)},
        )

    stream = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)

    def decompress(chunk):
        if chunk is None:
            if not stream.eof:
                raise _not_gzip()
            return stream.flush()

        try:
            return stream.decompress(chunk)
        except zlib.error:
            raise _not_gzip()

    return decompress


def _not_gzip():
    return AgentRequestRejected(
        "invalid_encoding",
        _("The upload was offered as gzip but could not be decompressed."),
    )


def _guard_size(size):
    if size is None or size <= MAX_UPLOAD_BYTES:
        return

    raise AgentRequestRejected(
        "file_too_large",
        _("Files must be at most %(limit)s bytes.") % {"limit": MAX_UPLOAD_BYTES},
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        limit=MAX_UPLOAD_BYTES,
    )
