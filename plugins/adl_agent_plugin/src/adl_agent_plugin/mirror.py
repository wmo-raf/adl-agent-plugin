"""
Getting a release from one place to twenty-six instances.

ADL is not one deployment. Each NMHS runs its own instance, and an agent can
only be updated from the instance it is paired with -- its machines have no
route to anywhere else. Left there, publishing a version would mean an
operator in every country uploading the same file, and a fleet that is
current in the countries whose administrator had time.

So the instances mirror, and the agents do not. An ADL instance is an
internet-facing server that already pulls its own images; it can fetch a
release index from one canonical place and hold the packages locally. The
machines it serves still talk to nothing but it.

Two things this deliberately does not do:

**It does not publish.** A mirrored release arrives staged. WMO decides what
is available; each country decides when its own fleet moves, which for a
national meteorological service in the middle of a season is not a decision
worth making for them.

**It does not replace uploading.** An instance whose egress is locked down,
an operator testing a build before it goes everywhere, an air-gapped install
-- all of them upload in the admin, and produce exactly the same rows. The
feed cannot tell the difference and neither can an agent.

**And it is off until an instance asks for it.** See :func:`mirror_enabled`.

The index is a JSON document the agent's build publishes::

    {"releases": [
      {"version": "0.2.0",
       "released_at": "2026-08-21T10:00:00Z",
       "notes": "...",
       "artifacts": [
         {"kind": "msi",
          "url": "https://.../AdlAgent-0.2.0-x64.msi",
          "sha256": "9f2c...",
          "size": 43210987}]}]}

Every package is checked against the digest the index states before it is
stored, so a mirror that fetched half a file, or fetched something else
entirely, ends as a logged failure rather than as a release a country's fleet
installs.
"""

import hashlib
import logging
import tempfile
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.dateparse import parse_datetime

from .models import (
    AgentRelease,
    AgentReleaseArtifact,
    AgentReleaseArtifactKind,
    AgentReleaseSource,
    agent_release_sort_key,
    is_agent_release_version,
)

logger = logging.getLogger(__name__)

#: Where releases come from unless an instance says otherwise. GitHub serves
#: the newest release's assets at this stable path, so the URL does not have
#: to be changed for every version.
DEFAULT_INDEX_URL = (
    "https://github.com/wmo-raf/adl-agent/releases/latest/download/"
    "agent-releases.json"
)

#: How many of the index's releases to consider, newest first. A first run
#: against an index listing three years of history has no reason to pull all
#: of it: what a fleet can be moved to is the recent end, and an operator who
#: wants an old build for a pin can upload it.
DEFAULT_MIRROR_LIMIT = 3

#: What a single package may weigh. The agent is a self-contained .NET
#: publish -- tens of megabytes -- and anything approaching this is not one.
DEFAULT_MAX_PACKAGE_BYTES = 300 * 1024 * 1024

#: (connect, read) seconds. Generous on read: these are large files over
#: whatever link the instance has.
INDEX_TIMEOUT = (10, 30)
PACKAGE_TIMEOUT = (10, 120)

KNOWN_KINDS = {kind.value for kind in AgentReleaseArtifactKind}


def mirror_enabled():
    """Whether this instance fetches releases from upstream at all.

    Off unless an instance says otherwise, which is the opposite of what is
    convenient and the right way round for what this is. Mirroring gives an
    ADL instance a standing outbound dependency on a host outside the country
    running it, and an instance acquiring one because somebody upgraded a
    plugin is not a decision its IT department was party to -- particularly
    in a product whose whole premise is machines that reach nothing but their
    own ADL.

    Turning it on is one environment variable, and the README says which. An
    instance that never does still gets every release: an operator uploads
    the package in the admin, which produces the same rows and the same feed,
    and no agent can tell the difference.
    """
    return getattr(settings, "ADL_AGENT_RELEASE_MIRROR_ENABLED", False)


