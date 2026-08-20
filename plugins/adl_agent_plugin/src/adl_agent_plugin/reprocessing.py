"""
Applying a fix to data that has already arrived.

A decoder is fixed, a mapping is corrected, a CSV configuration turns out to
name the wrong column -- and the files it all applies to came in weeks ago.
Story 21 is the way back, and its whole design is that the operator asks for
files, not for a *method*: which of the two routes a file takes is the row's
answer, not a question put to someone who would have to know where a
retention sweep had got to.

**The bytes are still staged.** ADL re-decodes them itself. The machine in the
field hears nothing about it and is asked for nothing -- which is the point of
staging bytes at all.

**The bytes have been pruned.** The only copy left is on the vendor's disk, so
ADL forgets the file's hash; the machine offers the file on its next manifest,
is told to send it, and the upload resets the row on arrival. Two things make
that reach: no hash an agent can compute equals nothing, so a cleared hash is
a request; and the same cleared hash pulls the station's scan floor back down
to the file, so a machine is not asked for something outside the window it
looks at (see ``AgentStationLink.manifest_watermark``).

Both routes end in the ordinary drain. Nothing here decodes anything.
"""

import logging
from dataclasses import dataclass, field

from .models import (
    REPROCESS_REDECODED,
    REPROCESS_REOFFERED,
    AgentConnection,
    AgentDevice,
)
from .tasks import nudge

logger = logging.getLogger(__name__)


@dataclass
class ReprocessOutcome:
    """Which files went which way, so the operator can be told."""

    redecoded: list = field(default_factory=list)
    reoffered: list = field(default_factory=list)

    def __len__(self):
        return len(self.redecoded) + len(self.reoffered)


def reprocess(data_files):
    """Ask ADL to make observations of these files again.

    Returns a :class:`ReprocessOutcome` saying which files ADL will re-read
    for itself and which their machines are being asked for.

    Two things follow the writes, and both are about the request actually
    landing rather than about correctness:

    - the connections whose files are waiting again are nudged, so a re-decode
      is seconds away rather than an interval away -- exactly as an upload
      nudges them;
    - the devices being asked for bytes have their config version moved, so an
      agent holding a cached configuration re-reads the scan floor that was
      just lowered for it instead of scanning past the file it is wanted for.
    """
    outcome = ReprocessOutcome()
    to_drain, to_ask = set(), set()

    for data_file in data_files:
        result = data_file.request_reprocess()
        # The id off the station link rather than the connection itself:
        # ``network_connection`` is polymorphic, so reading it per row is a
        # query per row, and a decoder fix is pressed a hundred rows at a
        # time. The connections are read once, below.
        connection_id = data_file.station_link.network_connection_id

        if result == REPROCESS_REDECODED:
            outcome.redecoded.append(data_file)
            to_drain.add(connection_id)
        elif result == REPROCESS_REOFFERED:
            outcome.reoffered.append(data_file)
            to_ask.add(connection_id)

    for connection in AgentConnection.objects.filter(pk__in=to_drain):
        nudge(connection)

    device_ids = AgentConnection.objects.filter(pk__in=to_ask).values_list(
        "device_id", flat=True
    ).distinct()

    for device_id in device_ids:
        AgentDevice.bump_config_version_for(device_id)

    logger.info(
        "Re-process requested for %s file(s): %s to be decoded again, "
        "%s asked for from their machines.",
        len(outcome), len(outcome.redecoded), len(outcome.reoffered),
    )

    return outcome
