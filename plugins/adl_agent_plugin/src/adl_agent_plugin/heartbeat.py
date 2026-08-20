"""
A heartbeat: one machine saying what it is and how it is doing.

Read here, in native types, before anything touches the database -- the same
division of labour :mod:`adl_agent_plugin.entries` makes for manifest entries,
and for the same reason: a machine ADL does not control is on the other end.

Two rules shape what this module refuses. **Everything is optional**, because
a heartbeat that ADL rejects is a heartbeat that never arrives, and a machine
whose disk query failed should still be able to say it is alive -- which is
the one fact the whole liveness ladder rests on. But **nothing is guessed**:
a field that is present and unreadable is refused rather than dropped, so an
agent shipping a wrong shape learns it at once instead of appearing healthy
while every number ADL shows is silently missing.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Optional, Tuple

from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from .errors import AgentRequestRejected

#: What the version columns can hold. Longer than any version string, short
#: enough that a machine cannot write an essay into the fleet listing. Stated
#: here and read by the model fields, so the bound and the truncation cannot
#: drift apart.
MAX_VERSION_LENGTH = 100
MAX_OS_VERSION_LENGTH = 255

#: Per-link cycle statistics kept from each report. Fixed rather than "the
#: whole object", so a future agent adding a field cannot grow ADL's stored
#: snapshot without ADL being taught what the field means.
CYCLE_COUNTS = ("scanned", "offered", "uploaded", "failed")

#: How many links and volumes one heartbeat may describe. A bound, not a
#: budget: the numbers are per station and per disk, and neither is large.
MAX_CYCLE_LINKS = 500
MAX_VOLUMES = 32


@dataclass(frozen=True)
class Heartbeat:
    """One report, checked and in native types."""

    app_version: str = ""
    os_version: str = ""
    uptime_seconds: Optional[int] = None
    #: The machine's own clock at the moment it built this report. The
    #: server never trusts it for anything but the skew it computes from it.
    device_time: Optional[datetime] = None
    backlog_count: Optional[int] = None
    last_cycle_completed_at: Optional[datetime] = None
    links: Tuple[dict, ...] = field(default_factory=tuple)
    volumes: Tuple[dict, ...] = field(default_factory=tuple)

    def details(self):
        """The half of the report that has no column of its own.

        Decision #264 keeps the snapshot bounded: typed columns for what the
        fleet listing and the health ladder read, and one JSON blob for the
        rest -- never a heartbeat history table.

        Written here and read by :func:`read_details`, which is the only
        other place that knows these keys.
        """
        return {
            "uptime_seconds": self.uptime_seconds,
            "backlog_count": self.backlog_count,
            "links": list(self.links),
            "volumes": list(self.volumes),
        }


def read_details(stored):
    """The stored blob, in the shape :meth:`Heartbeat.details` wrote it.

    The blob is JSON on a row that has outlived at least one agent release
    by the time anyone reads it, so every key is defaulted rather than
    assumed. Written as the counterpart of ``details()`` so that renaming a
    key is one edit in one file, not a template that silently empties.
    """
    stored = stored or {}

    return {
        "uptime_seconds": stored.get("uptime_seconds"),
        "backlog_count": stored.get("backlog_count"),
        "links": stored.get("links") or [],
        "volumes": stored.get("volumes") or [],
    }


def _invalid(detail):
    return AgentRequestRejected("invalid_heartbeat", detail)


def read_heartbeat(payload):
    """Read a heartbeat request body, or refuse it."""
    if not isinstance(payload, dict):
        raise _invalid(_("Send an object describing the machine."))

    cycle = payload.get("last_cycle") or {}

    if not isinstance(cycle, dict):
        raise _invalid(_("last_cycle must be an object."))

    return Heartbeat(
        app_version=_text(payload.get("app_version"), "app_version",
                          MAX_VERSION_LENGTH),
        os_version=_text(payload.get("os_version"), "os_version",
                         MAX_OS_VERSION_LENGTH),
        uptime_seconds=_count(payload.get("uptime_seconds"), "uptime_seconds"),
        device_time=_moment(payload.get("device_time"), "device_time"),
        backlog_count=_count(payload.get("backlog_count"), "backlog_count"),
        last_cycle_completed_at=_moment(cycle.get("completed_at"),
                                        "last_cycle.completed_at"),
        links=_links(cycle.get("links")),
        volumes=_volumes(payload.get("disk")),
    )


def _text(value, field_name, limit):
    if value is None:
        return ""

    if not isinstance(value, str):
        raise _invalid(_("%(field)s must be text.") % {"field": field_name})

    # Truncated rather than refused: a version string longer than any real
    # one is odd, but it is not a reason to stop believing the machine is
    # alive, and the column is what bounds it.
    return value.strip()[:limit]


def _count(value, field_name):
    """A non-negative whole number, or ``None`` when nothing was reported."""
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(_("%(field)s must be a number.") % {"field": field_name})

    number = int(value)

    if number < 0:
        raise _invalid(
            _("%(field)s cannot be negative.") % {"field": field_name}
        )

    return number


def _moment(value, field_name):
    """An aware datetime, or ``None`` when nothing was reported.

    A naive timestamp is read as UTC, exactly as a manifest entry's mtime is:
    an agent that forgets its offset should show up as skewed, not stop being
    able to report at all.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = parse_datetime(str(value))
        except ValueError:
            parsed = None

    if parsed is None:
        raise _invalid(
            _("%(field)s must be an ISO 8601 date and time.")
            % {"field": field_name}
        )

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_timezone.utc)

    return parsed


