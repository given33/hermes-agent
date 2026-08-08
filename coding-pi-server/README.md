# Standalone Pi Coding Service

This directory hosts the independent server process for the Hermes iOS
Coding mode. It runs the cloned `oh-my-pi` repository directly through its
official RPC CLI. The Pi source, Bun runtime, session state, model selection,
tools, extensions, slash commands, subagents, and native addon remain on the
server; the Hermes iOS app hosts a native React Native equivalent of the
official `collab-web` interaction surface inside a Hermes-styled iOS shell.
The untouched official `collab-web` build is also served at `/collab/` for
browser/share links.

The old Hermes plugin endpoint remains available as a compatibility path at
`/api/plugins/coding-pi`. The independent endpoint is `/api/coding-pi`.

## Start locally

From the Hermes release directory, prepare the private source checkout first:

```powershell
py -3.11 coding-pi-server\sync_private_source.py `
  --repository https://github.com/given33/hemres-pi.git `
  --ref 3a8591a8af5b6d200088d12ca75a5517cb064fa8 `
  --root C:\path\to\hemres-pi
```

The Git transport must already be authenticated on the server. The sync
script does not accept or persist a GitHub token.

Then start the service:

For the local deployment used here, Pi uses the native DeepSeek provider and
the `deepseek-v4-flash` model. Inject the key only into the service process:

```powershell
$env:CODING_PI_PROVIDER = "deepseek"
$env:CODING_PI_MODEL = "deepseek-v4-flash"
$env:DEEPSEEK_API_KEY = "<your DeepSeek API key>"
```

```powershell
py -3.11 coding-pi-server\standalone_server.py `
  --host 0.0.0.0 `
  --port 8787 `
  --root C:\path\to\hemres-pi `
  --repository https://github.com/given33/hemres-pi.git `
  --ref 3a8591a8af5b6d200088d12ca75a5517cb064fa8 `
  --workspace C:\path\to\your\workspace `
  --allow-workspace C:\path\to\your\workspace
```

The server resolves Bun from `PATH`. Use `--bun C:\path\to\bun.exe` when Bun
is not on the service account's `PATH`. `--home` controls the independent Pi
session directory; it defaults to `.coding-pi` under the service working
directory. Do not put the session directory in a public web root.

Build the official mobile collaboration surface from the same verified source
checkout before starting the service:

```powershell
py -3.11 coding-pi-server\sync_collab_web.py `
  --source C:\path\to\hemres-pi `
  --destination C:\path\to\hermes-v20-release\coding-pi-server\collab-web-dist
```

The generated page is served at `/collab/`; its WebSocket room endpoint is
`/r/<roomId>`, matching the upstream collab link grammar. Configure
`CODING_PI_PUBLIC_ORIGIN` and `CODING_PI_RELAY_URL` for a phone or an outside
network. Outside localhost the official browser client requires an HTTPS/WSS
origin, for example:

```powershell
$env:CODING_PI_PUBLIC_ORIGIN = "https://pi.example.com"
$env:CODING_PI_RELAY_URL = "wss://pi.example.com"
$env:CODING_PI_COLLAB_WEB_URL = "https://pi.example.com/collab"
```

Every Pi RPC session also loads the external
`plugins/coding-pi/pi-agent-control.ts` adapter through Pi's public
`--extension` flag. The adapter imports the same pinned checkout inside the Pi
process and delegates the official collab Agent Hub `chat`, `kill`, and
`revive` actions to Pi's own `AgentRegistry` and `AgentLifecycleManager`; the
Pi source itself is never edited. If that adapter is missing, Coding remains
usable but Agent Hub actions fail closed with an explicit bridge error.

For a development phone connected to the same LAN, the native Hermes Coding
surface can use the LAN HTTP API with `EXPO_PUBLIC_HERMES_ALLOW_HTTP=1`, but
the unmodified official browser client still requires WSS for a non-local
relay. Use a trusted HTTPS/WSS origin when sharing the generated `web_link`
with another device. `CODING_PI_LOCAL_RELAY_LINK=1` is reserved for an
integrating client that explicitly rewrites a localhost relay target; the
Hermes native surface does not rely on that rewrite.

The full link is private/write-capable; the view link is read-only. Room
credentials are stored inside each Pi session's metadata, so a service restart
recreates the same room and the Hermes client can reconnect to the same
session. The encrypted fragment is never sent to the HTTP API.

### LAN changes and Windows restart recovery

Use the local node supervisor when the Pi computer is not a permanently fixed
LAN address:

```powershell
.\coding-pi-server\set_local_pi_secret.ps1
.\coding-pi-server\register_local_pi_task.ps1
```

The first command stores the provider key as a Windows-user DPAPI-protected
blob under the independent Pi runtime directory; it is not part of either
source checkout and is never placed in a scheduled-task argument. The second
command registers a hidden user-logon task. The task starts `node_agent.py` on
8786, which starts/restarts the real Pi service on 8787. The Hermes native
client probes the current phone subnet on a failed direct connection, finds
8786 or 8787, wakes the child if necessary, and then continues using the same
stable node id and persistent session room.

This handles a router DHCP/IP change and a normal Windows reboot while the
computer is powered on. A powered-off computer cannot be started by an HTTP
request; Wake-on-LAN must be enabled separately in the PC firmware, network
adapter, and router if that scenario is required. For a central Hermes server,
set `--coordinator-url https://your-hermes-server` (or
`CODING_PI_COORDINATOR_URL`) and expose the Coding Pi plugin's WebSocket route.
The local Node Agent then keeps an outbound WSS tunnel to
`/nodes/<node-id>/tunnel`; Hermes forwards normal API calls and the SSE event
stream through that tunnel, so the phone does not need the PC's LAN address.
The server-side node registry supports remote dispatch and handoff without
assuming the old LAN address. Use `CODING_PI_COORDINATOR_TOKEN` on both sides
for tunnel authentication. When registering the Windows task, the URL can be
written into the action without putting a provider secret in it:

