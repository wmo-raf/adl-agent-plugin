"""
Keeping disk bounded without losing ADL's memory of a file.

A country sending a file every ten minutes fills a disk; keeping every byte
forever is not an option an instance has. But the ledger row that remembers a
file is not optional at all -- it is the whole reason a stateless agent stops
offering what it has already delivered, and a row pruned is a file eternally
new and re-uploaded forever (decision #268, story 22).

So the sweep here drops one and never the other. Bytes go; the row, its hash,
and what ADL made of the file stay. What is lost is only ADL's ability to
re-read the file for itself -- and even that is recoverable, because the
machine that sent it still has it: see ``reprocessing``.

The policy lives on the connection, one retention period per vendor per
machine, because that is where the shape of the data is known. What is
*never* pruned is stated in :meth:`AgentStationDataFile.prunable_for` and
tested at this module's own entry point.
"""

import logging

from .models import AgentConnection, AgentStationDataFile

logger = logging.getLogger(__name__)


def prune_connection(connection):
    """Drop the expired staged bytes on one connection.

    Returns how many files were pruned. Each file is its own unit of work: a
    row that cannot be written -- deleted underneath the sweep, or a database
    hiccup -- costs that file and not the rest of the country's backlog. There
    is nothing to retry and nothing to record on the row, because the row is
    not what is wrong; the next sweep finds it again.
    """
    pruned = 0

    for data_file in AgentStationDataFile.prunable_for(connection).iterator():
        try:
            if data_file.prune_bytes():
                pruned += 1
        except Exception as e:
            logger.error(
                "Could not prune the staged bytes of %s on %s: %s",
                data_file.file_name, connection.name, e,
            )

    if pruned:
        logger.info(
            "Pruned the staged bytes of %s file(s) on %s, kept in the ledger.",
            pruned, connection.name,
        )

    return pruned


def prune_expired_files():
    """Drop every connection's expired staged bytes. Returns the total."""
    total = 0

    for connection in AgentConnection.objects.all().iterator():
        total += prune_connection(connection)

    return total
