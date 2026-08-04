# Mobile-hosted collaboration architecture

## Runtime ownership

`given33/hermes-agent` is the official Hermes Agent history plus product-owned
server extensions. Hermes iOS is a native client; it does not embed a separate
agent runtime. The same approved `given33/hermes-agent` commit runs on the main
server, DBB3, and WSL so the product extensions and official capabilities stay
compatible.

The main server owns account authentication, durable conversations, task state,
append-only progress, files, notifications, and the final user-facing answer.
DBB3 and WSL are replaceable execution nodes. A phone disconnect never owns or
cancels a server task.

## Product routing contract

Hosted execution is a durability and transport mechanism, not a user-visible
conversation mode. Both ordinary chat and collaboration turns may use the
hosted-turn API so they survive app suspension, network loss, or a killed phone.
The client must never label an ordinary chat as hosted work merely because that
transport was used.

- Ordinary conversation, explanation, translation, summary, search, short
  confirmations, numeric replies, and single-Hermes single-step requests stay
  in the normal chat surface.
- Concrete multi-step execution, code or system mutation, deployment, testing,
  cross-device work, long-running work, or requested deliverables enter the
  Manager -> Worker -> Reviewer -> Reporter collaboration workflow.
- Completed or failed collaboration history does not promote a later ordinary
  turn. Only a current non-terminal work turn may activate the group surface.
- Reporter and workflow labels are reserved for collaboration turns. A simple
  chat failure remains a Hermes chat failure and must not be presented as a
  hosted task or final report.

For a simple chat, the visible wait states are authoritative and ordered:

`sending -> reconnecting (1/5 ... 5/5) -> thinking -> first token starts elapsed time -> completed | failed`

HTTP acceptance only means the durable message was stored; it does not mean the
model received it. `thinking` starts after model readiness succeeds but carries
no elapsed timer. The timer starts exactly when the first model token is
persisted. Intermediate runtime details remain collapsed by default.

## Task flow

```mermaid
flowchart TD
    A["Hermes iOS submits a message"] --> B["Main server persists task"]
    B --> C{"Server intent routing"}
    C -->|simple| D["Main server default Hermes"]
    C -->|complex| E["Hermes Manager plans and splits on DBB3"]
    E --> F["DBB3 Worker"]
    E --> G["WSL PC Worker"]
    F --> H["Reviewer, DBB3 by default"]
    G --> H
    H -->|rework| E
    H -->|approved| I["Hermes Manager structured handoff"]
    I --> J["Main server Reporter"]
    D --> K["Persist result, files, events, APNs"]
    J --> K
    K --> L["Hermes iOS resumes at any time"]
```

The Hermes Manager handoff contains the task objective, plan, worker results, reviewer
decision, rework history, file hashes, unresolved items, and a recommended
conclusion. The main server Reporter only summarizes verified handoff data. It
does not rerun the task or invent missing evidence.

## Durable state contract

The authoritative lifecycle is:

`accepted -> routing -> manager_planning -> dispatching -> worker_running -> reviewing -> rework -> manager_handoff -> reporting -> completed | failed | cancelled`

Every transition increments the conversation event cursor and is persisted
before the API acknowledges it. Hermes iOS loads an authoritative snapshot,
then resumes `/hosted-events` using the last cursor. SSE is primary; bounded
incremental polling is the fallback. Local mobile cache is never the authority.

If iOS exits, locks, loses network, or is killed, execution continues. If every
execution node is offline, the durable task pauses until a node reconnects. A
future main-server cloud worker may provide execution capacity during that
window without changing the persistence contract.

## WSL residency

WSL starts locally from Windows and does not depend on DBB3. Inside
`HermesUbuntu`, systemd runs the Hermes gateway and the `pc-cloud-connector`
user unit. Linger keeps the `hermes` user manager alive, service restart policy
recovers crashes, and the Windows scheduled task starts the distro after login.
The managed-node watchdog verifies health; it does not become the normal start
path.

## Upstream releases

The daily upstream workflow discovers the newest official tag and creates
`upstream-sync/<tag>`. It calculates direct fork overlap plus iOS/API and
deployment risk, merges only into that branch, runs product preflight, and opens
a pull request. The pull request runs the complete official CI matrix.

Codex review is mandatory. A conflict-free merge is not proof of product
compatibility. Changes affecting authentication, hosted conversations, MCP,
dashboard APIs, dependencies, or deployment must be reviewed explicitly. iOS
adaptations remain a product decision. Only an approved `main` commit may be
deployed transactionally to the main server, DBB3, and WSL.
