"""Shared arrangement for the agent plugin's HTTP-seam tests."""

import gzip
import hashlib
import itertools
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from datetime import timedelta

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone as dj_timezone

from adl.core.models import DataParameter, Network, Station, Unit
from adl_agent_plugin.entries import FileEntry
from adl_agent_plugin.models import (
    AgentConnection,
    AgentConnectionVariableMapping,
    AgentDevice,
    AgentRelease,
    AgentReleaseArtifact,
    AgentReleaseArtifactKind,
    AgentStationDataFile,
    AgentStationLink,
    AgentStationLinkVariableMapping,
)
from adl_agent_plugin.tasks import nudge_latch_key
from adl_agent_plugin.throttling import AgentPairThrottle

PAIR_URL = reverse("plugins:adl_agent:pair")
ME_URL = reverse("plugins:adl_agent:device_me")
SYNC_URL = reverse("plugins:adl_agent:sync")
MANIFEST_URL = reverse("plugins:adl_agent:manifest")
FILES_URL = reverse("plugins:adl_agent:files")
UPDATE_URL = reverse("plugins:adl_agent:update")


def update_package_url(version, kind):
    """The package endpoint, addressed the way an agent addresses it."""
    return reverse("plugins:adl_agent:update_package", args=[version, kind])


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


def clear_nudge_latch(connection):
    """Forget that this connection has already been nudged.

    Same reason as the throttle above: ADL's cache is Redis, shared and
    long-lived, so a latch taken by one test would silence the next.
    """
    cache.delete(nudge_latch_key(getattr(connection, "pk", connection)))


@contextmanager
def at_time(moment):
    """Run the block as if ADL's clock read ``moment``.

    The one thing an HTTP-seam test cannot arrange by calling the API: a
    heartbeat that arrived a quarter of an hour ago. The request is still a
    real request through the real endpoint -- only the wall clock it lands on
    is moved -- so a test of liveness is still driven by heartbeats posted and
    withheld, not by states written by hand.
    """
    from unittest.mock import patch

    with patch("django.utils.timezone.now", return_value=moment):
        yield


@contextmanager
def tasks_run_immediately():
    """Run enqueued Celery tasks in-process, the way a worker would.

    The nudge that follows an upload is a real task, so a test asserting that
    a file arriving becomes observations has to let it run. Celery reads its
    configuration from Django settings once, at import, so this flips the
    app's own switch rather than overriding a setting nothing would re-read.
    """
    from adl.config.celery import app

    previous = (app.conf.task_always_eager, app.conf.task_eager_propagates)
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    try:
        yield
    finally:
        app.conf.task_always_eager, app.conf.task_eager_propagates = previous


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


def standard_csv_config(**kwargs):
    """A CSV configuration for the FTP plugin's ``standard_csv`` decoder.

    The agent stages files and the FTP plugin's decoders read them, so the
    configuration a decode runs under is the FTP plugin's model -- imported
    here rather than reimplemented, which is the whole point of the shared
    decoder ecosystem.
    """
    from adl_ftp_plugin.models import StandardCSVConfig

    kwargs.setdefault("name", f"CSV Config {next(_counter)}")
    kwargs.setdefault("datetime_column", "timestamp")
    kwargs.setdefault("datetime_format", "%Y-%m-%d %H:%M:%S")
    return StandardCSVConfig.objects.create(**kwargs)


def decoding_connection(device=None, network=None, **kwargs):
    """A connection wired up to actually decode what arrives on it."""
    kwargs.setdefault("decoder", "standard_csv")
    kwargs.setdefault("stations_timezone", "UTC")
    if "csv_config" not in kwargs:
        kwargs["csv_config"] = standard_csv_config()
    return create_connection(device=device, network=network, **kwargs)


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


def stage_file(station_link, name, content, mtime=None):
    """A file that has already arrived, staged exactly as an upload stages it.

    Goes through :meth:`AgentStationDataFile.record_upload` rather than
    building a row by hand, so a drain test is draining what the upload
    endpoint really leaves behind.
    """
    entry = FileEntry(
        station_link_id=station_link.pk,
        name=name,
        size=len(content),
        mtime=mtime or dj_timezone.now(),
        content_hash=sha256_of(content),
    )
    return AgentStationDataFile.record_upload(
        station_link, entry, ContentFile(content),
    )


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


