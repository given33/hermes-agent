# HK worker

The HK node uses the shared Hermes source at the exact approved `main` commit,
with an independent connector identity (`hk-primary`), token file
(`/etc/hk-team/cloud_connector_token`), systemd user unit, state directory, and
profile (`hk-worker`). Skills belong in `~/.hermes/profiles/hk-worker/skills`;
they are not copied from DBB3 or PC. The fabric timer updates the code and
deployment assets from GitHub while preserving those local profile files.

The hosted workflow has one server-local dispatcher and three independent worker
lanes (DBB3, PC/WSL, and HK). There is no supervisor or
reviewer model turn. Worker progress and results use the authenticated
`/api/plugins/collaboration/worker/ws` WebSocket; the durable queue remains the
reconnect/replay fallback.
