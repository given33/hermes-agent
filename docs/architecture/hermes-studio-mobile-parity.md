# Hermes Studio Mobile Experience Parity

## Purpose

This document is the product contract for bringing the useful interaction and
information architecture of `EKKOLearnAI/hermes-studio` to Hermes iOS while
keeping our existing Hermes Agent runtime, hosted workflow, account boundary,
Apple integrations, and server-owned state.

The source audit is pinned to Hermes Studio commit
`841ab4a8e6a56d09407855dc10d6afa2d1bb8125` (v0.6.33, 2026-07-24). Its current
Business Source License 1.1 permits non-production use under the additional
grant but requires a separate commercial license for a commercial embedded or
hosted product until the 2029-05-10 Apache 2.0 change date. Therefore the iOS
implementation follows a clean-room parity contract: behavior, hierarchy, and
visual principles are reproduced with our React Native components and APIs;
Hermes Studio source files are not vendored into the production repositories.

## Visual Language

| Area | Hermes Studio behavior | Hermes iOS mobile contract |
| --- | --- | --- |
| Palette | Restrained black, white, and neutral gray | One continuous sidebar surface, one main surface, thin neutral separators |
| Themes | Light, dark, and comic | Ink light/dark first; comic remains an optional font treatment rather than a separate product shell |
| Radius | 6-8 px | 6-8 px for bubbles, fields, sheets, and repeated items |
| User bubble | Neutral gray | Same neutral family as assistant bubbles; role comes from alignment, avatar, and label rather than green fill |
| Assistant bubble | Neutral gray with readable Markdown | Same surface with distinct sender header, runtime state, model, and actions |
| Typography | Inter/system; optional Comic Neue, ZCOOL KuaiLe, Zen Maru Gothic, Gaegu | iOS system stack for production legibility; licensed/provenance-approved bundled faces may be selected in Display settings |
| Code | Monospace, syntax highlighting, copy action | Scrollable code block, language label, copy, no nested decorative cards |
| Motion | 150 ms control and 250 ms page transitions | Native Reanimated navigation, 160-260 ms, interruptible, Reduce Motion aware |
| Density | Flat sections and thin rules | No page-section cards; compact rows, stable touch targets of at least 44 pt |

## Complete Feature Inventory

Priority meanings: `P0` is required for the first preview, `P1` follows after
preview approval, and `P2` is useful but desktop-specific or lower priority.

### Shell And Navigation

| Priority | Capability | Mobile mapping |
| --- | --- | --- |
| P0 | Global navigation sidebar | Single continuous drawer background with profile header, routes, and live server footer |
| P0 | Context sidebar for sessions or rooms | Push screen on compact iPhone; optional second column on iPad |
| P0 | Current Profile and model identity | Compact header selector; long press copies Profile/model information |
| P0 | Connection and version indicator | Real server, DBB3, and WSL health only; explicit refresh and last-observed time |
| P0 | Search and command entry | Native search in sessions, rooms, files, models, skills, and settings |
| P0 | Light/dark system appearance | Follows iOS by default and supports an explicit override |
| P1 | Comic appearance | Display preference using approved bundled font assets |
| P1 | iPad split view | Navigation, collection, and detail columns without duplicating state |
| P2 | Desktop pet and title bar controls | Omitted from iPhone; they have no mobile workflow value |

### Direct Chat And Sessions

| Priority | Capability | Mobile mapping |
| --- | --- | --- |
| P0 | Streaming single-agent chat | Server snapshot plus SSE, incremental polling fallback, optimistic user message |
| P0 | Session create, rename, delete, switch | Swipe/context actions with confirmation for deletion |
| P0 | Session search | Full text query with profile and date filters |
| P0 | Markdown and syntax rendering | Native Markdown, fenced code, links, lists, tables, and copy |
| P0 | Message copy | Copy body; an action sheet also copies sender, model, or complete message information |
| P0 | Quote/reference message | Long press then Reply, with a compact referenced-content strip |
| P0 | Message timing | Created time, current phase timer, first-token boundary, terminal elapsed time |
| P0 | Reasoning display | One collapsible reasoning section showing the complete reasoning text supplied by the backend |
| P0 | Tool activity | One ordered collapsible activity timeline; terminal shows the command, other tools show concise result detail |
| P0 | Retry and cancellation | Five bounded model connection attempts, specific final provider error, durable cancellation |
| P0 | Attachments | Pick, preview, upload, retry, download, share, and server-side persistence |
| P0 | Background hosting | Work continues after iOS suspension or termination and restores from the authoritative server cursor |
| P1 | Voice playback and input | Native speech controls backed by configurable TTS/STT providers |
| P1 | Session fork and compression | Existing mobile endpoints surfaced as message and session actions |
| P2 | Desktop workspace/browser side panel | Presented as a full-screen mobile workspace route or bottom sheet |

