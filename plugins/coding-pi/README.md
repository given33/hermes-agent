# Hermes Coding / Pi bridge

This bundled plugin runs the cloned `oh-my-pi` source tree through its public
RPC mode. The Pi repository stays external and unmodified; the Hermes server
owns authentication, account scoping, session directories, and the mobile SSE
transport.

For a deployment where the clone is not beside the Hermes release, add a
non-secret section to the server's `config.yaml`:

```yaml
coding_pi:
  repository: https://github.com/given33/hemres-pi.git
  ref: 3a8591a8af5b6d200088d12ca75a5517cb064fa8
  root: C:/path/to/hemres-pi
  bun_path: C:/path/to/bun
  workspace: C:/path/to/allowed/repository
  allowed_workspaces:
    - C:/path/to/allowed/repository
```

When `repository` and a 40-character `ref` are configured, Hermes verifies
that `root` is a Git checkout of that exact remote and commit before starting
Pi. GitHub credentials must come from the server's Git credential helper,
deploy key, or secret manager; never put a token in `config.yaml`.

Run Pi's own preparation commands once after cloning:

```text
bun install --frozen-lockfile
bun --cwd=packages/coding-agent run gen:tool-views
bun run build:native
```

Each authenticated Hermes owner/profile gets a separate Pi coding-agent
directory below `HERMES_HOME/coding-pi/`; the normal Hermes chat, group chat,
and workflow runtimes do not share that process or state.
