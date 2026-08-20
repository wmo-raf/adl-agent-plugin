# ADL Agent Plugin

Server side of the **ADL Agent** — the Windows application that NMHS staff install on a
country server so that it *pushes* station files out to ADL over HTTPS, instead of ADL
dialing in to fetch them. Many of the services ADL serves have no public IP and no inbound
ports; inverting the direction is what makes ingestion possible at all for them.

This repository is the ADL-side half: the API those machines call, the admin an operator
manages them from, and (in later slices) the staging store and drain that turn pushed files
into observations. The Windows application lives in `wmo-raf/adl-agent`.

See the spec in [wmo-raf/adl#269](https://github.com/wmo-raf/adl/issues/269) for the whole
design and the decision tickets behind it.

## What is built so far

**Device identity.** An operator creates an `AgentDevice` in the ADL admin and gets a
single-use pairing code; the agent trades that code for a long-lived bearer token and uses
it on every later call.

**Configuration.** An `AgentConnection` per vendor on the machine, an `AgentStationLink` per
station, and one `sync` call that hands a paired device everything it needs for a cycle.
The machine's own settings — where the files sit, how they are named — are writable from
the app; what the data means stays in the ADL admin.

The manifest and upload endpoints, and the drain into ADL's ingestion pipeline, arrive in
later slices; until then the plugin ingests nothing.

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
| `PATCH /plugins/api/agent/v1/station-links/<id>/config/`         | device token | Writes the app's tier of one station link's config  |

`pair` is the only endpoint that answers without a credential, and the only one that is
rate-limited (30 attempts per client IP per hour by default; set
`ADL_AGENT_PAIR_THROTTLE_RATE` in DRF's `<n>/<period>` notation to change it).

A device token is a credential on these endpoints and nowhere else — it is not accepted by
ADL's core API or the Wagtail admin. A `401` from any agent endpoint means the device was
revoked, and the agent should stop uploading and ask to be re-paired. Every authenticated
call also records that the device was seen, which is the passive half of fleet liveness.

Errors answer with one envelope: a `code` an agent switches on, a `detail` a technician
reads, and — for a refused config write — either `fields` (what was not the app's to
write) or `errors` (what did not validate, keyed by field).

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

Each station link also carries a **watermark**: the oldest file it is worth offering ADL. It
is a floor rather than a high-water mark — a file backfilled into the folder weeks late must
still reach ADL — and today it is the link's collection start date. The file ledger, which
arrives with the manifest slice, will make it better informed without changing its meaning.

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
under Connections. A connection names the machine that sends its files and carries the
variable mappings for the vendor's file columns; a station link binds one ADL station to
one folder on that machine, and may override a mapping the connection got wrong for it.

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

# 6. Revoke the device in the admin, then repeat step 2
# -> 401 {"detail": "Invalid or revoked device token."}
```

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
enforcement, validation, last-write-wins, version propagation, liveness), the admin pages,
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