### Group Chat And Hosted Workflow

| Priority | Capability | Mobile mapping |
| --- | --- | --- |
| P0 | Multi-participant timeline | User, Hermes, Hermes Manager, workers, reviewer, and reporter each have stable identity and avatar |
| P0 | Hermes Manager planning | Internal profile remains `dbb3-manager`; display name is `Hermes 调度员` |
| P0 | Worker/reviewer/reporter handoff | Ordered stages, handoff target, status, elapsed time, and rework round |
| P0 | Live progress | Typing/thinking/executing/reviewing/reporting states driven only by persisted server events |
| P0 | Rework visualization | Reviewer rejection and each repair round remain visible in the timeline |
| P0 | Structured final report | Main server Reporter uses verified Manager handoff and publishes one final result |
| P0 | Participant information copy | Long press avatar/name to copy participant, Profile, role, node, model, and provider |
| P0 | Workspace changes | Changed-file summary, additions/deletions, diff preview, and generated artifact links |
| P0 | Resume at any point | Login fetches a snapshot then resumes at the next append-only event cursor |
| P1 | Room create, rename, clone, and delete | Mobile room management sheet using the existing account-scoped store |
| P1 | Agent add/remove and @mentions | Profile selector and mention suggestions; permissions stay account scoped |
| P1 | Context compression status | Token count and compression progress in room details |
| P1 | Invite codes and multi-user members | Added only when product accounts support shared rooms; not simulated with local test users |

### Model, Provider, Profile, And Account

| Priority | Capability | Mobile mapping |
| --- | --- | --- |
| P0 | Provider CRUD | Base URL, key reference, compatibility mode, save feedback, and delete |
| P0 | Fast model discovery | `/v1/models` detection inside the Detect Models action with timeout and cancellation |
| P0 | Model test | Shows exact 401/403/404/429/5xx/timeout/offline result without manufacturing a model response |
| P0 | Default and per-session model | Selector displays provider and context metadata; selection is persisted by server |
| P0 | Profile create, clone, rename, delete | Existing isolated Hermes homes remain the authority |
| P0 | Profile avatar and description | Local cached avatar asset with server identity metadata |
| P0 | Key inventory | Shows keys created through Model settings as masked references; delete revokes the server secret |
| P0 | Account identity | Avatar, username, role, session security, encrypted export, and deletion |
| P1 | OAuth providers | OpenAI Codex and Nous flows surfaced when backend reports support |
| P1 | User administration | Super-admin-only user list, role, disable, reset, and audit actions |

### Settings

Hermes Studio's tab layout becomes a searchable iOS settings list with detail
screens. All values are server sourced unless explicitly marked device local.

| Priority | Section | Included controls |
| --- | --- | --- |
| P0 | Account | Identity, avatar, server, session, export, deletion, sign out |
| P0 | Display | System/light/dark, approved font, text size, density, Reduce Motion |
| P0 | Proxy | Address, status, test, bypass list; secrets stay in the restricted backend store |
| P0 | Agent | Profile, model, max turns, tool policy, workspace, gateway status |
| P0 | Memory | Provider, enabled state, retrieval settings, memory inspection |
| P0 | Compression | Trigger tokens, history budget, tail messages, manual compression |
| P0 | Session | Reset policy, resume/fork, context usage, retention behavior |
| P0 | Privacy | Diagnostic redaction, upload policy, location/health scopes, account export/delete |
| P0 | Models | Providers, keys, discovery, test, grouping, default model |
| P1 | Voice | TTS/STT provider, voice, speed, pitch, test phrase, recording test |
| P1 | Users | Super-admin account and access management |
| P1 | Notifications | APNs, quiet hours, completion, weather, and live activity controls |
| P1 | Apple data | Location, Motion, Health, Watch, Screen Time, calendar, reminders, and permission status |

### Files And Workspace

