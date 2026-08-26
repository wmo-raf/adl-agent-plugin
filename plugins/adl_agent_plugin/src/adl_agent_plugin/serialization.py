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

from django.utils import timezone as dj_timezone

from .health import heartbeat_interval_minutes, station_stale_after_minutes
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

    And the heartbeat interval beside it, which is a different cadence for a
    different loop -- deliberately not derived from the check interval, since
    the two are separate precisely so that a wedged scan cycle still
    heartbeats (decision #264). It is the server's number, handed out rather
    than compiled into the agent, so a deployment can change how closely it
    watches its fleet without touching a machine in the field.

    ``dated_folder_window_hours`` is how far back a cycle walks the dated
    sub-folders of a station filed by date. Here rather than left to the
    agent's own default because the cost it bounds is this machine's:
    expanding a year of hourly folders is 8,760 directory listings every
    check interval, on exactly the country links this product exists for.
    """
    return {
        **device_summary(device),
        "check_interval_minutes": device.check_interval_minutes,
        "heartbeat_interval_minutes": heartbeat_interval_minutes(),
        "dated_folder_window_hours": device.dated_folder_window_hours,
    }


def station_link_payload(station_link, reoffer_point=None, last_received_at=None):
    station = station_link.station

    return {
        "id": station_link.pk,
        # How far back this station is worth offering files from. A floor,
        # never a high-water mark -- see manifest_watermark. What the ledger
        # contributes to it is read for the whole device at once, so a
        # machine with forty stations is still one query.
        "watermark": station_link.manifest_watermark(reoffer_point),
        # When ADL last received anything for this station, so the machine's
        # own list can say whether data is actually arriving. Beside the
        # watermark rather than under "admin", because it is neither the
        # machine's tier nor a setting anybody chose: it is a fact ADL
        # derived, and the agent may only read it.
        #
        # None means nothing has ever arrived, which the agent shows as a
        # station that has not started rather than one that has stopped.
        "last_received_at": last_received_at,
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


def connection_payload(connection, station_links, reoffer_points, last_received):
    return {
        "id": connection.pk,
        "name": connection.name,
        "admin": {
            "enabled": connection.plugin_processing_enabled,
            "network": connection.network.name,
            # Resolved here rather than sent as a null for the agent to fill
            # in: the fallback is ADL's number, and a machine that had to
            # know the default would be a machine that goes on using an old
            # one after a deployment changes it.
            "stale_after_minutes": (
                connection.stale_after_minutes or station_stale_after_minutes()
            ),
        },
        "station_links": [
            station_link_payload(
                link, reoffer_points.get(link.pk), last_received.get(link.pk)
            )
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
    last_received = AgentStationDataFile.last_received_for_connections(
        connections
    )

    return {
        "config_version": device.current_config_version(),
        "limits": limits_payload(),
        "device": device_payload(device),
        "connections": [
            connection_payload(
                connection, links.get(connection.pk, []), reoffer_points,
                last_received,
            )
            for connection in connections
        ],
    }


def heartbeat_payload(device, liveness):
    """The answer to a heartbeat: the clock, the cadence, and the verdict.

    ``clock_skew_seconds`` is sent back rather than merely stored: the
    machine is the only party that can do anything about its own clock, and
    telling it the number ADL computed is what lets the agent surface
    "this machine's clock is wrong" in its own tray without a second call.

    ``server_time`` is the raw material behind that number, so an agent can
    check ADL's arithmetic -- and correct a skew ADL has only just started
    measuring -- without waiting for the next heartbeat.
    """
    return {
        "device_id": device.pk,
        "server_time": dj_timezone.now(),
        "clock_skew_seconds": device.clock_skew_seconds,
        "status": liveness.state,
        "heartbeat_interval_minutes": heartbeat_interval_minutes(),
        "check_interval_minutes": device.check_interval_minutes,
        "config_version": device.current_config_version(),
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


def update_payload(offer):
    """What ADL says this machine should be running (story 28).

    The artifact's location is a path relative to the agent API's own base,
    never an absolute URL. What is being described is an executable that will
    replace a service running as LocalSystem on a national meteorological
    service's server, and "which host does it come from" is not a question
    the body of a response gets to answer -- not even this one, which travels
    through whatever reverse proxy a country's IT department has put in front
    of ADL.
    """
    artifact = offer.artifact

    return {
        "version": offer.version,
        "pinned": offer.pinned,
        "reason": offer.reason,
        "artifact": None if artifact is None else {
            "kind": artifact.kind,
            "path": f"update/{offer.version}/{artifact.kind}/",
            "file_name": artifact.file_name,
            "sha256": artifact.sha256,
            "size": artifact.size,
        },
    }