def _links(value):
    """The last cycle's per-station work, one entry per station link.

    This is where "the machine is up but doing nothing" becomes visible per
    station rather than only per machine: an operator looking at a country
    can see that one folder was scanned and another was not.
    """
    if value is None:
        return ()

    if not isinstance(value, list):
        raise _invalid(_("last_cycle.links must be a list."))

    if len(value) > MAX_CYCLE_LINKS:
        raise _invalid(
            _("Report at most %(limit)s station links per heartbeat.")
            % {"limit": MAX_CYCLE_LINKS}
        )

    links = []

    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise _invalid(
                _("last_cycle.links[%(index)s] must be an object.")
                % {"index": index}
            )

        station_link_id = _count(raw.get("station_link_id"), "station_link_id")

        if station_link_id is None:
            # The only required field anywhere in a heartbeat: a row of
            # counts nobody can attribute to a station is not a report, it
            # is noise in the snapshot an operator reads.
            raise _invalid(
                _("last_cycle.links[%(index)s] must name its station_link_id.")
                % {"index": index}
            )

        entry = {"station_link_id": station_link_id}
        entry.update({
            name: _count(raw.get(name), name) for name in CYCLE_COUNTS
        })

        # The message the agent has for this station, if it has one: a folder
        # that is not there, a pattern that matches nothing it can read.
        entry["error"] = _text(raw.get("error"), "error", 500) or None

        links.append(entry)

    return tuple(links)


def _volumes(value):
    """Free space on the volumes the machine watches.

    Kept because the failure it predicts is invisible from ADL: a country
    server whose disk fills stops being able to write the very files it is
    meant to be sending, and nothing about that reaches ADL as an error.
    """
    if value is None:
        return ()

    if not isinstance(value, list):
        raise _invalid(_("disk must be a list of volumes."))

    if len(value) > MAX_VOLUMES:
        raise _invalid(
            _("Report at most %(limit)s volumes per heartbeat.")
            % {"limit": MAX_VOLUMES}
        )

    volumes = []

    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise _invalid(
                _("disk[%(index)s] must be an object.") % {"index": index}
            )

        volumes.append({
            "volume": _text(raw.get("volume"), "volume", 100),
            "free_bytes": _count(raw.get("free_bytes"), "free_bytes"),
            "total_bytes": _count(raw.get("total_bytes"), "total_bytes"),
        })

    return tuple(volumes)
