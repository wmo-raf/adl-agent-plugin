"""
Post-seed state for the documentation capture run.

The harness runs this inside the web container after `seed_docs_demo` has
applied docs/screenshots/fixture.json and before the collection cycle.

This plugin's source is a Windows machine that pushes files in; there is no
server to dial and nothing to mock, so the demo has to contain the state a
real agent would have produced. What the declarative fixture cannot express,
and this does:

* the device pairing — done through the model's own redeem_pairing_code(),
  the same call the pair endpoint makes, so the device genuinely holds a
  token rather than having its flags set by hand;
* a heartbeat: agent and OS versions, the timestamps the liveness ladder
  reads, and the counts the device detail page shows;
* a staged-file ledger, including two failed rows — the data-files list is
  documented with its status filter, and a list with nothing failed cannot
  show what the filter is for;
* cycle passes for the last few hours, which is what the cycles list and a
  station's history are;
* one published release with an artifact, so the releases list is not empty.

Nothing here is real data, and the "machine" is a demo name.
"""

import hashlib
import random
from datetime import timedelta

from django.core.files.base import ContentFile
from django.utils import timezone as dj_timezone

from adl_agent_plugin.health import LivenessState
from adl_agent_plugin.models import (
    AgentConnection,
    AgentCyclePass,
    AgentCyclePassTrigger,
    AgentDevice,
    AgentDeviceStateTransition,
    AgentFileStatus,
    AgentRelease,
    AgentReleaseArtifact,
    AgentReleaseArtifactKind,
    AgentReleaseSource,
    AgentStationDataFile,
    AgentStationLink,
)

now = dj_timezone.now()
rng = random.Random("agent-docs-demo")

connection = AgentConnection.objects.get(name="Demo Agent Connection")
device = connection.device
links = list(
    AgentStationLink.objects.filter(network_connection=connection).order_by("id")
)

# -- pairing -----------------------------------------------------------------

if not device.is_paired:
    device, _token = AgentDevice.redeem_pairing_code(device.pairing_code)

# -- heartbeat ---------------------------------------------------------------

device.agent_version = "1.0.0"
device.os_version = "Windows 11 Pro 23H2"
device.last_heartbeat_at = now - timedelta(seconds=40)
device.last_cycle_completed_at = now - timedelta(minutes=3)
device.last_file_received_at = now - timedelta(minutes=3)
device.clock_skew_seconds = 1
# The shape heartbeat.read_details() reads back, key for key: the device
# detail page renders its Disk and "Last cycle, per station" panels only when
# `volumes` and `links` are non-empty, and station rows are matched by
# station_link_id.
device.heartbeat_details = {
    "uptime_seconds": 6 * 24 * 3600 + 4 * 3600,
    "backlog_count": 0,
    "dropped_passes": 0,
    "volumes": [
        {"volume": "C:\\", "free_bytes": 82 * 1024 ** 3, "total_bytes": 238 * 1024 ** 3},
        {"volume": "D:\\", "free_bytes": 411 * 1024 ** 3, "total_bytes": 931 * 1024 ** 3},
    ],
    "links": [
        {
            "station_link_id": link.pk,
            "scanned": 3,
            "offered": 2,
            "uploaded": 2,
            "failed": 0,
            "error": "",
        }
        for link in links
    ],
}
device.liveness_state = LivenessState.ONLINE
device.liveness_since = now - timedelta(hours=6)
device.save()

# The liveness history behind that state. The device detail page renders its
# "Recent state changes" panel only when transitions exist, and setting the
# state directly writes none — so record the run that led here: enrolled,
# a short outage overnight, recovered.
AgentDeviceStateTransition.objects.filter(device=device).delete()
for hours_back, from_state, to_state in [
    (30, "", LivenessState.ONLINE),
    (9, LivenessState.ONLINE, LivenessState.DEGRADED),
    (8, LivenessState.DEGRADED, LivenessState.OFFLINE),
    (6, LivenessState.OFFLINE, LivenessState.ONLINE),
]:
    AgentDeviceStateTransition.objects.create(
        device=device,
        at=now - timedelta(hours=hours_back),
        from_state=from_state,
        to_state=to_state,
    )

