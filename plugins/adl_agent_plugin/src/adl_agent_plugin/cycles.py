"""
What each machine's collection has been doing, kept long enough to answer for.

ADL held exactly one cycle's worth of agent history and overwrote it every
five minutes: ``AgentDevice.heartbeat_details`` carries the last beat's counts
and the beat before it is gone. That is the right shape for liveness
(decision #264) and the wrong one for the question an operator actually has,
which is "what has this station been doing for the last fortnight"
(wmo-raf/adl#307).

This module is the other half. The heartbeat now carries the unit passes that
finished on the machine since the last beat ADL accepted, and each station's
share of each pass becomes a row in :class:`AgentCyclePass` -- a hypertable,
because these are exactly the repetitive small-integer columns TimescaleDB's
columnar compression eats.

What bounds the table
---------------------

Not filtering. Every pass is stored, including the uneventful ones, for the
reason set out on the model: filtering saves rows only on quiet stations,
which are precisely the ones where "the agent looked and there was nothing" is
the fact worth having.

Time bounds it instead, and the volume is a single country's stations rather
than the fleet's -- each NMHS runs its own instance. At two hundred station
links on a ten-minute interval that is some twenty-nine thousand rows a day,
around a gigabyte a year raw and well under a tenth of that once compressed.

Both policies are deployment-wide settings read from the environment, the way
``ADL_AGENT_CONCURRENT_UPLOADS`` and ``ADL_AGENT_CYCLE_STUCK_MULTIPLIER``
already are: how long a country keeps its diagnostic history is a decision
about that country's disk, not about any one machine.
"""

import logging
import os

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone as dj_timezone

from .models import AgentCyclePass, AgentStationLink

logger = logging.getLogger(__name__)

#: When a chunk is turned into columns. A week, so that the days anybody
#: actually opens at speed are still row-shaped, and everything behind them
#: costs a tenth of the disk.
DEFAULT_COMPRESS_AFTER_DAYS = 7

#: When a chunk is dropped. A quarter, which is the same window
#: ``DEFAULT_FILE_RETENTION_DAYS`` gives a staged file -- long enough that a
#: fault noticed at the end of a rainy season can still be traced back through
#: the passes that made it.
DEFAULT_RETENTION_DAYS = 90

COMPRESS_AFTER_SETTING = "ADL_AGENT_CYCLE_COMPRESS_AFTER_DAYS"
RETENTION_SETTING = "ADL_AGENT_CYCLE_RETENTION_DAYS"


def _days(name, default):
    """One configured number of days, or the default if it is nonsense.

    Django settings first, the environment second, exactly as every other
    operator-settable number in this plugin is read. A deployment that
    mistypes one keeps ADL's default rather than getting an instance that will
    not start.
    """
    raw = getattr(settings, name, None)

    if raw is None:
        raw = os.environ.get(name)

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default

    # Zero is not "switch it off": a zero-day compression policy would
    # compress the chunk being written to, and a zero-day retention policy
    # would drop today's passes. Both are nonsense rather than a choice.
    return value if value >= 1 else default


def compress_after_days():
    """How old a chunk is before it is turned into columns."""
    return _days(COMPRESS_AFTER_SETTING, DEFAULT_COMPRESS_AFTER_DAYS)


def retention_days():
    """How long a pass is kept before its chunk is dropped."""
    return _days(RETENTION_SETTING, DEFAULT_RETENTION_DAYS)


def apply_policies():
    """Put the configured compression and retention on the hypertable.

    Idempotent, and re-run on a schedule rather than only at migration time.
    A migration applies the numbers that were configured on the day it ran;
    these are settings, so an operator who changes one has to be able to see
    the change take without a migration to hang it on.

    Removing and re-adding rather than ``if_not_exists``: adding a policy that
    is already there is a no-op whatever interval it names, so an
    ``if_not_exists`` version would silently ignore every change after the
    first.

    Never raises. This is housekeeping on a diagnostic table; an instance
    whose policies could not be set keeps collecting, keeps recording passes,
    and says so in the log.
    """
    table = AgentCyclePass._meta.db_table
    compress_after = compress_after_days()
    retention = retention_days()

    statements = [
        # Segmented by station link because the query this table exists for is
        # one station's history, and a segment-by column is the one dimension
        # a compressed chunk can still be filtered on without decompressing.
        f"ALTER TABLE {table} SET ("
        f" timescaledb.compress,"
        f" timescaledb.compress_segmentby = 'station_link_id',"
        f" timescaledb.compress_orderby = 'time DESC'"
        f")",
        f"SELECT remove_compression_policy('{table}', if_exists => true)",
        f"SELECT add_compression_policy("
        f"'{table}', INTERVAL '{compress_after} days')",
        f"SELECT remove_retention_policy('{table}', if_exists => true)",
        f"SELECT add_retention_policy('{table}', INTERVAL '{retention} days')",
    ]

    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
    except Exception as e:
        logger.error(
            "Could not set the retention policies on %s: %s", table, e,
        )

        return False

    logger.info(
        "Agent cycle passes compress after %s day(s) and are dropped after "
        "%s.", compress_after, retention,
    )

    return True