def index_url():
    return getattr(settings, "ADL_AGENT_RELEASE_INDEX_URL", "") or DEFAULT_INDEX_URL


def mirror_limit():
    return getattr(settings, "ADL_AGENT_RELEASE_MIRROR_LIMIT", DEFAULT_MIRROR_LIMIT)


def max_package_bytes():
    return getattr(
        settings, "ADL_AGENT_RELEASE_MAX_BYTES", DEFAULT_MAX_PACKAGE_BYTES,
    )


class MirrorRefused(Exception):
    """This release will not be mirrored, and why."""


def mirror_releases(url=None, limit=None):
    """Pull anything new from the upstream index into this instance.

    Returns ``{"mirrored": [...], "skipped": [...], "failed": [...]}`` --
    versions, not objects, because the caller is a Celery task writing a log
    line and the tests read the same three lists.

    Nothing here raises. A mirror that fails is an instance that goes on
    serving the releases it already has, which is the correct behaviour for
    the least urgent job this plugin runs.
    """
    result = {"mirrored": [], "skipped": [], "failed": []}

    if not mirror_enabled():
        return result

    url = url or index_url()

    try:
        entries = _read_index(url)
    except Exception as e:
        logger.warning("[AGENT RELEASES] Could not read the release index at %s: %s", url, e)
        return result

    entries.sort(key=lambda entry: agent_release_sort_key(entry.get("version")), reverse=True)

    held = set(AgentRelease.objects.values_list("version", flat=True))

    considered = 0

    for entry in entries:
        version = (entry.get("version") or "").strip()

        if not is_agent_release_version(version):
            # Not three numbers. An index this instance is too old to read
            # properly, or one somebody hand-edited.
            result["failed"].append(version or "?")
            logger.warning("[AGENT RELEASES] '%s' is not an agent version; skipped.", version)
            continue

        considered += 1

        if considered > (limit if limit is not None else mirror_limit()):
            break

        if version in held:
            # Already here, in whatever state this instance's operator has
            # put it. Never re-fetched and never re-published: a release that
            # was staged, looked at and left staged must stay that way.
            result["skipped"].append(version)
            continue

        try:
            _mirror_one(entry, url)
        except MirrorRefused as e:
            result["failed"].append(version)
            logger.error("[AGENT RELEASES] Did not mirror %s: %s", version, e)
        except Exception as e:
            result["failed"].append(version)
            logger.exception("[AGENT RELEASES] Could not mirror %s: %s", version, e)
        else:
            result["mirrored"].append(version)
            logger.info(
                "[AGENT RELEASES] Mirrored agent %s. It is staged; publish it "
                "when this instance's fleet should move.", version,
            )

    return result


def _read_index(url):
    """The index document's releases, as a list."""
    response = requests.get(url, timeout=INDEX_TIMEOUT)
    response.raise_for_status()

    document = response.json()

    releases = document.get("releases") if isinstance(document, dict) else None

    if not isinstance(releases, list):
        raise ValueError("the index has no 'releases' list")

    return [entry for entry in releases if isinstance(entry, dict)]