```powershell
.\coding-pi-server\register_local_pi_task.ps1 -CoordinatorUrl "https://your-hermes-server"
```

For a protected deployment, add `--token <long-random-token>`. The mobile app
should normally reach the service through the deployment's authenticated
reverse proxy rather than embedding a long-lived service token in an Expo
bundle. The service token option is useful for local/private network testing.

Equivalent environment variables are available for process managers:

`CODING_PI_ROOT`, `CODING_PI_REPOSITORY`, `CODING_PI_REF`,
`CODING_PI_BUN_PATH`, `CODING_PI_CLI_PATH`,
`CODING_PI_WORKSPACE`, `CODING_PI_PROVIDER`, `CODING_PI_MODEL`,
`CODING_PI_CONFIG`, `CODING_PI_ALLOWED_WORKSPACES` (separated by the
platform path separator), `CODING_PI_HOME`, `CODING_PI_SERVER_TOKEN`, and
`CODING_PI_CORS_ORIGINS`, `CODING_PI_PUBLIC_ORIGIN`, `CODING_PI_RELAY_URL`,
`CODING_PI_COLLAB_WEB_URL`, `CODING_PI_LOCAL_RELAY_LINK`,
`CODING_PI_NODE_ID`, `CODING_PI_NODE_PEERS`, `CODING_PI_COORDINATOR_URL`,
`CODING_PI_COORDINATOR_TOKEN`, `CODING_PI_COORDINATOR_BASE_PATH`, and
`CODING_PI_COORDINATOR_INTERVAL`. Provider credentials such as
`DEEPSEEK_API_KEY` remain deployment environment variables and are never
written to the source checkout or the Hermes config file.

The local bootstrap agent accepts `GET /health` and authenticated (when
`CODING_PI_NODE_AGENT_TOKEN` is set) `POST /wake`, `POST /start`, and
`POST /stop` on port 8786. It is a convenience supervisor only; all coding,
tools, approvals, sessions, and collaboration continue to execute in the
unmodified Pi checkout on the child service.

## Point Expo Go at the independent service

Set `EXPO_PUBLIC_CODING_PI_URL` to the service origin before starting Metro.
Private RFC1918 HTTP is allowed only for this independent companion service;
the Hermes account/server origin still follows its HTTPS policy:

```powershell
$env:EXPO_PUBLIC_CODING_PI_URL = "http://192.168.1.4:8787"
pnpm exec expo start --go --lan --port 8082
```

If `EXPO_PUBLIC_CODING_PI_URL` is absent, the app falls back to the existing
Hermes plugin route so ordinary Hermes deployments continue to work. Hermes
chat, group chat, and workflow jobs use their existing API clients and are not
shared with Pi session processes.

To let the Hermes agent itself dispatch implementation work into Coding, enable
the bundled `coding-pi` plugin in the active Hermes profile and point its tool at
the same service:

```powershell
hermes plugins enable coding-pi
$env:CODING_PI_DISPATCH_URL = "http://127.0.0.1:8787/api/coding-pi/dispatch"
```

The tool is named `coding_pi_dispatch`. It returns the Pi session id and
collab-web link immediately; the Hermes turn, group rooms, workflows, and
multiple Pi sessions remain independent and can run concurrently.
