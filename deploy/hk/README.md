# HK worker

The HK node uses the shared Hermes source at the exact approved `main` commit,
with an independent connector identity (`hk-primary`), token file
(`/etc/hk-team/cloud_connector_token`), systemd user unit, state directory, and
profile (`/home/hermes/.hermes/profiles/hk-worker`). Initial provisioning
creates that profile's real `config.yaml`, `SOUL.md`, and empty `skills/`
directory, then installs and starts `hermes -p hk-worker gateway` as the
profile-scoped user service.
Skills are not copied from DBB3 or PC.

Each fabric release is a complete, read-only Git source generation under
`/opt/hk-team/hermes-agent/.fabric-generations/<commit>`, with dependencies
built from the same generation's `uv.lock`. The `.fabric-current` symlink is
switched atomically. Profile data, connector state, recovery state, and all
credentials stay outside those generations and survive both updates and
rollbacks. A release SHA is committed only after the connector advertises a
new healthy WebSocket generation and the HK profile gateway is healthy on the
new runtime.

The hosted workflow has one server-local dispatcher and three independent worker
lanes (DBB3, PC/WSL, and HK). There is no supervisor or
reviewer model turn. Worker progress and results use the authenticated
`/api/plugins/collaboration/worker/ws` WebSocket; the durable queue remains the
reconnect/replay fallback.
