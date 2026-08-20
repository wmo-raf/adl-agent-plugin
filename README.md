# ADL Agent Plugin

Server side of the **ADL Agent** — the Windows application that NMHS staff install on a
country server so that it *pushes* station files out to ADL over HTTPS, instead of ADL
dialing in to fetch them. Many of the services ADL serves have no public IP and no inbound
ports; inverting the direction is what makes ingestion possible at all for them.

This repository is the ADL-side half: the API those machines call, the admin an operator
manages them from, the staging store their files land in, and the drain that turns those
files into observations. The Windows application lives in `wmo-raf/adl-agent`.

See the spec in [wmo-raf/adl#269](https://github.com/wmo-raf/adl/issues/269) for the whole
design and the decision tickets behind it.

> **Requires the FTP plugin.** This plugin decodes with
> [`adl-ftp-plugin`](https://github.com/wmo-raf/adl-ftp-plugin) (0.12.0 or later) and does
> not ship decoders of its own — install it on the same ADL instance, along with whichever
> country-specific decoder plugins the vendor files need.

## What is built so far

**Device identity.** An operator creates an `AgentDevice` in the ADL admin and gets a
single-use pairing code; the agent trades that code for a long-lived bearer token and uses
it on every later call.

**Configuration.** An `AgentConnection` per vendor on the machine, an `AgentStationLink` per
station, and one `sync` call that hands a paired device everything it needs for a cycle.
The machine's own settings — where the files sit, how they are named — are writable from
the app; what the data means stays in the ADL admin.

**Files.** A `manifest` call that tells a machine which of its files ADL wants, a `files`
call that takes one of them, and an `AgentStationDataFile` ledger that remembers what
arrived so the same bytes are never asked for twice.

**The drain.** Staged files decoded through the FTP plugin's decoder ecosystem and upserted
by ADL's ordinary ingestion pipeline, run on the connection's schedule and again, within
seconds, whenever an upload cycle finishes.

### Credentials

|                | Pairing code                      | Device token                         |
|----------------|-----------------------------------|--------------------------------------|
| Shape          | `XXXX-XXXX`, human-typeable       | ~32 random bytes, opaque             |
| Lifetime       | 72 hours, single use              | Never expires                        |
| Stored         | In the clear, so it can be relayed| SHA-256 digest only                  |
| Shown          | On the device's admin page        | Once, in the pair response           |

Codes are read out over the phone as often as they are copied, so the alphabet leaves out
every character people confuse: `0`, `1`, `I`, `L`, `O` and `U`. Whatever separators or
casing a technician types is folded away; a character outside the alphabet is treated as a
wrong code, not a mistyped separator.

### API

The versioned surface is `api/agent/v1/`, mounted by ADL under `plugins/`:

| Endpoint                                                       | Auth         | What it does                                       |
|----------------------------------------------------------------|--------------|----------------------------------------------------|
| `POST  /plugins/api/agent/v1/pair/`                              | none         | Trades a pairing code for a device token           |
| `GET   /plugins/api/agent/v1/me/`                                | device token | What ADL believes about the calling device         |
| `GET   /plugins/api/agent/v1/sync/`                              | device token | The device's whole configuration, in one call      |
| `POST  /plugins/api/agent/v1/manifest/`                          | device token | Offers candidate files, answers with the ones to send |
| `POST  /plugins/api/agent/v1/files/`                             | device token | Takes one file, verified against its manifest entry |
| `PATCH /plugins/api/agent/v1/station-links/<id>/config/`         | device token | Writes the app's tier of one station link's config  |

`pair` is the only endpoint that answers without a credential, and the only one that is
rate-limited (30 attempts per client IP per hour by default; set
`ADL_AGENT_PAIR_THROTTLE_RATE` in DRF's `<n>/<period>` notation to change it).

A device token is a credential on these endpoints and nowhere else — it is not accepted by
ADL's core API or the Wagtail admin. A `401` from any agent endpoint means the device was
revoked, and the agent should stop uploading and ask to be re-paired. Every authenticated
call also records that the device was seen, which is the passive half of fleet liveness.

Errors answer with one envelope: a `code` an agent switches on, a `detail` a technician
reads, and whatever else that refusal owes the caller — `fields` for a config write that
reached outside the app's tier, `errors` for entries that could not be read (each with its
`index` in the batch), `limit` for a batch or file that was too big, `declared`/`actual`
for a file whose bytes did not match its entry.

### Files: propose, send, remember

The agent keeps no record of what it has already delivered — the vendor's folder is its
only state, and a folder cannot remember. So each cycle it *offers* what it can see and is
told what to send:

1. **Manifest.** One call for the whole machine, however many stations it serves, listing
   candidate files as `(station_link_id, name, size, mtime, hash)`. ADL diffs each against
   its ledger and answers with the ones it wants. Nothing is written — a proposal is not an
   arrival, so a cycle that dies between the manifest and the uploads leaves ADL believing
   exactly what it believed before.
2. **Upload.** One file per request, multipart, carrying its manifest entry alongside the
   bytes. ADL hashes what arrives and checks it against what was promised before storing
   anything.

The diff is on name and hash alone:

| The agent offers                              | ADL answers   |
|-----------------------------------------------|---------------|
| a name the ledger has never held              | send it       |
| a name whose hash matches what is held        | nothing       |
| a name whose hash differs — a grown daily CSV | send it again |
| a name whose ledger hash has been cleared     | send it again |

The last row is the re-process path: a file whose staged bytes have been pruned is asked
for again simply by clearing its ledger hash, because no hash an agent can compute equals
`NULL`. A whole file always comes again, never a delta — the core's upsert by observation
time makes re-ingesting the overlap harmless.

**The ledger.** One `AgentStationDataFile` per (station link, filename), for the life of
that filename. It is both the ledger row the manifest diffs against and the staging record
the drain will read: the ledger fields, the bytes on Django's default storage (plain disk,
or MinIO/S3 through a storage class — no object-store code lives here), and a
`received / processed / failed` status with `processed_at`, `values_saved` and
`last_error` beside it. A changed file updates the row **in place**, and everything ADL had
made of the previous bytes is cleared with it: what was decided about a shorter version of
a file says nothing about this one. The bytes it replaces are deleted once the row that
replaced them has committed.

Rows are permanent even where their bytes are not — pruning a row would make its file
eternally new and re-uploaded forever.

**The watermark**, which each station link carries in `sync`, is the oldest point a
station's files are still worth offering from. It is a **floor**, and only ever a floor.

It is tempting to raise it to the newest file ADL holds — that is what would make a settled
folder cheap to offer every five minutes — but two of the promises this system makes forbid
it. A file backfilled into the folder weeks late must still reach ADL, and a fresh install
facing months of backlog uploads newest first *so that history fills in behind*; a floor
that followed the newest arrival would close over both. Raising it safely needs something
ADL does not yet have: an agent saying "I have offered you my whole folder down to here".
That assertion belongs to the reconciliation sweep, and so does the raise.

So the floor is the collection start date, and what the ledger contributes is the power to
pull it back **down**: to the oldest file this link is waiting to be offered again. A
request to re-send a file nobody would be asked for is not a request at all.

**Limits**, both stated in every `sync` and `manifest` response under `limits` so a fleet
in the field follows a change without being reinstalled:

- `manifest_entries` — 500 candidate files per call. A longer manifest is **refused**, not
  truncated: an agent told about the first five hundred of its files would take ADL's
  silence about the rest for "already held" and never offer them again. The agent pages.
- `file_bytes` — 50 MB per file, after decompression, enforced as the bytes arrive rather
  than measured afterwards.

**Verification.** The declared size is checked as well as the hash, because size is the one
an agent can get wrong honestly — a file that grew between being stat'ed and being read —
and `size_mismatch` tells a technician something a digest mismatch does not. Either way
nothing is stored and no ledger row is touched: the file is simply offered again next
cycle. Filenames are checked too; a name carrying a folder, or reaching out of one, is
refused before it can name anything in storage.

**Compression** is optional. Send `encoding=gzip` as a form field alongside the file and
ADL decompresses as it reads. The hash is always over the file as it sits on the vendor's
disk, never over the compressed form, so switching compression on cannot make ADL
re-request everything it already holds.

A form field rather than the `Content-Encoding` header the API decision named: that header
describes the whole request body, and gzipping a whole multipart body would leave the
server unable to find the parts at all. Compression here is a property of one part, so it
is one part's field to carry.

**Switched-off stations** take no files: a station link an administrator has disabled is
reported in the manifest response under `disabled_station_links` and never asked for
anything, and an upload for one is refused with `409 station_link_disabled`. A station link
the device does not own — deleted centrally, or never its own — is reported under
`unknown_station_links` rather than raised, so one stale entry from a cached configuration
does not cost the machine the rest of its cycle. Pausing a *connection's* processing does
not stop its files arriving; it stops them being processed, which is the point of pausing.

### From file to observation

A staged file is not yet data. Turning it into observations is an ordinary ADL ingestion
run — the same `get_station_data` lifecycle every other plugin uses, with the same date
window, per-station lock, unit conversion, QC pipeline, upsert and activity log. The only
unusual thing about this plugin is where the bytes came from.

**Decoding is the FTP plugin's, unchanged.** The agent ships raw files precisely so that it
can be: the decoder ecosystem written for `adl-ftp-plugin` — standard CSV, TOA5, and every
country-specific decoder written against its registry — reads local file paths, and a
staged file is a local file path. So a connection picks its **Decoder** and, where that
decoder needs one, a **CSV Configuration**, from exactly the lists an FTP connection picks
them from. A decoder fix therefore deploys on ADL and never on a machine in the field.

**What each run does**, per station link: take every file the ledger has as `received`,
oldest first; decode it; yield its records to core; wait for core to persist them; then
write down what became of the file.

| Outcome | Status | What is recorded |
|---|---|---|
| Decoded, records saved | `processed` | `processed_at`, `values_saved` |
| Decoded, nothing ADL maps | `processed` | `values_saved` = 0 — a mapping or window problem, not a file problem |
| Would not decode | `failed` | `last_error`, the decoder's own message |
| Bytes unreadable — storage down, object gone | `received` | nothing — the fault is the instance's, not the file's |
| No decoder chosen on the connection | `received` | nothing — the fault is the connection's, and choosing a decoder drains the backlog |

"Processed" means *persisted*, not merely decoded: the stamp is written after core has
flushed that file's records, so `values_saved` is what reached the database — which may be
fewer values than the file held, and that difference is usually the interesting part.

A `failed` file is **not retried**. The same bytes fail the same way, and a station
re-reporting the same error every quarter of an hour teaches an operator to stop reading.
It waits for a decoder fix and a deliberate re-process, or for the vendor to write the file
again — a changed file comes again in full and resets its row to `received`.

That is exactly why `failed` is reserved for the *file* being wrong. Anything that will be
right again on its own — storage unreachable, the instance out of disk — leaves the row
`received` and is logged instead. Marking it would let one bad minute from the object store
permanently sideline a country's data, with nothing in this slice able to bring it back.

`last_error` is redacted on the way in. It is a decoder's exception text, and it is
rendered in the listing, on the inspect page and in worker logs, so a vendor path or
storage URL carrying a credential is bounded at the write point rather than at each reader.

One bad file costs nothing but itself: the drain marks it and moves on to the rest of the
station's backlog.

**Latency: the nudge.** Celery Beat runs each connection on its interval, and that pass is
the safety net. But an interval is the wrong latency for push delivery — a machine that has
just uploaded has *told* ADL there is work. So an upload asks for its connection to be
drained a few seconds later, and two things keep that from becoming a stampede:

- *One nudge per burst.* The first upload of a cycle takes a short-lived latch and
  schedules the drain; every upload behind it lets that drain cover its file too. The latch
  expires as the drain runs, so a machine still uploading re-arms one.
- *One drain per station.* The nudge runs the ordinary ingestion path, so it takes the same
  per-station lock the scheduled pass takes. A nudge and a scheduled run landing together
  never both process a file — the second records a skip.

Two layers stop a file being processed twice, and they cover different failures. The
per-station lock stops two runs starting together. The ledger status stops a file already
`processed` being picked up at all — so even if the lock were lost or expired, the second
run finds nothing to do. Core's upsert by observation time makes any overlap that does
happen harmless.

**Pausing a connection** stops its files being processed, not its files arriving. Uploads
are still accepted and staged; the drain — scheduled or nudged — leaves them alone until
processing is switched back on. That distinction is the point of pausing one.

**Where failures are seen.** *Agent Station Data Files* under Snippets lists every file a
machine has sent, with its status and the first line of its error as columns and a status
filter, so an operator can ask a country for its failures alone. The whole error is on the
file's own page.

### Configuration, and who owns which half

ADL stores every durable setting; the app is an editor writing through the API, holding
only the ADL URL, its token, and a cache of the last configuration it fetched, so that it
keeps shipping while ADL is unreachable. The split follows one rule: **what the data means
is HQ's call; where the files sit and how they are named is the machine's.**

| Tier             | Settings                                                                                                                                                   | Written from            |
|------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------|
| Admin-only       | Device lifecycle, which station a link is for, whether it is enabled, variable mappings, collection start date                                              | ADL admin               |
| App-editable     | Local folder path, file pattern, folder-structure settings, listing strategy and its Direct Fetch settings, stability window                                | The app, or the admin   |
| Per device       | Check interval — one loop per machine scans every folder it has                                                                                              | ADL admin               |

The check interval sits in the app's tier in the design too, but the API contract has no
device-scoped write — its five endpoints are pair, sync, manifest, files, and per-link
config — so for now it is set in the admin. A device config `PATCH` is a small addition
whenever the app needs one.

`AgentStationLink.APP_EDITABLE_FIELDS` is that middle row, stated once: the sync response
renders exactly it under `config`, and the config endpoint accepts exactly it. Anything
else in a `PATCH` body is refused — an admin-tier field by name, a misspelled one as
unknown — and the whole write is refused with it, so a machine is never left half
configured.

Both sides may write the app tier, so conflicts resolve **last-write-wins**: no `409`s,
no merging. Every response carries a `config_version` for the device, which moves whenever
anything in that device's configuration changes — a folder path written from the app, a
mapping added in the admin, a station link deleted. An agent whose cached version has moved
re-reads; that is the whole protocol.

Each station link also carries a **watermark**: the oldest file it is worth offering ADL,
derived from the ledger and the collection start date together — see
[Files](#files-propose-send-remember) above.

Station links and connections that an administrator has disabled are still sent, flagged
rather than omitted, so the technician at the machine can see that a station is switched
off centrally instead of watching it disappear.

### Admin

**Agent Devices** in the main menu. Creating a device issues its first pairing code
immediately; the device's page shows the code, its expiry, and the two actions:

- **Issue new pairing code** — enrollment, and also rotation. The device's current token
  keeps working until the new code is redeemed, so rotating does not create a data gap; the
  moment it is redeemed the old token stops working.
- **Revoke device** — destroys the token *and* any unused pairing code, so a compromised
  machine cannot re-enroll itself. Letting it back in means issuing a new code.

Both actions need change permission on the device, not merely admin access.

**Agent Connections** and **Agent Station Links** appear alongside every other plugin's
under Connections. A connection names the machine that sends its files, the decoder that
reads them (and its CSV configuration, where the decoder needs one), and the variable
mappings for the vendor's file columns; a station link binds one ADL station to one folder
on that machine, and may override a mapping the connection got wrong for it.

**Agent Station Data Files** under Snippets is the file-by-file record: what arrived, what
became of it, and why anything failed.

Deleting a device that has connections is refused — the delete page offers no button. Take
a machine out of service by revoking it, which cuts it off and leaves a country's folder
configuration intact.

### End to end, with curl

```bash
# 1. Create a device in the admin and copy its pairing code, then:
curl -X POST http://localhost:8080/plugins/api/agent/v1/pair/ \
     -H 'Content-Type: application/json' \
     -d '{"pairing_code": "ZTFA-DBVY"}'
# -> {"token": "...", "device": {...}}

# 2. Use the token
curl http://localhost:8080/plugins/api/agent/v1/me/ -H "Authorization: Bearer $TOKEN"
# -> 200 {"id": 1, "name": "...", ...}

# 3. Read the whole configuration for this device
curl http://localhost:8080/plugins/api/agent/v1/sync/ -H "Authorization: Bearer $TOKEN"
# -> 200 {"config_version": 5, "device": {...}, "connections": [{... "station_links": [...]}]}

# 4. Point a station link at the folder the files are really in
curl -X PATCH http://localhost:8080/plugins/api/agent/v1/station-links/1/config/ \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"local_folder_path": "D:\\AWS\\Demo", "file_pattern": "DEMO_*.csv"}'
# -> 200 {"station_link_id": 1, "config_version": 6, "config": {...}}
# and the new path is on the station link's page in the admin.

# 5. Try to move something that is not the app's to move
curl -X PATCH http://localhost:8080/plugins/api/agent/v1/station-links/1/config/ \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"start_date": "2020-01-01T00:00:00Z"}'
# -> 400 {"code": "read_only_fields", "fields": ["start_date"], "detail": "..."}

# 6. Offer the files the machine can see
curl -X POST http://localhost:8080/plugins/api/agent/v1/manifest/ \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"files": [{"station_link_id": 1, "name": "DEMO_0220.csv", "size": 34,
                     "mtime": "2026-02-20T10:00:00Z", "hash": "'"$HASH"'"}]}'
# -> 200 {"requested": [{"station_link_id": 1, "name": "DEMO_0220.csv", "hash": "..."}], ...}

# 7. Send one of the files it asked for
curl -X POST http://localhost:8080/plugins/api/agent/v1/files/ \
     -H "Authorization: Bearer $TOKEN" \
     -F station_link_id=1 -F name=DEMO_0220.csv -F size=34 \
     -F mtime=2026-02-20T10:00:00Z -F hash=$HASH \
     -F file=@DEMO_0220.csv
# -> 201 {"station_link_id": 1, "name": "DEMO_0220.csv", "status": "received", ...}

# 8. Offer it again — ADL already has it
#    (repeat step 6) -> 200 {"requested": [], ...}

# 9. Within seconds, the file has been decoded. Check in the admin under
#    Snippets -> Agent Station Data Files (status "Processed", with a count of
#    values saved), and the observations themselves in the monitoring views.

# 10. Revoke the device in the admin, then repeat step 2
# -> 401 {"detail": "Invalid or revoked device token."}
```

`$HASH` above is `shasum -a 256 DEMO_0220.csv`, which is exactly what the agent computes.

## Tests

```bash
make test    # with the stack already up
```

Or, without needing the app container running:

```bash
docker compose run --rm --entrypoint adl adl test --keepdb adl_agent_plugin.tests
```

The suite drives the plugin through the same surfaces a real agent and a real operator use:
the pairing lifecycle over HTTP (exchange, replay, expiry, revocation, rotation, the rate
limit, the authorization boundary), the sync and config endpoints (scope, both tiers, tier
enforcement, validation, last-write-wins, version propagation, liveness), the manifest and
upload endpoints (the full diffing matrix, paging, hash and size verification, gzip, the
size cap, re-upload in place, and what the watermark does as the ledger fills), the drain
(a real vendor CSV becoming observation records, failure isolation, what a run reports it
had to work with, idempotency under a held lock, and a grown file re-decoding), the nudge
that makes an upload become observations without waiting for the clock, the admin pages,
and the credential and variable-mapping rules on their own.

## Getting started

### Prerequisites

- Docker and Docker Compose installed on your machine.
- Git installed on your machine.

### Install and build the ADL Core Image

The ADL Agent Plugin is a module intended to be installed in an [ADL](https://github.com/wmo-raf/adl)
instance. This means that you need to first get the core ADL system and build it on your local development environment.

You can follow the instructions on the [ADL core repository](https://github.com/wmo-raf/adl) to install and build the
ADL core image

### Install ADL Agent Plugin

The `dev.Dockerfile` file uses the `adl` image as a base image. The `ADL Agent Plugin` is
installed during the build process. Using docker mounted volumes, the plugin is editable such that any changes made to
the code trigger Django to reload the development server, allowing you to see the changes as you develop

1. Clone the plugin repository:

```bash
git clone https://github.com/wmo-raf/adl-agent-plugin.git
cd adl-agent-plugin
```

2. Create a `.env` file using the provided `.env.sample` file:

```bash
cp .env.sample .env
```

3. Edit the `.env` file to set the required environment variables

```bash
nano .env
```

You can use the default values provided in the `.env.sample` file, but be sure to set the following correctly:

- `PLUGIN_BUILD_UID`: The UID of the user that will run the plugin inside the container
- `PLUGIN_BUILD_GID`: The GID of the user that will run the plugin inside the container

You can find the UID and GID of your user by running the following command:

```bash
id -u
id -g
```

4. Build the plugin image:

```bash
docker compose build
```

If you are getting errors like
`failed to solve: adl:latest: failed to resolve source metadata for docker.io/library/adl:latest: pull access denied`,
you might need to disable `DOCKER_BUILDKIT` when building the image.

You can do this by running the following

```bash
DOCKER_BUILDKIT=0  docker compose build
```

5. Start the plugin:

```bash
docker compose up
```

If everything is set up correctly, you should see the plugin starting up and listening for incoming requests. You can
access the plugin at `http://localhost:8000`. The port number can be changed using the `PORT` environment variable in
the `.env`. The default port is `8000`.

6. Create superuser

```bash
docker compose exec adl adl createsuperuser
```

The `adl`command is shorthand for `python manage.py` command. You can use it to run any Django management command
inside the container.


