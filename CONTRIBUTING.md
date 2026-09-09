# Contributing

## Documentation

`docs/guide.md` is the operator guide for this plugin. It is the single place
configuration is documented — the README deliberately stays short — and it is
aggregated into the central ADL documentation site.

**A pull request that touches the device, connection or station-link models
(adding, removing or renaming a field, changing a default or a validation
rule), the pairing or heartbeat contract, the file ledger, the release feed,
or any admin surface this plugin adds (the devices list, device info, the
data-files list and its bulk actions, the cycles list, the releases list)
must update `docs/guide.md` in the same PR.**

If the change is visible on screen, also update `docs/screenshots.yml` and
regenerate the images with the capture harness in the `adl` repo:

```bash
# from a checkout of wmo-raf/adl, with Docker running
scripts/capture-plugin-docs.sh ../adl-plugins/adl-agent-plugin
```

`--only <entry>` re-shoots a single entry, which is what to use for a crop fix:
a full run re-renders every image and the device and cycle pages carry live
timestamps, so fixing one crop otherwise lands as a diff in unrelated images.

### The demo state

There is no server to dial and nothing to mock: this plugin's source is a
machine that pushes. The demo therefore has to contain the state a real agent
would have produced, and two scripts build it.

- `docs/screenshots/pre_seed.py` runs **before** the fixture, and creates the
  device — `AgentConnection.device` is a non-null foreign key, and the
  fixture format names foreign keys rather than creating their targets.
- `docs/screenshots/seed.py` runs **after** it, and pairs the device through
  `redeem_pairing_code()` (the same call the pair endpoint makes, so the
  device really holds a token), writes a heartbeat, a staged-file ledger
  including two failed rows, several hours of cycle passes, the liveness
  transitions behind the current state, and one published release.

Two things there are load-bearing and easy to break:

- `heartbeat_details` must keep the shape `heartbeat.read_details()` reads.
  The device page renders its **Disk** and **Last cycle, per station** panels
  only when `volumes` and `links` are non-empty, and matches stations by
  `station_link_id`.
- **Recent state changes** renders only when `AgentDeviceStateTransition`
  rows exist. Setting `liveness_state` writes none, so the script records the
  run that led to the current state.

If you change any of those models or the heartbeat contract, change these
scripts in the same PR, or the capture quietly stops producing the panels the
guide numbers.

Images are code: never hand-edit a PNG in `docs/images/`; change the manifest
entry and regenerate. Keep images free of text (only numbered badges), since
the docs are translated.

Messages the plugin shows to operators are listed verbatim-shaped in the
guide's feedback catalogue — add a row when you add or change one.

## Development

See the README for the dev stack. Lint with `make lint` and format with
`make format` inside `plugins/adl_agent_plugin/`.

## Releases

Note the asymmetry: **this plugin repo tags bare** (`0.3.0`, never `v0.3.0`),
because `plugins.toml` entries pin the tag verbatim — while the **agent
application** in `wmo-raf/adl-agent` tags with a leading `v`, since its CI
triggers on `v*` and parses the version out of the ref. Cut a release here
with `gh release create 0.3.0`.

`wmo-raf/adl-agent-plugin` enforces this with a ruleset that refuses to create
any `refs/tags/v*`; a push rejected with `GH013 ... creations being
restricted` is that rule, not a permissions problem.