| Priority | Capability | Mobile mapping |
| --- | --- | --- |
| P0 | Categorized file library | Recent, uploads, generated, images, documents, code, and other |
| P0 | File operations | Upload, download, share, rename, move, copy, delete, and new folder |
| P0 | Preview | Images, PDF, text/code, Markdown, HTML, office metadata, and unsupported-type fallback |
| P0 | Generated artifact provenance | Conversation, turn, role, hash, size, and created time |
| P0 | Encrypted export | No full plaintext account export is written to cache |
| P1 | Remote workspace browser | Server, DBB3, or WSL scope with explicit path and node status |
| P1 | Diff editor | Read-only first; write approval gates any mutation |

### Operations And Agent Capabilities

| Priority | Capability | Mobile mapping |
| --- | --- | --- |
| P0 | Kanban | Root task, child tasks, assignees, evidence, reviewer status, and progress |
| P0 | Workflows | Visual stage list on iPhone; graph view on iPad/web preview |
| P0 | Jobs | Create, edit, pause, resume, run now, and execution history |
| P0 | MCP | 21 iOS MCP services, 44 tools, scopes, health, version, and recent calls |
| P0 | Skills and memory | Search, inspect, enable, disable, and usage history |
| P0 | Devices | Main server, DBB3, WSL, iPhone, and Watch from real heartbeat timestamps |
| P0 | Logs and health | Refreshable source-specific logs and health; no fixture status |
| P1 | Usage analytics | Input/output/cache tokens, cost, models, sessions, and 30-day trends |
| P1 | Channels | Telegram, Discord, Slack, WhatsApp, Matrix, Feishu, WeChat, and WeCom status/configuration |
| P1 | Coding agents | Launch, observe, cancel, and retain output/reasoning through the hosted workflow |
| P2 | Full interactive terminal | Read-only command activity first; interactive shell remains an explicitly privileged tool |

### Apple-Specific Product Features Retained

Hermes Studio parity does not replace the product capabilities already unique
to Hermes iOS: Smart Weather and MapKit, Core Location and trajectory capture,
Core Motion, HealthKit, WatchConnectivity, Screen Time, EventKit, battery and
charging context, BackgroundTasks, APNs, Live Activities, App Intents, and the
independent iOS MCP supervisor. These remain first-class sidebar routes and use
the same neutral visual system.

## Mobile Message Presentation Contract

Every public collaboration message projects stable top-level fields instead of
requiring a client to infer them from historical metadata:

```json
{
  "sender_id": "dbb3-manager",
  "sender_name": "Hermes 调度员 · 规划",
  "sender_role": "dispatcher",
  "role_stage": "manager_planning",
  "role_label": "Hermes 调度员 · 规划",
  "profile": "dbb3-manager",
  "provider": "openai",
  "model": "gpt-test",
  "model_display": "openai · gpt-test",
  "status": "completed",
  "created_at": 1000,
  "started_at": 1000,
  "completed_at": 4500,
  "duration_ms": 3500,
  "handoff_to": ["dbb3-worker"],
  "activity_count": 0,
  "copy_context": {
    "version": 1,
    "sender": {},
    "model": {},
    "workflow": {},
    "timing": {}
  }
}
```

The server redacts activity secrets before projection. The client may copy the
message body, sender block, model block, or a formatted combination, but must
never include authorization headers, API keys, tokens, cookies, or raw secret
values.

## iPhone Interaction Rules

- The primary iPhone hierarchy is drawer -> collection -> detail. Returning
  from a feature detail returns to its collection, not directly to chat.
- Edge-swipe back, native back buttons, and keyboard dismissal share one stack
  transition and cannot diverge.
- Long press opens message actions; it does not trigger navigation or scroll to
  the newest message.
- Expanding reasoning or tool detail preserves the current scroll anchor.
- Automatic bottom scrolling occurs only when the user is already near the
  bottom or after the user sends a new message.
- Optimistic user messages appear immediately and are reconciled by request ID;
  a failed server request remains visible with retry/error state.
- Avatars are bundled or cached local assets with a stable fallback, so route
  changes never render an empty participant image.
- Safe areas form part of one continuous surface. The home indicator is not
  placed in a separate visual block.
- Accessibility labels, Dynamic Type, VoiceOver order, Reduce Motion, color
  contrast, and 44 pt hit targets are release requirements.

## Delivery Boundary

Backend changes, API contracts, and this document belong in `hermes-agent` and
are tested and pushed normally. The React Native parity implementation remains
local in `hermes-ios` until the Expo preview is approved. A future production
merge must either retain the clean-room implementation or document a separate
commercial Hermes Studio license before vendoring any upstream source or font.
