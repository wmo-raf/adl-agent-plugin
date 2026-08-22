"""
When the agent plugin's own work happens.

Three things run on a clock here. The **fleet sweep** is the one that exists
purely because of what does *not* happen: an offline machine sends no
request, so noticing its silence has to be ADL's own initiative. The other
two pull in opposite directions -- the
**nudge** makes a drain happen sooner than the schedule would, because a
machine that has just uploaded has told ADL there is work. The nightly
**retention sweep** lets staged bytes go once nobody needs them any more; what
it prunes and what it must never touch is in ``retention``.

Draining without waiting for the clock
--------------------------------------

Celery Beat already runs every connection on its interval, and that pass is
the safety net: whatever a nudge misses, loses or arrives too early for is
picked up there. But an interval is the wrong latency for push-based
delivery. A machine that has just finished uploading has *told* ADL there is
work, and making it wait up to a quarter of an hour for the scheduler to
notice would throw away the whole point of inverting the direction (story 19).

So an upload asks for its connection to be drained now. Two things keep that
from becoming a stampede:

- **One nudge per burst.** A cycle uploads many files, and each one arriving
  is not a separate reason to drain. The first upload takes a short-lived
  latch and schedules the drain a few seconds out; every upload behind it
  finds the latch taken and simply lets that drain cover its file too. The
  latch expires as the drain runs, so a machine still uploading re-arms one
  for the files that came after. It is a damper, not an exact count: an
  upload landing in the moment between the latch expiring and the drain
  starting schedules a second one, which the per-station lock then absorbs.
- **One drain per station.** The nudge runs the ordinary ingestion path, so
  it takes the same per-station lock the scheduled pass takes. A nudge and a
  scheduled run landing together do not both process a file: the second finds
  the lock held and records a skip.
"""

import logging

from adl.config.celery import app
from adl.core.tasks import INGESTION_QUEUE_NAME
from celery import shared_task
from celery.schedules import crontab
from celery_singleton import Singleton
from django.core.cache import cache
from django.db import transaction

from .fleet import (
    prune_state_transitions,
    publish_source_evidence,
    sweep_liveness,
)
from .mirror import mirror_releases
from .retention import prune_expired_files

logger = logging.getLogger(__name__)

#: How long a drain waits after being asked for, and how long the latch that
#: coalesces a burst of uploads lives. Long enough that a station uploading
#: several files is drained once; short enough that "within seconds of
#: arrival" is true.
NUDGE_DELAY_SECONDS = 5

#: How often ADL re-reads what its fleet's last reports say. A minute: the
#: thresholds it enforces are whole heartbeats apart, so this is fine enough
#: that "offline after three missed" means what it says, and coarse enough
#: that a settled fleet costs one query a minute.
FLEET_SWEEP_SECONDS = 60


def nudge_latch_key(connection_id):
    """The cache key that says "a drain is already on its way for this one"."""
    return f"adl_agent_plugin:nudge:{connection_id}"


def nudge(connection):
    """Ask for ``connection`` to be drained shortly, if nobody already has.

    The task is queued on commit, not now. A worker is a separate process
    reading its own snapshot of the database, so a drain that started before
    the row committed would find nothing and the file would wait for the
    scheduler after all. Outside a transaction -- which is where the upload
    endpoint runs -- ``on_commit`` fires immediately, so this costs nothing in
    the ordinary case and is correct if the view is ever made atomic.

    Returns whether this call is the one that scheduled it -- which is what
    the tests read, and what makes the coalescing observable rather than
    something to be taken on trust.
    """
    if not cache.add(nudge_latch_key(connection.pk), "1", timeout=NUDGE_DELAY_SECONDS):
        return False

    connection_id = connection.pk

    transaction.on_commit(lambda: drain_agent_connection.apply_async(
        args=[connection_id],
        countdown=NUDGE_DELAY_SECONDS,
        queue=INGESTION_QUEUE_NAME,
    ))

    return True


@shared_task(name="adl_agent_plugin.tasks.drain_agent_connection")
def drain_agent_connection(connection_id):
    """Run one agent connection's ingestion, now.

    Deliberately the same entry point Celery Beat uses -- the connection's own
    ``collect_data`` -- so a nudged run and a scheduled run are the same run,
    with the same locking, the same activity logs and the same window
    resolution. A nudge is about *when*, never about *what*.
    """
    from .models import AgentConnection

    connection = AgentConnection.objects.filter(pk=connection_id).first()

    if connection is None:
        # Deleted between the upload and the drain. Its files went with it.
        logger.warning(
            "Agent connection %s no longer exists; nothing to drain.", connection_id,
        )
        return

    if not connection.plugin_processing_enabled:
        # Pausing a connection stops its files being processed, not its files
        # arriving -- that distinction is the point of pausing one.
        logger.info(
            "Agent connection %s is paused; its files stay staged.", connection.name,
        )
        return

    connection.collect_data()


