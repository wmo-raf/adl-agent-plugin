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

- **Upload concurrency, per deployment.** The `limits` block of `sync` -- and of every
  `manifest` response beside it -- now carries `concurrent_uploads`: how many files one
  machine may have on the wire at once, across every station it serves. Four by default;
  set with `ADL_AGENT_CONCURRENT_UPLOADS` (Django settings or the environment), clamped to
  32 here and again on the agent's own side.

  The only one of the three limits that is a tuning knob rather than a guard. The other two
  exist so that one machine having a bad day cannot take the instance with it; this one is
  a judgement about the country's link and this instance's capacity, and neither is visible
  from the vendor's server room where the machine sits. Three thousand files of backfill
  sent one round trip at a time is the difference between a station catching up this
  morning and catching up this week -- and the link belongs to the whole met service.

  Zero is nonsense rather than a choice, unlike the reconciliation interval: a machine that
  may upload no files at once is a machine that is not doing anything, so it takes the
  default, as does anything unreadable. A deployment that mistypes it keeps its fleet
  uploading.

  Read by the agent from [wmo-raf/adl#304](https://github.com/wmo-raf/adl/issues/304);
  one that predates the field uploads one file at a time, as every agent does today. No
  migration.

- **A machine's collection history, kept.** The heartbeat now carries `completed_passes`:
  the unit passes that finished on the machine since the last beat ADL accepted. Each
  station's share of each becomes one row in the new `AgentCyclePass` hypertable — what the
  pass walked, what it scanned, held, offered, wanted, uploaded and lost, how long it took,
  why it stopped if it did, and **up to three names of files that were seen and did not
  arrive**, with their reason.

  Before this ADL held exactly one cycle's worth of what a machine had been doing and
  overwrote it every five minutes. That is the right shape for liveness — decision #264 said
  so, and this does not reverse it: beats stay stateless and only transitions are logged.
  What gets history is the *pass*, which is a different object with a different lifetime.

  The missing-files field is the point of it. ADL already stores the name of every file it
  received; the names of the ones that were *seen and did not arrive* is where "the vendor
  renamed its files on the 14th" lives, and it is the difference between "this station is
  quiet" and "this station is quiet because the files are now called something else". No new
  privacy exposure, for the same reason.

  Every pass is stored, including the uneventful ones — filtering saves rows only on quiet
  stations, which are precisely the ones where "the agent looked and there was nothing" is
  the fact worth having. Time bounds the table instead: a compression policy at 7 days and a
  retention policy at 90, both settable with `ADL_AGENT_CYCLE_COMPRESS_AFTER_DAYS` and
  `ADL_AGENT_CYCLE_RETENTION_DAYS` and re-applied nightly so a change to either takes
  without a migration to hang it on.

  `last_cycle` is **unchanged and stays indefinitely**. Agents auto-update through the
  release feed; ADL instances are upgraded by a person, per country — so a new agent meeting
  an old plugin is the normal, long-lived state across twenty-six ministries, and
  `last_cycle.completed_at` is what `AgentDevice.last_cycle_completed_at` and the
  `cycle_stuck` check are written from. An agent sending `last_cycle` alone is read as one
  pass per beat: coarser than a newer one's, and emphatically better than nothing.

  `StationLinkActivityLog` is untouched and the monitoring activity list gains no rows from
  this. Ships migrations `0013` and `0014`; see the upgrade notes.

- **"Agent Cycles", a filterable listing.** Machine, station, trigger, outcome and date
  range — which makes *every failed pass this week, across every device* a question ADL can
  answer at all. "Did not arrive" is a column rather than something to open a row for,
  because it is what brings anybody here. **Recent cycles** panels on the device and
  station-link edit pages show the last ten and link into the listing already narrowed.

- **A device's log level, set from ADL.** `AgentDevice.log_level` is sent in the `device`
  block of `sync` when it is set, and the agent prefers it over whatever the machine itself
  is configured with. Blank sends nothing, which the agent reads as "use the local setting"
  — the same reading of silence `reconciliation_interval_hours` and
  `dated_folder_window_hours` get, so clearing the field gives the machine back to whoever
  is standing at it.

  Raising a country server to `Debug` otherwise means reaching the machine, which is the
  exact problem this product exists to solve. Leaving one raised is safe: the agent's log
  has a fixed size ceiling, so what it costs is how far back the log reaches, not disk.
  `None` is not offered — a machine told to keep no record is a machine with nothing to show
  on its next bad day.

  From [wmo-raf/adl#307](https://github.com/wmo-raf/adl/issues/307).

### Upgrade notes

Migrations: `0013_agentdevice_log_level_agentcyclepass`,
`0014_agentcyclepass_policies`.

`0014` sets a TimescaleDB compression policy and a retention policy on the new hypertable,
so the deployment must be on the `timescalegis` backend — which it already must be, since
core's own observation tables are hypertables. Nothing to do before running them: the table
is new, so there is nothing to migrate into it and nothing that can be lost. An instance
whose policies could not be set logs the failure and keeps collecting; the nightly
`run_agent_cycle_policies` task tries again.

Nothing needs to happen to the fleet. Agents that predate `completed_passes` go on sending
`last_cycle` and are stored one pass per beat; agents that carry it are stored one row per
station per pass. Neither needs reinstalling.

## [0.4.0] — 2026-08-27

### Fixed

- **A machine working through a backlog is no longer called stuck.** *Cycle
  stuck* now means **no progress** — heartbeats fresh, and for over 2× the check
  interval neither a scan cycle completed **nor a file arrived** — rather than
  simply *no completed cycle*
  ([wmo-raf/adl#303](https://github.com/wmo-raf/adl/issues/303)).

  The agent stamps a completed cycle only once it has been round every station
  on the machine, so a server pushing a first bind's backlog goes hours without
  one while uploading the whole time. Every deployment hits this the first time
  an administrator binds a station with history behind it, and it showed as a
  permanently amber machine that was in fact working hardest.

  The two signals are complementary and neither works alone: an idle machine
  proves itself by finishing empty cycles and sends nothing, a busy one proves
  itself by files arriving and finishes no cycles. The arrival window is the
  existing cycle threshold — `ADL_AGENT_CYCLE_STUCK_MULTIPLIER` × the device's
  check interval — so there is no new setting.

  A machine green on arrivals rather than on a cycle stays **Online** and says
  why in its own sentence, following the rule clock skew already sets: a finding
  about a machine that is otherwise fine belongs in the message, not in the
  state. The device page shows *Last file received* beside *Last completed scan
  cycle*, so the verdict can be read off the page.

  The alarm is narrowed, not removed. A machine that is heartbeating and sending
  nothing — a station bound to a folder that does not exist, say — is still
  reported stuck, and silence still outranks stuckness.

### Upgrade notes

Ships one migration:

- `0012_agentdevice_last_file_received_at` — adds `AgentDevice.last_file_received_at`,
  null on every existing row. Correctly so: nothing has been stamped yet. A
  machine that is genuinely working stamps it on its next upload; one that is not
  keeps the verdict it already had. Nothing to do before running it.

No new settings, and no wire change: this is computed from what ADL already
stores, so it takes effect on every machine in the fleet regardless of which
agent version it is running.

## [0.3.0] — 2026-08-26

### Added

- **Reconciliation interval, per deployment.** The device block of `sync` --
  and the heartbeat response beside it -- now carries
  `reconciliation_interval_hours`: how often a station stops trusting the cheap
  scan path and offers everything back to its collection start date. Daily by
  default, which is what every install already did; set with
  `ADL_AGENT_RECONCILIATION_INTERVAL_HOURS` (Django settings or the
  environment), and `0` switches sweeps off for a link that cannot carry a full
  folder's manifest. Deployment-wide rather than per device, like the heartbeat
  interval it sits next to, because what a sweep spends is manifest traffic on
  the link to ADL rather than anything on the machine's disks. The agent has
  read this field since it learned to reconcile
  ([wmo-raf/adl#280](https://github.com/wmo-raf/adl/issues/280)) and fell back
  to a fixed 24 hours while nothing sent it; an agent that predates the field
  ignores it. No migration.

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

One new setting, optional and defaulted:

- `ADL_AGENT_RECONCILIATION_INTERVAL_HOURS` (default `24`) — how often a
  station offers its whole folder back to its collection start date. Leave it
  alone and the fleet behaves exactly as it did before this release. `0`
  switches sweeps off, for a deployment whose links cannot carry a full
  folder's manifest.

Both halves are backward-compatible in both directions: an agent predating
[wmo-raf/adl#280](https://github.com/wmo-raf/adl/issues/280) or
[#289](https://github.com/wmo-raf/adl/issues/289) ignores the new device-block
keys, and an agent that reads them falls back to its own defaults against an
older plugin. Upgrading the two sides in either order is safe.

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

[Unreleased]: https://github.com/wmo-raf/adl-agent-plugin/compare/0.4.0...HEAD
[0.4.0]: https://github.com/wmo-raf/adl-agent-plugin/compare/0.3.0...0.4.0
[0.3.0]: https://github.com/wmo-raf/adl-agent-plugin/compare/0.2.0...0.3.0
[0.2.0]: https://github.com/wmo-raf/adl-agent-plugin/releases/tag/0.2.0
