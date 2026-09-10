# ADL Agent Plugin

The server side of the **ADL Agent** — the Windows application an NMHS
installs on the machine where its station software writes files, so that the
machine **pushes** those files to [ADL](https://github.com/wmo-raf/adl) over
HTTPS instead of ADL dialling in. Use it when the server holding the files
has no public address or no inbound port, which is the common case.

This package is what those pushes arrive at: the API the agents call, the
admin pages an operator manages them from, the staging store the files land
in, and the drain that decodes staged files into observations — using the
[ADL FTP Plugin](https://github.com/wmo-raf/adl-ftp-plugin)'s decoders, so
every country-specific decoder written for FTP works unchanged.

The Windows application itself lives in
[adl-agent](https://github.com/wmo-raf/adl-agent).

**Operator guide:** [docs/guide.md](docs/guide.md) — enrolling a machine and
pairing it, every connection and station-link field, the file ledger and
reprocessing, collection passes, device health and liveness, agent releases
and the update feed, diagnostics and troubleshooting. The guide is also
published on the central ADL documentation site.

## Development setup

The plugin runs inside the ADL core image, alongside the ADL FTP Plugin
(whose decoders it uses). Build the `adl:latest` image from the
[ADL core repository](https://github.com/wmo-raf/adl) first, then:

```bash
git clone https://github.com/wmo-raf/adl-agent-plugin.git
cd adl-agent-plugin
cp .env.sample .env        # set PLUGIN_BUILD_UID=$(id -u), PLUGIN_BUILD_GID=$(id -g), ADL_DB_PASSWORD
docker compose build
docker compose up
docker compose exec adl adl createsuperuser
```

The admin is served on `PORT` (default 8080). The plugin source is
bind-mounted, so code changes reload the dev server. If the build fails with
`pull access denied` for `adl:latest`, prefix the build with
`DOCKER_BUILDKIT=0`.

Tests are Django-runner tests under
`plugins/adl_agent_plugin/src/adl_agent_plugin/tests/`, with helpers for a
paired device, a decoding connection and station links in `tests/helpers.py`.
Lint and format from `plugins/adl_agent_plugin/` with `make lint` and
`make format`. See [CONTRIBUTING.md](CONTRIBUTING.md) — a change to any
connection, station-link or device field must update the guide in the same
PR.
