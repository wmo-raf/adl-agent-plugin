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

#: Per-pass counts kept from each station's line of a completed pass. Fixed,
#: exactly as ``CYCLE_COUNTS`` is and for the same reason: a future agent
#: adding a field cannot grow ADL's stored row without ADL being taught what
#: the field means.
PASS_COUNTS = (
    "scanned", "held", "offered", "wanted", "uploaded", "failed", "backlog",
)

#: How many finished passes one heartbeat may carry. A machine catching up
#: after a long outage empties its queue over several beats rather than in one
#: body ADL has to hold whole; the agent bounds its own side to the same
#: number.
MAX_COMPLETED_PASSES = 200

#: How many stations one pass may describe. A pass covers a unit -- a folder
#: and whatever shares it -- so in the field this is a handful; the bound is
#: against a machine sending nonsense, not against a real folder.
MAX_PASS_STATIONS = 200

#: How many named files a pass may say did not arrive. Three is what the agent
#: sends; the extra room costs nothing and means a slightly more generous
#: agent is trimmed rather than refused.
MAX_MISSING_FILES = 5

#: What a file that did not arrive was doing. Anything else is dropped rather
#: than stored: this vocabulary is what the admin filters and reads, and a
#: word ADL has never heard of is not something it can show anybody.
MISSING_OUTCOMES = ("failed", "held", "unmatched")

#: What started a pass. Same reasoning as the outcomes above -- a trigger ADL
#: cannot label is stored as the empty string rather than as a word from an
#: agent nobody here has read.
PASS_TRIGGERS = ("scheduled", "reconciliation", "collect")


@dataclass(frozen=True)
class CyclePass:
    """One unit pass, checked, as the machine finished it.

    A pass is a folder group walked once: what it looked in, what each of its
    stations did, and a few of the files it saw and did not deliver. The
    counts here are the same ones ``last_cycle`` carries, and the difference
    is entirely one of lifetime -- ``last_cycle`` is a snapshot the next beat
    overwrites, and a pass is a row.
    """

    at: Optional[datetime] = None
    seconds: Optional[float] = None
    unit: str = ""
    trigger: str = ""
    completed: bool = False
    stopped: str = ""
    folders: Optional[int] = None
    stations: Tuple[dict, ...] = field(default_factory=tuple)
    #: Up to a few files this pass saw and ADL never received, with the
    #: reason. ADL stores the name of every file it *did* receive; this is
    #: the negative space, and the difference between "this station is quiet"
    #: and "this station is quiet because the files are now called something
    #: else".
    missing: Tuple[dict, ...] = field(default_factory=tuple)


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
    #: The passes that finished on the machine since the last beat ADL
    #: accepted -- ``None`` from an agent too old to have the field at all,
    #: which is a normal long-lived state and not a fault, and an empty tuple
    #: from a new one that simply had nothing to say. The two are different
    #: instructions: the first means "fall back to ``last_cycle``", the second
    #: means "this machine has already told you everything".
    passes: Optional[Tuple[CyclePass, ...]] = None
    #: Passes the machine made and could not keep, because ADL was
    #: unreachable for longer than its queue is deep. Recorded rather than
    #: left as an unexplained gap in the history.
    dropped_passes: Optional[int] = None

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
            # Not a history -- the history is rows, in ``AgentCyclePass``.
            # This is the last beat's word on how much of it never got here,
            # which belongs beside the rest of what the machine last said.
            "dropped_passes": self.dropped_passes,
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
        "dropped_passes": stored.get("dropped_passes"),
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
        passes=_passes(payload.get("completed_passes")),
        dropped_passes=_count(payload.get("dropped_passes"),
                              "dropped_passes"),
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


def _passes(value):
    """The unit passes that finished on the machine since the last beat.

    The history ``last_cycle`` cannot be. A beat overwrites the snapshot, so
    before this ADL held exactly one cycle's worth of what a machine had been
    doing and threw away the one before it -- which is the right shape for
    liveness and the wrong one for "what has this station been doing for the
    last fortnight" (wmo-raf/adl#307).

    A fixed set of fields is read from each pass, never the whole object, for
    the same reason ``CYCLE_COUNTS`` is fixed: an agent is on the other end of
    this, it updates itself, and a field ADL has not been taught the meaning
    of is a column ADL cannot show anybody.

    Absent is normal and permanent, not a fault. Agents auto-update through
    the release feed and ADL instances are upgraded by a person, one country
    at a time, so an old agent talking to this plugin is a long-lived state --
    and what it sends instead is ``last_cycle``, which
    :meth:`AgentDevice.record_heartbeat` turns into one pass per beat.
    """
    if value is None:
        # Absent, not empty. See ``Heartbeat.passes``.
        return None

    if not isinstance(value, list):
        raise _invalid(_("completed_passes must be a list."))

    if len(value) > MAX_COMPLETED_PASSES:
        raise _invalid(
            _("Report at most %(limit)s completed passes per heartbeat.")
            % {"limit": MAX_COMPLETED_PASSES}
        )

    return tuple(_pass(raw, index) for index, raw in enumerate(value))


