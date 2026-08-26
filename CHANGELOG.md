# Changelog

Notable changes to the ADL Agent Plugin — the server-side half of ADL's
push-based file delivery. The agent that talks to it is versioned separately in
[`adl-agent`](https://github.com/wmo-raf/adl-agent); ADL core is versioned
separately again.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
with one addition borrowed from ADL core: each release carries an **Upgrade
notes** section listing the migrations it ships and anything an operator must do
*before* running them. Read that section before upgrading a deployment.

This file starts at 0.2.0, which is the first tagged release. Earlier history is
in the git log.

## [Unreleased]

### Added

- **Dated folder window, per device.** `AgentDevice.dated_folder_window_hours`
  (two days by default) now travels in the device block of the sync response as
  `dated_folder_window_hours`. It is how far back an agent walks the dated
  sub-folders of a station with `dir_structured_by_date` on an ordinary cycle;
  anything older is picked up by the agent's daily reconciliation. `0` means the
  current folder alone. The agent has read this field since it learned to walk
  dated trees ([wmo-raf/adl#289](https://github.com/wmo-raf/adl/issues/289)) and
  fell back to its own two-day default while nothing sent it; an agent that
  predates the field ignores it.

### Changed

- **The admin menu.** *Agent Devices* and *Agent Releases* no longer sit at the
  top level of the sidebar; both are now reached through
  *Settings > ADL Agent*. Both are administration done once per machine or per
  release, and the top level is for the things an operator opens every morning.

### Upgrade notes

Ships one migration:

- `0011_agentdevice_dated_folder_window_hours` — adds the field with a default
  of 48, which is what agents assumed before it existed. Nothing to do before
  running it, and no behaviour changes for a deployment that leaves it alone.

## [0.2.0] — 2026-08-25

The first tagged release, numbered to match the agent it serves. Everything
below is what the plugin does as it stands, rather than a delta from a release
that does not exist.

Many NMHSs cannot expose anything to the internet, so ADL cannot reach in to
collect their AWS files. This plugin inverts the direction: a machine on the
vendor's own server is paired once against ADL and pushes raw files outbound
over HTTPS. That outbound call is the only network path it ever needs.

### Added

- **Device identity and pairing.** A device trades a short pairing code for a
  bearer token, which is a credential on the agent endpoints and nowhere else.
  Revoking answers `401`, which is the agent's signal to stop sending and ask to
  be re-paired. Pairing is the only unauthenticated endpoint and the only
  rate-limited one.
- **Connections, station links and `sync`.** One call hands a machine its whole
  world: which folders to scan, how they are named, how far back to look. The
  two configuration tiers are kept visibly apart on the wire — `config` is the
  machine's and is exactly what the config endpoint accepts, `admin` is HQ's and
  travels only so the app can show it. Disabled connections and station links
  are sent flagged rather than omitted, so a technician sees a station switched
  off centrally instead of watching it disappear.
- **Manifest, upload, and the file ledger.** A machine offers candidates and ADL
  answers with the ones it wants, so a file is transferred once. The ledger
  remembers what ADL holds, which is what lets the agent keep no delivery state
  of its own.
- **Draining staged files to observations.** Files are decoded by the FTP
  plugin's decoder registry, so every country-specific decoder already written
  against it keeps working and a decoder fix deploys on ADL rather than on a
  machine in the field.
- **Bounded disk, and the way back.** Per-connection retention prunes staged
  bytes once ADL has made observations of them; files that failed to process
  keep their bytes. A re-process action reaches a file whose bytes are gone by
  asking the machine for them again.
- **Fleet health.** Liveness is inverted like everything else here — the machine
  says so itself every few minutes and ADL notices when it stops. Offline,
  degraded and cycle-stuck are told apart, and clock skew is reported beside the
  state rather than becoming it. Every threshold is settable per deployment.
- **Updating a fleet that cannot reach the internet.** ADL mirrors agent
  releases and serves the packages itself, with a per-device version pin.
  Mirroring is off until an instance asks for it.
- **Telling a station that stopped from one that never started.** Each station
  link carries `last_received_at` — when ADL last received anything for it — and
  each connection carries `stale_after_minutes`, how long one of that vendor's
  stations may say nothing before the machine's own list marks it quiet. The
  window is per connection because a cadence belongs to the vendor's software,
  not to the station it happens to be writing for. Every file counts toward it
  whatever ADL then made of it: a file that failed to decode still proves the
  folder, the pattern, the share and the upload all worked, and that fault is
  fixed in the ADL admin rather than by anyone standing at the vendor's server.

### Upgrade notes

First release, so a deployment installing this plugin applies migrations
`0001`–`0010` in one pass. Nothing has to be done beforehand.

Two settings are worth knowing about, both optional and both defaulted:

- `ADL_AGENT_STATION_STALE_AFTER_MINUTES` (default `360`) — the deployment-wide
  quiet-after window. Raise it on an individual connection instead when one
  vendor legitimately writes one file a day.
- `ADL_AGENT_RELEASE_MIRROR_ENABLED` (default `false`) — agent release mirroring
  stays off until an instance asks for it.

[Unreleased]: https://github.com/wmo-raf/adl-agent-plugin/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/wmo-raf/adl-agent-plugin/releases/tag/v0.2.0