def record_passes(device, beat, now=None):
    """Store what this beat said the machine has been doing. Returns the rows.

    Called from :meth:`AgentDevice.record_heartbeat`, after the beat's own
    columns are written and never before: a pass that cannot be stored must
    not cost the machine its liveness, which is the one fact the whole ladder
    rests on.

    Station links this ADL does not know are dropped rather than refused. A
    machine goes on collecting a station HQ unlinked an hour ago until its
    next sync, and a beat carrying a pass about it is a machine doing exactly
    what it was told to -- not a machine to argue with.
    """
    now = now or dj_timezone.now()

    passes = beat.passes

    if passes is None:
        # An agent too old to have the field at all. What it sends instead is
        # the rolling snapshot, and one pass per beat out of it is a coarser
        # history than a newer agent's but is emphatically better than none:
        # the counts are real, and the gaps are the beats.
        passes = _from_last_cycle(beat, now)

    if not passes:
        return []

    known = set(
        AgentStationLink.for_device(device).values_list("pk", flat=True)
    )

    rows = [
        row
        for unit_pass in passes
        for row in _rows(device, unit_pass, known, now)
    ]

    if not rows:
        return []

    try:
        # One statement for the whole beat. A machine catching up after an
        # outage sends many passes at once, and a row at a time would put a
        # country's heartbeat behind two hundred round trips to its own
        # database.
        with transaction.atomic():
            AgentCyclePass.objects.bulk_create(rows)
    except Exception as e:
        # A beat's first job is to say the machine is alive, and that is the
        # one fact the whole liveness ladder rests on. History is worth
        # having; it is not worth a country reading as offline because a
        # diagnostic table would not take a write.
        logger.error(
            "Could not store %s collection pass row(s) from %s: %s",
            len(rows), device.name, e,
        )

        return []

    return rows


def _rows(device, unit_pass, known, now):
    """One pass, as a row per station of it."""
    at = unit_pass.at or now
    missing = list(unit_pass.missing)

    for station in unit_pass.stations:
        station_link_id = station.get("station_link_id")

        if station_link_id not in known:
            continue

        yield AgentCyclePass(
            time=at,
            device=device,
            station_link_id=station_link_id,
            unit=unit_pass.unit,
            trigger=unit_pass.trigger,
            completed=unit_pass.completed,
            stopped=unit_pass.stopped,
            duration_ms=(
                None if unit_pass.seconds is None
                else int(unit_pass.seconds * 1000)
            ),
            folders_walked=unit_pass.folders,
            scanned=station.get("scanned"),
            held=station.get("held"),
            offered=station.get("offered"),
            wanted=station.get("wanted"),
            uploaded=station.get("uploaded"),
            failed=station.get("failed"),
            backlog=station.get("backlog"),
            error=station.get("error") or "",
            # The unit's list, on every station's row. A pass covers a folder
            # group, and the file that stopped matching anybody's pattern is
            # the unit's fact rather than one station's -- an unmatched file
            # belongs to no station by definition, which is what makes it
            # invisible everywhere else.
            missing_files=_missing_for(missing, station_link_id),
            received_at=now,
        )


def _missing_for(missing, station_link_id):
    """The files that did not arrive, as one station's row should carry them.

    This station's own, plus the ones that belong to no station -- which is
    what an unmatched file is, and the reason the whole field exists.
    """
    return [
        file for file in missing
        if file.get("station_link_id") in (None, station_link_id)
    ]


def _from_last_cycle(beat, now):
    """One pass, built from the rolling snapshot an old agent sends.

    Deliberately coarse and deliberately not deduplicated. A machine beating
    every five minutes on a ten-minute cycle sends the same snapshot twice, so
    this stores the same counts twice -- which is the honest reading of what
    arrived, and is what "one pass per beat" means. What it must not do is
    refuse the beat or store nothing: an operator upgrading ADL before the
    fleet has caught up should see a coarser history, not an empty one.

    The unit is blank because an old agent does not say which folder its
    counts came from, and the trigger with it: neither is a fact ADL has.
    """
    from .heartbeat import CyclePass

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
