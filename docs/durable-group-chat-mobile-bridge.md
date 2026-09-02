# Durable Group Chat mobile bridge

## Purpose

The official durable Group Chat protocol keeps room authority, scoped peer
grants, replication, and failover in the gateway. A mobile account bearer token
is not an API-server key and must never be used to call `/v1/runs` or carry a
`HermesRoom` grant.

This bridge exposes the same-gateway user workflow through the authenticated
collaboration plugin while delegating every room operation to the process-owned
`HostedRoomService`. It does not start a separate scheduler or implement a
second event protocol.

It also exposes a narrow, owner-authenticated cross-gateway join path. The
phone submits only a configured `gateway_id` and `profile`; the manager reads
the operator's `bot_peers` entry and its `HERMES_PEER_<NAME>_KEY` secret, asks
the target gateway for the official scoped RoomLink invitation, probes the
returned catalog, and then registers the official `PeerMemberRoute`. Neither
the API-server key nor the scoped `HermesRoom` grant crosses the mobile BFF.

## Owner boundary

`hosted_room_mobile_owners` binds one live room id to one `owner_id` plus
`account_generation`. The table is an access index only: it does not mutate a
room's authority, roster, event log, grants, or policy state.

Every mobile room request validates that exact binding before reading or
mutating the room. Room creation derives its id from the owner, account
generation, and idempotency key, and the official room-create transaction
persists the owner binding before it wakes the worker. A later same-name
account generation therefore cannot access an earlier account's room, and a
failed binding write cannot leave a newly-created active room behind.

## Mobile endpoints

All endpoints are mounted below
`/api/plugins/collaboration/mobile/group-chat` and require normal owner-mobile
authentication.

| Operation | Route |
| --- | --- |
| capabilities | `GET /capabilities` |
| selectable gateways (secret-free) | `GET /gateways` |
| list/create rooms | `GET` / `POST /rooms` |
| room state / replay | `GET /rooms/{room_id}` and `GET /rooms/{room_id}/events` |
| send / rename / stop | `POST /rooms/{room_id}/messages`, `POST /rooms/{room_id}/rename`, `POST /rooms/{room_id}/stop` |
| retry / approval | `POST /rooms/{room_id}/tasks/retry`, `POST /rooms/{room_id}/tasks/approval` |
| confirmed disband | `DELETE /rooms/{room_id}` |

Creation accepts the official two-to-six-member roster. A member without
`gateway_id` (or with `gateway_id: "local"`) targets a local profile; a member
with `gateway_id: "<bot_peers name>"` targets that operator-registered peer and
may select a named profile. The target gateway must advertise RoomLink protocol
v2, direct text execution, a configured HTTPS endpoint, and a matching scoped
catalog. Invitations are made before room publication; if room creation or
route registration fails, the bridge revokes the pending grants and attempts to
disband the newly-created room while retaining the owner binding until cleanup
completes.

Sending uses the official idempotent user-event identifier. Deletion and account
deletion run the same order: stop and acknowledge work, revoke peer routes,
write the official disband tombstone, then remove the owner binding. A failure
leaves the binding in place, so authorization does not outlive cleanup.

The `/gateways` response deliberately separates RoomLink-capable gateways from
the current managed execution nodes (`dbb3`, `wsl`, `hk`). Those workers are
connector-only in the product topology and are shown as informational; they
cannot be selected as peer gateways until an independent Hermes API gateway
with RoomLink is deployed on that device.

For the four-device layout described in the deployment audit, keep the roles
separate: server 1 runs the `hermes-manager` authority/BFF, server 2 runs an
independent HTTPS Hermes gateway and is registered in `bot_peers`, Windows/WSL
is a connector-only worker, and DBB3 Linux is a connector-only worker. The
second server becomes a room member only after its RoomLink endpoint and
server-managed key are configured; Windows/WSL and DBB3 remain execution
lanes unless they are separately promoted to full gateways.

## Operator configuration

The manager uses the upstream peer registry. For example, the manager may
contain the following non-secret configuration:

```yaml
bot_peers:
  hk:
    url: https://hk-gateway.example.test
    note: Hong Kong gateway
    profiles: [default]
```

The matching API key is stored in the manager's secret scope as
`HERMES_PEER_HK_KEY`. On the target gateway, configure a strong
`API_SERVER_KEY` and an HTTPS `HERMES_ROOM_LINK_URL` (or the equivalent gateway
config setting). The BFF only returns `gateway_id`, labels, declared profiles,
and readiness reasons; it never returns the URL or either credential.

## Explicit non-goals

The bridge intentionally does not expose peer add/remove, arbitrary target URL
or key submission, RoomLink grant rotation, peer transport diagnostics,
replication, authority promotion/demotion, or remote failover. Those remain
administrator-managed upstream controls. Mobile creation can consume only
already-registered peers and the server performs the scoped invitation/probe
sequence on the account's behalf.

## Verification

`tests/gateway/test_hosted_rooms.py` covers the generation-scoped owner index.
`tests/plugins/test_mobile_group_chat_api.py` proves delegation, secret-free
gateway discovery, official invitation/probe/route registration, idempotent
send, same-order deletion, account deletion cleanup, minimum roster size, and
the absence of API-server or room-grant credentials in the mobile facade.
The upstream two-gateway UAT in
`tests/tui_gateway/test_hosted_room_two_gateway_scoped.py` exercises the real
API-server RoomLink transport boundary.
