"""
Propose and ack: what a device offers, and what ADL asks it to send.

The agent keeps no record of what it has already delivered -- the vendor's
folder is its only state, and a folder cannot remember. So each cycle it
offers what it can see and is told which of those files to send
(decision #266). Everything ADL needs to answer is in one place, the file
ledger, and the answer is a diff against it: a name ADL has never seen, or a
name whose bytes now hash differently, is a file ADL wants.

Reading this module, note what it does *not* do: it never writes. A proposal
is not an arrival, and a cycle interrupted between the manifest and the
uploads must leave ADL believing it holds exactly what it held before.
"""

from dataclasses import dataclass, field

from django.utils.translation import gettext as _

from .entries import parse_entries
from .errors import AgentRequestRejected
from .limits import MANIFEST_PAGE_LIMIT
from .models import AgentStationDataFile, AgentStationLink


def read_manifest(payload):
    """The candidate files in a manifest request body.

    Raises :class:`~adl_agent_plugin.errors.AgentRequestRejected` if the body
    is not a manifest, holds more than one page, or describes a file in a way
    ADL cannot read.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("files", []), list):
        raise AgentRequestRejected(
            "invalid_body",
            _("Send an object with a \"files\" list of candidate files."),
        )

    raw_entries = payload.get("files", [])

    if len(raw_entries) > MANIFEST_PAGE_LIMIT:
        # Refused, never truncated: an agent told about the first five hundred
        # of its files would take ADL's silence about the rest for "already
        # held" and never offer them again.
        raise AgentRequestRejected(
            "manifest_too_large",
            _("Offer at most %(limit)s files per manifest, in pages.")
            % {"limit": MANIFEST_PAGE_LIMIT},
            limit=MANIFEST_PAGE_LIMIT,
        )

    return parse_entries(raw_entries)


@dataclass
class ManifestDiff:
    """What ADL made of a manifest.

    ``unknown`` and ``disabled`` are the two ways a device working from a
    cached configuration can offer files ADL will not take: a station link
    that is no longer its own (or never was), and one an administrator has
    switched off centrally. Both are reported rather than raised, because one
    stale entry must not cost a machine the rest of its cycle.
    """

    requested: list = field(default_factory=list)
    unknown: list = field(default_factory=list)
    disabled: list = field(default_factory=list)


def diff_against_ledger(device, entries):
    """Which of ``entries`` ADL wants, and which it could not consider."""
    links = {link.pk: link for link in AgentStationLink.for_device(device)}

    diff, considered = ManifestDiff(), []

    for entry in entries:
        link = links.get(entry.station_link_id)

        if link is None:
            _remember(diff.unknown, entry.station_link_id)
        elif not link.enabled:
            _remember(diff.disabled, entry.station_link_id)
        else:
            considered.append(entry)

    held = AgentStationDataFile.held_hashes(
        (entry.station_link_id, entry.name) for entry in considered
    )

    diff.requested = [
        entry for entry in considered
        if held.get((entry.station_link_id, entry.name)) != entry.content_hash
    ]

    return diff


def _remember(ids, station_link_id):
    if station_link_id not in ids:
        ids.append(station_link_id)