def _pass(raw, index):
    if not isinstance(raw, dict):
        raise _invalid(
            _("completed_passes[%(index)s] must be an object.")
            % {"index": index}
        )

    where = "completed_passes[%s]" % index
    stopped = _text(raw.get("stopped"), f"{where}.stopped", 500)

    return CyclePass(
        at=_moment(raw.get("at"), f"{where}.at"),
        seconds=_seconds(raw.get("seconds"), f"{where}.seconds"),
        # The folder the unit is named by. There is no stable unit id
        # anywhere in this product and inventing one to store would be
        # inventing a fact.
        unit=_text(raw.get("unit"), f"{where}.unit", 500),
        trigger=_word(raw.get("trigger"), f"{where}.trigger", PASS_TRIGGERS),
        completed=_completed(raw.get("completed"), f"{where}.completed",
                             stopped),
        stopped=stopped,
        folders=_count(raw.get("folders"), f"{where}.folders"),
        stations=_pass_stations(raw.get("stations"), where),
        missing=_missing(raw.get("missing"), where),
    )


def _pass_stations(value, where):
    """Each station's share of one pass, one entry per station link."""
    if value is None:
        return ()

    if not isinstance(value, list):
        raise _invalid(
            _("%(field)s.stations must be a list.") % {"field": where}
        )

    if len(value) > MAX_PASS_STATIONS:
        raise _invalid(
            _("Report at most %(limit)s stations per pass.")
            % {"limit": MAX_PASS_STATIONS}
        )

    stations = []

    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise _invalid(
                _("%(field)s.stations[%(index)s] must be an object.")
                % {"field": where, "index": index}
            )

        station_link_id = _count(raw.get("station_link_id"), "station_link_id")

        if station_link_id is None:
            # The same rule the cycle snapshot has: counts nobody can
            # attribute to a station are not a report.
            raise _invalid(
                _("%(field)s.stations[%(index)s] must name its "
                  "station_link_id.")
                % {"field": where, "index": index}
            )

        station = {"station_link_id": station_link_id}
        station.update({
            name: _count(raw.get(name), name) for name in PASS_COUNTS
        })
        station["error"] = _text(raw.get("error"), "error", 500)

        stations.append(station)

    return tuple(stations)


def _missing(value, where):
    """The files this pass saw and ADL never received.

    Held back as still being written, failed on the way, or sitting in the
    folder matching nobody's pattern. That last one is the reason the field
    exists: a vendor that renamed its files looks, from every other number in
    this product, exactly like a folder with nothing in it.
    """
    if value is None:
        return ()

    if not isinstance(value, list):
        raise _invalid(
            _("%(field)s.missing must be a list.") % {"field": where}
        )

    if len(value) > MAX_MISSING_FILES:
        raise _invalid(
            _("Report at most %(limit)s missing files per pass.")
            % {"limit": MAX_MISSING_FILES}
        )

    files = []

    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise _invalid(
                _("%(field)s.missing[%(index)s] must be an object.")
                % {"field": where, "index": index}
            )

        name = _text(raw.get("name"), "name", 255)

        if not name:
            # A file with no name is the shape of an answer without being
            # one, and this field is nothing but names.
            continue

        files.append({
            "name": name,
            "outcome": _word(
                raw.get("outcome"), "outcome", MISSING_OUTCOMES),
            "reason": _text(raw.get("reason"), "reason", 255),
            "station_link_id": _count(
                raw.get("station_link_id"), "station_link_id"),
        })

    return tuple(files)


def _completed(value, field_name, stopped):
    """Whether the pass ran to its end.

    Read together with the reason it stopped, because the two are one fact in
    two parts and the agent builds them as one: a pass marked finished with a
    reason for stopping, or cut short with none, is a record that contradicts
    itself. So an agent that omits the flag is read by the sentence beside it
    rather than defaulted -- a plain ``False`` would file every such pass
    under "cut short", which is the listing an operator opens to find the
    machines in trouble.
    """
    if value is None:
        return not stopped

    if not isinstance(value, bool):
        raise _invalid(
            _("%(field)s must be true or false.") % {"field": field_name}
        )

    return value


def _seconds(value, field_name):
    """How long something took, or ``None``. Never negative."""
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(_("%(field)s must be a number.") % {"field": field_name})

    if value < 0:
        raise _invalid(
            _("%(field)s cannot be negative.") % {"field": field_name}
        )

    return float(value)


def _word(value, field_name, vocabulary):
    """One of a closed set of words, or the empty string.

    Dropped rather than refused when it is not one of them. These vocabularies
    are what the admin filters and labels, so a word this ADL has never heard
    of is not something it can show anybody -- but an agent using one is a
    newer agent, not a broken one, and refusing its beat would cost the
    liveness signal to save a label.
    """
    text = _text(value, field_name, 50)

    return text if text in vocabulary else ""


def passes_from_last_cycle(beat, now):
    """One pass, built from the rolling snapshot an old agent sends.

    What an agent too old to have :attr:`Heartbeat.passes` sends instead, read
    as the coarsest honest history: the counts are real and the gaps are the
    beats. An operator who upgrades ADL before the fleet has caught up should
    see a coarser history, not an empty one.

    Deliberately not deduplicated. A machine beating every five minutes on a
    ten-minute cycle sends the same snapshot twice, and this stores the same
    counts twice -- which is what "one pass per beat" means, and is the honest
    reading of what arrived.

    The unit is blank because an old agent does not say which folder its
    counts came from, and the trigger with it: neither is a fact ADL has.
    """
    if not beat.links:
        return ()

    return (
        CyclePass(
            at=beat.last_cycle_completed_at or now,
            unit="",
            trigger="",
            completed=True,
            stations=tuple(
                {
                    "station_link_id": link["station_link_id"],
                    "scanned": link.get("scanned"),
                    "offered": link.get("offered"),
                    "uploaded": link.get("uploaded"),
                    "failed": link.get("failed"),
                    "error": link.get("error") or "",
                }
                for link in beat.links
            ),
        ),
    )


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
