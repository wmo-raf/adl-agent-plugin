"""
A manifest entry: one file, as the agent describes it.

The same four facts travel twice -- once in the manifest, where they are a
proposal, and again with the upload, where they are a promise ADL checks the
bytes against. Reading them is therefore one job, done here, so that a
filename the manifest would refuse cannot slip in through the upload.

Nothing in this module touches the database. It turns whatever arrived on
the wire into a :class:`FileEntry` or refuses it.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from .errors import AgentRequestRejected

#: A sha-256 digest, lowercase hex. Stated rather than inferred: the ledger
#: diffs on exact equality, so an agent hashing with something else has to
#: fail loudly here rather than have every one of its files look "changed".
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: What the field can hold, and so what ADL will remember a file as.
MAX_NAME_LENGTH = 255

REQUIRED_FIELDS = ("station_link_id", "name", "size", "mtime", "hash")


@dataclass(frozen=True)
class FileEntry:
    """One candidate file, checked and in native types."""

    station_link_id: int
    name: str
    size: int
    mtime: datetime
    content_hash: str


def _invalid(detail):
    """One entry could not be read, in a technician's words."""
    return AgentRequestRejected("invalid_entry", detail)


def parse_entry(raw):
    """Read one entry, or refuse it.

    Raises :class:`~adl_agent_plugin.errors.AgentRequestRejected` -- the one
    envelope this API says no with -- so that a view refusing a single entry
    and a view refusing a batch of them answer in the same shape.
    """
    if not isinstance(raw, dict):
        raise _invalid(_("Each file must be an object."))

    missing = [name for name in REQUIRED_FIELDS if raw.get(name) is None]
    if missing:
        raise _invalid(
            _("Missing: %(fields)s.") % {"fields": ", ".join(missing)}
        )

    return FileEntry(
        station_link_id=_positive_int(raw["station_link_id"], "station_link_id"),
        name=_file_name(raw["name"]),
        size=_size(raw["size"]),
        mtime=_mtime(raw["mtime"]),
        content_hash=_content_hash(raw["hash"]),
    )


def _positive_int(value, field):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _invalid(_("%(field)s must be a number.") % {"field": field})

    if number <= 0:
        raise _invalid(_("%(field)s must be positive.") % {"field": field})

    return number


def _size(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _invalid(_("size must be a number of bytes."))

    # Zero is a real size: a logger that has created today's file but not yet
    # written to it is offering an empty file, not a broken one.
    if number < 0:
        raise _invalid(_("size cannot be negative."))

    return number


def _file_name(value):
    """The bare filename, with everything that is not one refused.

    A name arrives from a machine ADL does not control and goes on to name an
    object in storage, so this is a boundary: no directories, no traversal,
    nothing that resolves anywhere but where the station link's own files go.
    """
    if not isinstance(value, str):
        raise _invalid(_("name must be text."))

    name = value.strip()

    if not name:
        raise _invalid(_("name cannot be empty."))

    if len(name) > MAX_NAME_LENGTH:
        raise _invalid(
            _("name cannot be longer than %(limit)s characters.")
            % {"limit": MAX_NAME_LENGTH}
        )

    if "/" in name or "\\" in name or "\x00" in name:
        raise _invalid(
            _("name must be the file's own name, without any folder.")
        )

    if name in {".", ".."}:
        raise _invalid(_("name must be a file name."))

    return name


def _mtime(value):
    """The file's last-write time, as an aware datetime.

    A naive timestamp is read as UTC rather than refused. Vendor machines
    keep local time and their agents are meant to convert; one that does not
    should not silently stop delivering, and the ledger only ever compares
    these to each other.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = parse_datetime(str(value))
        except ValueError:
            parsed = None

    if parsed is None:
        raise _invalid(_("mtime must be an ISO 8601 date and time."))

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed


def _content_hash(value):
    if not isinstance(value, str) or not HASH_PATTERN.match(value.strip().lower()):
        raise _invalid(_("hash must be a sha-256 digest in lowercase hex."))

    return value.strip().lower()


def parse_entries(raw_entries):
    """Read a list of entries, reporting every bad one at once.

    All-or-nothing, and by position: a technician reading the log of a failed
    cycle needs to know which of five hundred files was wrong, and an agent
    that had half its manifest accepted would believe the other half was
    already held.
    """
    entries, errors = [], []

    for index, raw in enumerate(raw_entries):
        try:
            entries.append(parse_entry(raw))
        except AgentRequestRejected as exc:
            errors.append({"index": index, "detail": exc.detail})

    if errors:
        raise AgentRequestRejected(
            "invalid_entry",
            _("%(count)s of the files offered could not be read.")
            % {"count": len(errors)},
            errors=errors,
        )

    return entries
