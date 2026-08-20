"""
Draining without waiting for the clock.

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

from adl.core.tasks import INGESTION_QUEUE_NAME
from celery import shared_task
from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger(__name__)

#: How long a drain waits after being asked for, and how long the latch that
#: coalesces a burst of uploads lives. Long enough that a station uploading
#: several files is drained once; short enough that "within seconds of
#: arrival" is true.
NUDGE_DELAY_SECONDS = 5


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