def _mirror_one(entry, source_url):
    """Fetch and store one release, or leave nothing behind.

    Every package is downloaded and verified before any row is written, so a
    release whose second artifact is corrupt does not land as a release with
    one artifact -- which an agent on the other tier would then be offered
    and never be able to install.
    """
    version = entry["version"].strip()

    fetched = []

    for artifact in entry.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue

        kind = (artifact.get("kind") or "").strip()

        if kind not in KNOWN_KINDS:
            # A package kind from a newer agent than this plugin. Ignored
            # rather than refused: the tiers this instance does know about
            # should still be mirrorable.
            logger.info(
                "[AGENT RELEASES] Ignoring the '%s' package of %s: this ADL "
                "does not know that kind.", kind, version,
            )
            continue

        fetched.append((kind, artifact, _fetch_package(version, kind, artifact)))

    if not fetched:
        raise MirrorRefused("the index lists no package this ADL can serve")

    stored_files = []

    try:
        with transaction.atomic():
            release = AgentRelease.objects.create(
                version=version,
                notes=(entry.get("notes") or "")[:20000],
                released_at=parse_datetime(entry.get("released_at") or "") or None,
                # Staged, always. Mirroring is WMO saying a version exists;
                # publishing is this country saying its machines may have it.
                is_published=False,
                source=AgentReleaseSource.MIRRORED,
                upstream_url=source_url[:200],
            )

            for kind, artifact, staged in fetched:
                stored = AgentReleaseArtifact(
                    release=release,
                    kind=kind,
                    expected_sha256=(artifact.get("sha256") or "").strip().lower(),
                )
                stored.file.save(
                    _file_name(artifact, version, kind), File(staged), save=False,
                )
                stored.save()

                stored_files.append(stored.file.name)
    except Exception:
        # The rows go back, but storage has no transaction: a package written
        # before the failure would sit there for ever, belonging to a release
        # that does not exist. Nothing else knows its name after this frame.
        for name in stored_files:
            try:
                default_storage.delete(name)
            except Exception as e:  # pragma: no cover - storage-dependent
                logger.warning(
                    "[AGENT RELEASES] Could not delete %s after a failed "
                    "mirror of %s: %s", name, version, e,
                )

        raise
    finally:
        for _kind, _artifact, staged in fetched:
            staged.close()


def _fetch_package(version, kind, artifact):
    """Download one package to a temporary file, or refuse it.

    The digest the index states is checked here, before anything is stored.
    An instance that mirrored a package it could not verify would be serving
    its whole fleet something nobody has checked -- and the agent's own
    verification would then reject it on every machine at once, which is a
    fleet-wide outage discovered one country at a time.
    """
    url = (artifact.get("url") or "").strip()
    stated = (artifact.get("sha256") or "").strip().lower()

    _refuse_bad_url(version, kind, url)

    if len(stated) != 64:
        raise MirrorRefused(f"the {kind} package states no usable sha256")

    limit = max_package_bytes()
    digest = hashlib.sha256()
    written = 0

    staged = tempfile.NamedTemporaryFile(suffix=f"-{version}-{kind}")

    try:
        with requests.get(url, stream=True, timeout=PACKAGE_TIMEOUT) as response:
            response.raise_for_status()

            for chunk in response.iter_content(chunk_size=1024 * 1024):
                written += len(chunk)

                if written > limit:
                    raise MirrorRefused(
                        f"the {kind} package is larger than the {limit} bytes "
                        "this instance will mirror"
                    )

                digest.update(chunk)
                staged.write(chunk)

        actual = digest.hexdigest()

        if actual != stated:
            raise MirrorRefused(
                f"the {kind} package hashes to {actual}, not the {stated} the "
                "index states"
            )

        staged.flush()
        staged.seek(0)

        return staged
    except Exception:
        staged.close()
        raise


def _refuse_bad_url(version, kind, url):
    """Only fetch a package from somewhere worth fetching one from."""
    parsed = urlparse(url)

    if parsed.scheme == "https":
        return

    # The same allowance the agent makes for its own ADL URL: loopback is a
    # test fixture, not a network. Everything else carrying an executable
    # into a fleet travels over TLS.
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return

    raise MirrorRefused(
        f"the {kind} package of {version} is at '{url}', which is not an https URL"
    )


def _file_name(artifact, version, kind):
    """What to store the package as.

    Taken from the URL when it looks like a file name, because that is what
    an operator will recognise in the admin, and built from the version when
    it does not. Never taken from anywhere that could put a separator in it:
    this becomes a path under the instance's storage.
    """
    from_url = urlparse(artifact.get("url") or "").path.rsplit("/", 1)[-1]

    if from_url and not set(from_url) & {"/", "\\"} and ".." not in from_url:
        return from_url

    return f"adl-agent-{version}-{kind}"
