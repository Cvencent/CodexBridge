# Device Command Protocol (Schema 1)

This command model is device independent. HTTP phones and TCP devices use the
same dispatcher. The existing firmware protocol remains supported.

Authenticated HTTP discovery: `GET /api/companion/protocol`.
Authenticated HTTP commands: `POST /api/companion/commands`.
TCP devices send the same envelope with `type: command` after pairing.

```json
{"type":"command","request_id":"device-42-0001","method":"turn.start","params":{"thread_id":"THREAD_ID","text":"Continue"}}
```

Successful responses have `type: command_result`, the original `request_id`,
`method`, `ok: true` and `result`. TCP failures have `ok: false` and `error`;
HTTP failures use the existing error envelope and non-2xx status.

Capabilities are checked on every command, including retries. TCP permissions
are supported capabilities intersect enabled capabilities. The new `control`
capability is opt-in, not automatically granted to existing firmware. Paired
phones preserve legacy permissions until they negotiate. Phones can POST
`{"supported":["status","tasks"]}` to `/api/companion/capabilities`; the saved
intersection applies to commands, legacy routes and event payloads.

| Capability | Methods |
| --- | --- |
| status | status.read |
| tasks | threads.list, thread.read, models.list, projects.list |
| control | thread.start, thread.rename, turn.start, turn.steer, turn.interrupt |
| approval | approvals.list, approval.resolve, approval.respond |

The discovery catalog lists required and optional fields. Unknown methods,
unknown fields, invalid types and missing permissions are rejected.

Use a new request ID for each logical operation. Retries of the same operation
must reuse both its ID and payload. The dispatcher caches the most recent 1000
requests in memory, scoped to device ID. Reusing an ID with a different payload
returns `request_id_conflict`. After an execution exception, retry returns
`outcome_unknown`; it does not execute the operation again. This is a bounded
process-lifetime guarantee, not persistent exactly-once delivery. Do not retry
an uncertain write after a server restart without reconciling task history.

TCP lines remain at most 768 bytes. Large results use `command_chunk` envelopes
with `request_id`, zero-based `seq`, `total` and `data`. Collect every chunk in
sequence and parse their concatenated data as the original JSON response.
TCP long requests use `command_request_chunk` with the same fields. Each device
has one in-flight assembly, maximum 512 ordered chunks, 64 KiB total, 30-second
expiry and 600 UTF-8 bytes of data per chunk (the encoded line must still fit).
The assembled JSON must be a command with the same request ID.

TCP discovery uses `{"type":"protocol_request","request_id":"catalog-1"}`.
Subscribe with `{"type":"events_subscribe","after":"EVENT_ID"}` and stop with
`{"type":"events_unsubscribe"}`. Event envelopes are `device_event` with an
`event` object; large envelopes use the same chunk mechanism. Events are filtered
using current enabled capabilities. Disconnect cleans up the subscription.
Intermediate content visibility is host-global and defaults to off. With
`show_intermediate_events=false`, commentary, reasoning, command output, file
changes, tool calls, plans and context notices are filtered on the host before
they reach TCP devices or HTTP phones; approvals, errors and status events remain.
`GET /api/companion/status` exposes the current flag for clients that display it.
HTTP events use `/api/companion/events`, stable event
IDs and `Last-Event-ID`; `resync_required` means refetch authoritative state.

Phone-created tasks are committed with a neutral placeholder so Codex Desktop
can hydrate them, then the first user message is used to persist a concise title
through `thread.rename`. Renaming publishes `thread_name` plus `threads_changed` and
invalidates the desktop task-list queries. Desktop IPC is an internal adapter, not
part of this public protocol. An owner
is discovered before forwarding existing-thread writes. Once a write is sent
to that owner, an error must not trigger independent-process fallback. Desktop
stream following and mobile-created task ownership were verified against desktop
26.1007.2314.0 on Windows. New task creation commits a named task using a short-lived
connection, releases its writer, then opens the desktop thread URI and discovers
its owner. This switches the desktop to the new task. Router connectivity alone
must never be reported as global shared realtime execution.

`approval.respond` requires `approval_id` and a `response` object. Input responses
use `{"answers":{"question-id":{"answers":["choice"]}}}`. MCP responses use
`{"action":"accept","content":{}}` (or decline/cancel). Permission responses use
`{"permissions":{},"scope":"turn"}`; empty permissions grant nothing. Command
and file approvals retain the explicit `decision` form. Invalid response types
leave the request pending. Desktop requests are routed back to their owner.
