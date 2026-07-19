# Smart Weather and Managed Node Boundaries

## Smart Weather

- There is no fixed `23:00-08:00` notification silence window.
- Weather queries and notifications are driven by current place, indoor/stationary state,
  motion, learned departure probability and time, destination, route duration/exposure,
  origin/destination/route weather, and learned notification usefulness.
- A user predicted to remain indoors does not receive a travel-weather alert solely because
  rain exists. A user predicted to leave at night can receive one when the trip is exposed.
- Incomplete multi-location weather suppresses the alert for that evaluation instead of
  presenting partial coverage as a complete trip forecast.
- Every valid alert remains visible until its validity interval ends; there is no six-hour
  display cutoff.

## Windows PC + WSL

`wsl` is one composite managed device, displayed as `Windows PC + WSL`:

- CPU, memory, disk, network, and uptime come from the Windows host observation.
- Hermes gateway, worker, and tunnel state come from the WSL runtime observation.
- The composite is online only when both observations are fresh and the required WSL runtime
  components are ready.
- A missing or stale component is unavailable, never a real `0%` metric. The UI shows `-`
  and offers reconnect.

## Mutual Recovery

Each node runs the independent recovery receiver outside the Hermes process it may restart:

```bash
python -m hermes_cli.managed_node_recovery_service \
  --host 127.0.0.1 --port 9121 \
  --config /path/to/managed-nodes.json
```

Expose the loopback receiver through an authenticated HTTPS reverse proxy or private HTTPS
control plane. Remote recovery URLs require HTTPS; plain HTTP is accepted only on loopback.

Example configuration on each host:

```json
{
  "nodes": [
    {
      "id": "home",
      "label": "Home Hermes",
      "status_url": "https://status.internal.example/live",
      "token_file": "/run/secrets/hermes-node-recovery",
      "recovery_urls": {
        "dbb3": "https://dbb3-control.internal.example/recover",
        "wsl": "https://windows-control.internal.example/recover"
      },
      "auto_recover": true,
      "recovery_cooldown_seconds": 90
    }
  ],
  "recovery_receiver": {
    "node_id": "wsl",
    "token_file": "/run/secrets/hermes-node-recovery",
    "command": ["/usr/local/sbin/recover-hermes-node", "wsl"]
  }
}
```

Use `node_id: "dbb3"` and the DBB3 recovery command on DBB3. Commands are local fixed argv
arrays; request bodies cannot supply a shell command. Recovery requests are token-authenticated,
persistently idempotent, and rate-limited by target-specific cooldowns.

Recovery covers a stopped Hermes service, worker, tunnel, or WSL instance when the peer control
plane is reachable. A powered-off or network-isolated physical machine needs a separate
Wake-on-LAN, smart-PDU, BMC, or equivalent out-of-band action in the configured recovery command.
