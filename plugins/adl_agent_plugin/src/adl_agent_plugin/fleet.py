"""
Noticing that a machine has stopped.

Everything else in this plugin is driven by a machine calling in. Silence is
the one thing that cannot be: an offline server sends no request, so nothing
would ever run to observe that it has gone quiet, and its state would sit at
whatever it was when it last spoke. So one sweep runs on ADL's own clock and
asks the whole fleet the same question the heartbeat endpoint asks of one
device -- what does this machine's last report say about it now?

The sweep records nothing when nothing has changed (decision #264). A country
that has been offline all weekend has one row saying so, not one every minute.
"""

import logging
from datetime import timedelta

from django.utils import timezone as dj_timezone

from .health import SOURCE_EVIDENCE_REFRESH_SECONDS
from .models import AgentConnection, AgentDevice, AgentDeviceStateTransition

logger = logging.getLogger(__name__)


def sweep_liveness(now=None):
    """Re-read every paired machine's state, logging the ones that moved.

    Returns the number of devices whose state changed. Each device is its own
    unit of work: one row that cannot be written -- deleted underneath the
    sweep, a database hiccup -- costs that device and not the rest of the
    fleet.

    Only paired devices are asked. An unpaired one has no machine behind it
    to be alive or dead, and sweeping it would fill the transitions log of a
    device that has never existed.
    """
    now = now or dj_timezone.now()
    changed = 0

    for device in AgentDevice.objects.active().iterator():
        before = device.liveness_state

        try:
            liveness = device.record_liveness(now)
        except Exception:
            logger.exception(
                "Could not record the liveness of agent device %s", device.pk,
            )
            continue

        if liveness.state != before:
            changed += 1
            logger.info(
                "[AGENT FLEET] %s: %s -> %s",
                device.name, before or "-", liveness.state,
            )

    return changed


def publish_source_evidence(now=None):
    """Put each connection's current liveness verdict where core reads it.

    Core's ingestion diagnostic reads layer 5 from stored probe results, and
    it never runs a probe itself: external layers are on-demand only, because
    dialling partner hosts on a timer across twenty-six deployments risks
    getting ADL's addresses banned. That reasoning is about network calls,
    and this check makes none -- it reads columns on a row the heartbeat
    endpoint wrote. So the one thing the rule protects is not at stake here,
    and running it on a clock is what turns "press this button to find out"
    into "the fleet's state is simply on the page" (story 23).

    Rows are written only when the machine's state has moved since the last
    one, or when the standing row is about to age out of core's
    fifteen-minute freshness window -- so a settled fleet costs a few rows an
    hour rather than one a minute, and this stays a published verdict rather
    than becoming the heartbeat history decision #264 refused to keep.

    Run after :func:`sweep_liveness`, which is what has just moved the states
    this reads.

    Returns how many rows were written. Each connection is its own unit of
    work: one that cannot be written costs that connection, not the fleet.
    """
    from adl.core.source_checks import (
        CHECK_SOURCE,
        normalise_source_check_result,
    )
    from adl.monitoring.constants import PROBE_LAYER_IDS
    from adl.monitoring.models import SourceProbeResult

    now = now or dj_timezone.now()
    written = 0

    connections = (AgentConnection.objects
                   .select_related("device")
                   .filter(plugin_processing_enabled=True))

    for connection in connections.iterator():
        try:
            if not _worth_writing(_standing_row(connection), connection.device, now):
                continue

            result = normalise_source_check_result(connection.check_source())

            SourceProbeResult.objects.create(
                connection=connection,
                station_link=None,
                check_id=CHECK_SOURCE,
                layer=PROBE_LAYER_IDS[5],
                status=result.status,
                category=result.category,
                message=result.message,
                # No network call was made, so there is no round trip to
                # time. Left null rather than reported as zero, which would
                # read as an implausibly fast one.
                latency_ms=None,
                at=now,
            )
            written += 1
        except Exception:
            logger.exception(
                "Could not publish the source verdict of agent connection %s",
                connection.pk,
            )

    return written


def _standing_row(connection):
    """The verdict core is currently reading for this connection."""
    from adl.core.source_checks import CHECK_SOURCE
    from adl.monitoring.models import SourceProbeResult

    return (SourceProbeResult.objects
            .filter(connection=connection, station_link__isnull=True,
                    check_id=CHECK_SOURCE)
            .order_by("-at")
            .first())


def _worth_writing(standing, device, now):
    """Whether this sweep has something to say that the stored row does not.

    Two reasons to write, and the first is why ``liveness_since`` exists: it
    is the moment this machine's state last moved, so a standing row older
    than it is a row describing a machine that has since changed. Comparing
    the *messages* would not do -- they carry ages ("last heard from eleven
    minutes ago"), so every sweep would look like news and this table would
    become the heartbeat history decision #264 refused to keep.

    The second is age: core stops trusting a probe result after fifteen
    minutes, so an unchanged verdict is restated well inside that or the
    connection's source layer falls back to "not recently checked".
    """
    if standing is None:
        return True

    if device.liveness_since is not None and standing.at < device.liveness_since:
        return True

    return (now - standing.at).total_seconds() >= SOURCE_EVIDENCE_REFRESH_SECONDS


def prune_state_transitions(now=None):
    """Drop state changes older than the retention period. Returns the count.

    The transitions log is a history, not an archive: ninety days is long
    enough to answer "was this machine flapping last month" and short enough
    that a fleet nobody has looked at in years does not grow without bound.
    The period matches the one core keeps its own connection-health
    transitions for, so an operator reading both sees the same horizon.
    """
    now = now or dj_timezone.now()
    cutoff = now - timedelta(days=AgentDeviceStateTransition.RETENTION_DAYS)

    deleted, _details = AgentDeviceStateTransition.objects.filter(
        at__lt=cutoff
    ).delete()

    return deleted
