"""
What an agent is told, and in what shape.

One idea runs through every payload here: the two configuration tiers are
kept visibly apart on the wire. Anything under ``config`` is the machine's to
change and is exactly what the config endpoint accepts; anything under
``admin`` is HQ's, and travels only so the app can show it. An app author
never has to guess which is which, and neither does a reader of this file.

The wire form of a station link's own tier is built by the model
(``AgentStationLink.app_config``), so that a field's shape on the wire stays
beside the field's definition. This module assembles the rest.
"""

from .limits import MANIFEST_PAGE_LIMIT, MAX_UPLOAD_BYTES
from .models import AgentConnection, AgentStationDataFile, AgentStationLink


def device_summary(device):
    """What an agent is told about itself. Never includes a credential."""
    return {
        "id": device.pk,
        "name": device.name,
        "paired_at": device.paired_at,
        "last_seen_at": device.last_seen_at,
    }


def device_payload(device):
    """The device block of a sync response.

    Carries the check interval because the cadence is per machine, not per
    connection (decision #260): one loop scans every folder this device has
    been given.
    """
    return {
        **device_summary(device),
        "check_interval_minutes": device.check_interval_minutes,
    }


def station_link_payload(station_link, reoffer_point=None):
    station = station_link.station

    return {
        "id": station_link.pk,
        # How far back this station is worth offering files from. A floor,
        # never a high-water mark -- see manifest_watermark. What the ledger
        # contributes to it is read for the whole device at once, so a
        # machine with forty stations is still one query.
        "watermark": station_link.manifest_watermark(reoffer_point),
        "config": station_link.app_config(),
        "admin": {
            "enabled": station_link.enabled,
            "timezone": str(station_link.timezone),
            "start_date": station_link.start_date,
            "station": {
                "id": station.pk,
                "name": station.name,
                "station_id": station.station_id,
                "wigos_id": station.wigos_id,
            },
        },
    }


def connection_payload(connection, station_links, reoffer_points):
    return {
        "id": connection.pk,
        "name": connection.name,
        "admin": {
            "enabled": connection.plugin_processing_enabled,
            "network": connection.network.name,
        },
        "station_links": [
            station_link_payload(link, reoffer_points.get(link.pk))
            for link in station_links
        ],
    }


def limits_payload():
    """What an agent may send in one go.

    Handed out rather than assumed so that a fleet already installed in the
    field follows a change to these numbers without being reinstalled.
    """
    return {
        "manifest_entries": MANIFEST_PAGE_LIMIT,
        "file_bytes": MAX_UPLOAD_BYTES,
    }


def sync_payload(device):
    """Everything this device needs for a cycle, in one response.

    A disabled connection or station link is present and flagged rather than
    absent: the technician at the machine should be able to see that a
    station is switched off in ADL, instead of watching it disappear and
    wondering what they broke.
    """
    connections = list(
        AgentConnection.objects
        .filter(device=device)
        .select_related("network")
        .order_by("sort_order", "name")
    )

    links = _station_links_by_connection(connections)
    reoffer_points = AgentStationDataFile.reoffer_points_for_connections(
        connections
    )

    return {
        "config_version": device.current_config_version(),
        "limits": limits_payload(),
        "device": device_payload(device),
        "connections": [
            connection_payload(
                connection, links.get(connection.pk, []), reoffer_points
            )
            for connection in connections
        ],
    }


def config_write_payload(station_link, device):
    """The answer to a config write: what now stands, and at which version."""
    return {
        "station_link_id": station_link.pk,
        "config_version": device.current_config_version(),
        "config": station_link.app_config(),
    }


def _station_links_by_connection(connections):
    """Every link for these connections, in one query, grouped by connection.

    A device with two vendors and forty stations is one sync call, so the
    payload is assembled from two queries rather than forty-two.
    """
    grouped = {}

    if not connections:
        return grouped

    station_links = (
        AgentStationLink.objects
        .filter(network_connection__in=connections)
        .select_related("station", "network_connection")
        .order_by("station__name", "pk")
    )

    for station_link in station_links:
        grouped.setdefault(station_link.network_connection_id, []).append(station_link)

    return grouped


def manifest_payload(device, diff):
    """The answer to a manifest: send these, and here is what I ignored.

    ``requested`` echoes the hash each file was offered under, so the agent
    can match the answer to the candidate it proposed rather than re-reading
    the file to work out which version this is.

    The two lists of station link ids are not errors -- a machine works from
    a cached configuration, so it will sometimes offer files for a link that
    has been deleted, moved to another device, or switched off centrally.
    Saying which is the difference between a technician seeing "ADL is
    ignoring Garissa" and seeing nothing at all.
    """
    return {
        "config_version": device.current_config_version(),
        "limits": limits_payload(),
        "requested": [
            {
                "station_link_id": entry.station_link_id,
                "name": entry.name,
                "hash": entry.content_hash,
            }
            for entry in diff.requested
        ],
        "unknown_station_links": diff.unknown,
        "disabled_station_links": diff.disabled,
    }


def upload_payload(data_file, device):
    """The answer to an upload: what ADL now holds under that name."""
    return {
        "station_link_id": data_file.station_link_id,
        "name": data_file.file_name,
        "size": data_file.size,
        "hash": data_file.content_hash,
        "status": data_file.status,
        "received_at": data_file.received_at,
        "config_version": device.current_config_version(),
    }