# -- staged files ------------------------------------------------------------

AgentStationDataFile.objects.filter(station_link__in=links).delete()

SAMPLE = (
    '"TOA5","DEMO001","CR1000X","12345","CR1000X.Std.05.01","CPU:demo_aws.CR1X","4321","Table10"\r\n'
    '"TIMESTAMP","RECORD","AirTC_Avg","RH","BP_hPa_Avg","Rain_mm_Tot","WS_ms_Avg","WindDir","SlrW_Avg"\r\n'
    '"TS","RN","Deg C","%","hPa","mm","meters/second","Deg","W/m^2"\r\n'
    '"","","Avg","Smp","Avg","Tot","Avg","Smp","Avg"\r\n'
)

files_made = 0
for index, link in enumerate(links):
    for step in range(6):
        moment = now - timedelta(minutes=10 * (step + 1))
        name = f"{link.station.station_id}_{moment:%Y%m%d%H%M}.dat"
        body = SAMPLE + (
            f'"{moment:%Y-%m-%d %H:%M:%S}",{step},{21 + step * 0.3:.2f},'
            f"{68 - step:.1f},1012.4,0,2.4,118,540.2\r\n"
        )
        # Two failures on the first link, so the list's status filter has
        # something to filter and the reprocess action has something to act on.
        status = (
            AgentFileStatus.FAILED
            if index == 0 and step < 2
            else AgentFileStatus.PROCESSED
        )
        data_file = AgentStationDataFile(
            station_link=link,
            file_name=name,
            size=len(body),
            mtime=moment,
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            received_at=moment + timedelta(seconds=20),
            status=status,
        )
        data_file.file.save(name, ContentFile(body.encode()), save=False)
        data_file.save()
        files_made += 1

# -- cycle passes ------------------------------------------------------------

AgentCyclePass.objects.filter(device=device).delete()

passes_made = 0
for minutes_back in range(5, 360, 5):
    moment = now - timedelta(minutes=minutes_back)
    for link in links:
        scanned = rng.randint(1, 4)
        offered = rng.randint(0, scanned)
        AgentCyclePass.objects.create(
            time=moment,
            # When the beat carrying this pass reached ADL, a moment after the
            # machine finished it.
            received_at=moment + timedelta(seconds=12),
            device=device,
            station_link=link,
            unit=link.local_folder_path,
            trigger=AgentCyclePassTrigger.SCHEDULED,
            completed=True,
            duration_ms=rng.randint(400, 2600),
            folders_walked=1,
            scanned=scanned,
            held=0,
            offered=offered,
            wanted=offered,
            uploaded=offered,
            failed=0,
        )
        passes_made += 1

# -- a published release -----------------------------------------------------

release, _ = AgentRelease.objects.get_or_create(
    version="1.0.0",
    defaults={
        "notes": "First packaged build for the demo fleet.",
        "is_published": True,
        "released_at": now - timedelta(days=9),
        "source": AgentReleaseSource.UPLOADED,
    },
)
if not release.artifacts.exists():
    artifact = AgentReleaseArtifact(
        release=release,
        kind=AgentReleaseArtifactKind.MSI,
    )
    payload = b"MSI placeholder for the documentation demo; not a real installer."
    artifact.size = len(payload)
    artifact.file.save("adl-agent-1.0.0.msi", ContentFile(payload), save=False)
    artifact.save()

print(f"[docs-seed] device {device.pk} paired and online, {files_made} staged file(s) "
      f"({AgentStationDataFile.objects.filter(station_link__in=links, status=AgentFileStatus.FAILED).count()} failed), "
      f"{passes_made} cycle pass(es), release {release.version}")