# ---------------------------------------------------------------------------
# Vendor files, and the window their observations fall in
# ---------------------------------------------------------------------------

def observation_time(minutes_ago):
    """A whole minute in the recent past, in UTC.

    Whole minutes because the CSV format these tests use carries seconds but
    not microseconds, and a record that survives the round trip should compare
    equal to what the test wrote.
    """
    return (dj_timezone.now() - timedelta(minutes=minutes_ago)).replace(
        second=0, microsecond=0
    )


def csv_file(*readings, column="AirTemp"):
    """A vendor CSV: one datetime column, one variable column.

    ``readings`` are ``(datetime, value)`` pairs.
    """
    lines = [f"timestamp,{column}"]
    lines += [
        f"{moment.strftime('%Y-%m-%d %H:%M:%S')},{value}"
        for moment, value in readings
    ]
    return ("\n".join(lines) + "\n").encode()


#: Rendering a Wagtail admin page asks the staticfiles storage for hashed asset
#: names, and the test runner never runs collectstatic -- so the manifest these
#: pages resolve against does not exist. Serving static files unhashed is a
#: property of the test process, not of the plugin.
UNHASHED_STATICFILES = override_settings(STORAGES={
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
})


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


# ---------------------------------------------------------------------------
# Releases and the upstream they are mirrored from
# ---------------------------------------------------------------------------

def create_release(version, packages=None, published=True, **kwargs):
    """A release this instance holds, with a package per tier asked for.

    ``packages`` maps an artifact kind to its bytes; the default is a
    service-tier package, because that is the tier almost every test is about.
    """
    release = AgentRelease.objects.create(
        version=version, is_published=published, **kwargs,
    )

    for kind, content in (packages or {AgentReleaseArtifactKind.MSI: b"MSI BYTES"}).items():
        artifact = AgentReleaseArtifact(release=release, kind=kind)
        artifact.file.save(f"AdlAgent-{version}-{kind}.pkg", ContentFile(content), save=False)
        artifact.save()

    return release


class UpstreamReleaseHost:
    """The canonical release host, small enough to keep in a test.

    Real HTTP on a loopback port rather than a patched ``requests``: what the
    mirror has to get right is mostly protocol and bytes -- an index that is
    not JSON, a package that arrives short, a digest that does not match what
    the index promised -- and every one of those is exactly what patching the
    call would paper over.
    """

    def __init__(self):
        self.releases = []
        self.packages = {}
        self.index_body = None
        self.hits = []

        host = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's spelling
                host.hits.append(self.path)

                if self.path == "/index.json":
                    body = host.index_document()
                    self._send(200, "application/json", body)
                    return

                package = host.packages.get(self.path)

                if package is None:
                    self._send(404, "text/plain", b"no such package")
                    return

                self._send(200, "application/octet-stream", package)

            def _send(self, status, content_type, body):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def index_url(self):
        return f"{self.base_url}/index.json"

    def publish(self, version, packages=None, states_sha256=None, **entry):
        """Put a release on the upstream index.

        ``states_sha256`` maps a kind to the digest the index will *claim*,
        which is how a test becomes an upstream serving a package that is not
        what it says it is.
        """
        packages = packages or {"msi": b"MSI BYTES " + version.encode()}
        artifacts = []

        for kind, content in packages.items():
            path = f"/{version}/{kind}.pkg"
            self.packages[path] = content

            artifacts.append({
                "kind": kind,
                "url": f"{self.base_url}{path}",
                "sha256": (states_sha256 or {}).get(kind, sha256_of(content)),
                "size": len(content),
            })

        self.releases.append({
            "version": version,
            "released_at": entry.pop("released_at", "2026-08-21T10:00:00Z"),
            "notes": entry.pop("notes", f"Agent {version}"),
            "artifacts": artifacts,
            **entry,
        })

    def index_document(self):
        """The bytes the index URL answers with."""
        if self.index_body is not None:
            return self.index_body

        return json.dumps({"releases": self.releases}).encode()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