@app.task(base=Singleton, bind=True)
def sweep_agent_fleet_liveness(self):
    """Notice which machines have gone quiet, and which have come back.

    Every minute, because the thresholds it enforces are counted in whole
    heartbeats and a sweep coarser than that would blur the boundary an
    operator is told about ("offline after three missed"). It costs one query
    over the device table plus a write for each machine that actually moved,
    which for a settled fleet is no writes at all.

    A singleton, like every other sweep in ADL: two of them running together
    would each see the other's pre-state and both append the same transition.

    Two steps, in this order. First work out where every machine stands and
    log the ones that moved; then publish those verdicts where the ingestion
    diagnostic reads them, so the connection health page shows what the fleet
    is doing without anyone pressing anything.
    """
    changed = sweep_liveness()

    if changed:
        logger.info("[AGENT FLEET] %s device(s) changed state", changed)

    published = publish_source_evidence()

    if published:
        logger.info("[AGENT FLEET] Published %s source verdict(s)", published)


@app.task(base=Singleton, bind=True)
def run_agent_fleet_retention(self):
    """Drop state changes older than the retention period."""
    logger.info("[AGENT FLEET] Pruning expired agent device state transitions")
    pruned = prune_state_transitions()
    logger.info("[AGENT FLEET] Pruned %s old state transition(s)", pruned)


@app.task(base=Singleton, bind=True)
def run_agent_file_retention(self):
    """Drop every connection's expired staged bytes (story 22).

    A singleton, like every other nightly sweep in ADL: it walks the whole
    ledger, and two of them running at once would be two workers deleting the
    same files.
    """
    logger.info("[AGENT RETENTION] Pruning expired staged agent files")
    pruned = prune_expired_files()
    logger.info("[AGENT RETENTION] Pruned the bytes of %s staged file(s)", pruned)


@app.task(base=Singleton, bind=True)
def run_agent_release_mirror(self):
    """Pull any new agent release from upstream into this instance.

    ADL is one deployment per country and an agent can only update from the
    instance it is paired with, so without this every NMHS would upload the
    same build by hand -- and a fleet would be current only where somebody
    had time. What arrives is staged, never published: this brings the
    release within reach of the country's machines, and an operator decides
    when they take it (see ``mirror``).

    A singleton because it downloads tens of megabytes; two of them would
    fetch the same release twice and race to store it.
    """
    result = mirror_releases()

    if result["mirrored"]:
        logger.info(
            "[AGENT RELEASES] Mirrored %s; publish in the admin when this "
            "fleet should move", ", ".join(result["mirrored"]),
        )

    if result["failed"]:
        logger.warning(
            "[AGENT RELEASES] Could not mirror %s", ", ".join(result["failed"]),
        )


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    # An hour after midnight rather than on it: core and the FTP plugin both
    # run their nightly cleanups at midnight, and a sweep that walks every
    # connection's ledger has no reason to queue behind them.
    sender.add_periodic_task(
        crontab(hour=1, minute=0),
        run_agent_file_retention.s(),
        name="run-agent-file-retention-daily",
    )

    # Ten past the same hour, behind the file sweep: the two are unrelated
    # and neither is urgent, and running them one after another keeps the
    # nightly work of this plugin in one window.
    sender.add_periodic_task(
        crontab(hour=1, minute=10),
        run_agent_fleet_retention.s(),
        name="run-agent-fleet-retention-daily",
    )

    # Daily, in the same quiet window as the other two. A release is
    # published rarely and installed on the fleet's own cadence, so checking
    # more often would buy hours at most -- and an instance that pulls a
    # hundred megabytes from upstream should do it while nobody is working.
    sender.add_periodic_task(
        crontab(hour=1, minute=20),
        run_agent_release_mirror.s(),
        name="run-agent-release-mirror-daily",
    )

    sender.add_periodic_task(
        FLEET_SWEEP_SECONDS,
        sweep_agent_fleet_liveness.s(),
        name="sweep-agent-fleet-liveness",
    )
