"""Shared arrangement for the agent plugin's HTTP-seam tests."""

import gzip
import hashlib
import itertools
import tempfile

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone as dj_timezone

from adl.core.models import DataParameter, Network, Station, Unit
from adl_agent_plugin.models import (
    AgentConnection,
    AgentConnectionVariableMapping,
    AgentDevice,
    AgentStationLink,
    AgentStationLinkVariableMapping,
)
from adl_agent_plugin.throttling import AgentPairThrottle

PAIR_URL = reverse("plugins:adl_agent:pair")
ME_URL = reverse("plugins:adl_agent:device_me")
SYNC_URL = reverse("plugins:adl_agent:sync")
MANIFEST_URL = reverse("plugins:adl_agent:manifest")
FILES_URL = reverse("plugins:adl_agent:files")


def station_link_config_url(station_link):
    """The per-link config endpoint, addressed the way an agent addresses it."""
    pk = getattr(station_link, "pk", station_link)
    return reverse("plugins:adl_agent:station_link_config", args=[pk])


def clear_pair_throttle(ident="127.0.0.1"):
    """Forget what the pair endpoint has seen from ``ident``.

    ADL's cache is Redis, shared and long-lived, so the throttle history
    outlives a test run and would otherwise leak between tests. Deleting the
    one key this scope uses is surgical where ``cache.clear()`` would not be.
    """
    key = AgentPairThrottle.cache_format % {
        "scope": AgentPairThrottle.scope, "ident": ident,
    }
    cache.delete(key)


def configured_pair_attempts():
    """How many pair attempts the throttle allows per window."""
    return AgentPairThrottle().num_requests


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Builders
#
# factory_boy is not installed in this plugin's image, and the rows here are
# few enough that plain constructors read better than factories would. Names
# and identifiers are sequenced so a test can build as many as it likes
# without tripping the unique constraints core puts on them.
# ---------------------------------------------------------------------------

_counter = itertools.count(1)


def create_device(**kwargs):
    kwargs.setdefault("name", f"Device {next(_counter)}")
    return AgentDevice.objects.create(**kwargs)


def paired_device(**kwargs):
    """A device that has already traded its pairing code for a token.

    Returns ``(device, token)``. The exchange goes through the model rather
    than the pair endpoint so that tests of other endpoints are not also
    testing (or throttled by) pairing.
    """
    device = create_device(**kwargs)
    device, token = AgentDevice.redeem_pairing_code(device.pairing_code)
    return device, token


def create_network(**kwargs):
    kwargs.setdefault("name", f"Network {next(_counter)}")
    kwargs.setdefault("type", "automatic")
    return Network.objects.create(**kwargs)


def create_station(network=None, **kwargs):
    n = next(_counter)
    kwargs.setdefault("station_id", f"ST-{n:04d}")
    kwargs.setdefault("name", f"Station {n}")
    kwargs.setdefault("station_type", 0)
    kwargs.setdefault("location", Point(36.8, -1.3))
    kwargs.setdefault("wsi_series", 0)
    kwargs.setdefault("wsi_issuer", 0)
    kwargs.setdefault("wsi_issue_number", 0)
    kwargs.setdefault("wsi_local", str(n))
    return Station.objects.create(network=network or create_network(), **kwargs)


def create_connection(device=None, network=None, **kwargs):
    kwargs.setdefault("name", f"Connection {next(_counter)}")
    kwargs.setdefault("plugin", "adl_agent_plugin")
    kwargs.setdefault("stations_timezone", "Africa/Nairobi")
    return AgentConnection.objects.create(
        device=device or create_device(),
        network=network or create_network(),
        **kwargs,
    )


def create_station_link(connection=None, station=None, **kwargs):
    connection = connection or create_connection()
    kwargs.setdefault("local_folder_path", "C:\\vendor\\data")
    kwargs.setdefault("file_pattern", "*.dat")
    return AgentStationLink.objects.create(
        network_connection=connection,
        station=station or create_station(connection.network),
        **kwargs,
    )


def celsius():
    """The one unit these tests measure anything in."""
    unit, _created = Unit.objects.get_or_create(
        name="Celsius", defaults={"symbol": "degC"},
    )
    return unit


def create_parameter(**kwargs):
    kwargs.setdefault("name", f"parameter_{next(_counter)}")
    kwargs.setdefault("unit", celsius())
    return DataParameter.objects.create(**kwargs)


def map_on_connection(connection, parameter, unit, file_variable_name):
    return AgentConnectionVariableMapping.objects.create(
        network_connection=connection,
        adl_parameter=parameter,
        file_variable_name=file_variable_name,
        file_variable_unit=unit,
    )


def map_on_station_link(station_link, parameter, unit, file_variable_name):
    return AgentStationLinkVariableMapping.objects.create(
        station_link=station_link,
        adl_parameter=parameter,
        file_variable_name=file_variable_name,
        file_variable_unit=unit,
    )


def wire_datetime(value):
    """How DRF renders an aware datetime, so tests can compare like for like."""
    if value is None:
        return None
    rendered = value.isoformat()
    return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered


# ---------------------------------------------------------------------------
# Files, as an agent offers and sends them
# ---------------------------------------------------------------------------

def sha256_of(content):
    """The content hash an agent computes over a file before offering it."""
    return hashlib.sha256(content).hexdigest()


def manifest_entry(station_link, name, content, mtime=None, **overrides):
    """One candidate file, in the shape the manifest endpoint reads."""
    entry = {
        "station_link_id": getattr(station_link, "pk", station_link),
        "name": name,
        "size": len(content),
        "mtime": (mtime or dj_timezone.now()).isoformat(),
        "hash": sha256_of(content),
    }
    entry.update(overrides)
    return entry


class AgentClient:
    """A paired device, driving the file endpoints the way the app does."""

    def __init__(self, test_case, token):
        self.client = test_case.client
        self.token = token

    def manifest(self, entries):
        return self.client.post(
            MANIFEST_URL,
            data={"files": list(entries)},
            content_type="application/json",
            **bearer(self.token),
        )

    def requested(self, entries):
        """The ``(station_link_id, name)`` pairs ADL asks for."""
        body = self.manifest(entries).json()
        return [(f["station_link_id"], f["name"]) for f in body["requested"]]

    def upload(self, station_link, name, content, compress=False, **overrides):
        payload = manifest_entry(station_link, name, content, **overrides)

        if compress:
            payload["encoding"] = "gzip"
            content = gzip.compress(content)

        payload["file"] = SimpleUploadedFile(
            name, content, content_type="application/octet-stream"
        )

        return self.client.post(FILES_URL, data=payload, **bearer(self.token))


class TemporaryMediaRoot:
    """Keep bytes written by a test out of the developer's media folder."""

    @classmethod
    def setUpClass(cls):
        cls._media_dir = tempfile.TemporaryDirectory()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_dir.name)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        cls._media_dir.cleanup()
