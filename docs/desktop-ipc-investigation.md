# Desktop IPC Investigation

## Verified on 2026-10-15

Desktop build: 26.1007.2314.0 (Windows).

The app-server daemon socket is not the only local integration surface.
The desktop has a separate cross-client router at `\\.\pipe\codex-ipc`.
Frames use a four-byte little-endian byte length followed by UTF-8 JSON.

The adapter in `codexbridge/codex/desktop_ipc.py` successfully:

- initialized with clientType `codexbridge` (version 0);
- discovered the current desktop thread owner using
  `thread-owner-discovery` (version 1);
- received `supportsUntrustedAppInput: true` from the desktop;
- disconnected without restarting or modifying the desktop.

No desktop installation file was changed or desktop process restarted.
Dedicated integration tasks verified new-task ownership, owner-routed start,
steer, interrupt and completion events. A no-tool request returned `BRIDGE_OK`.
A second returned a desktop-derived completion event without a polling thread.
Status reports `owner_routed` per known tasks, not a global shared daemon.

## Static Findings

The installed desktop's local daemon transport explicitly excludes Windows:
`process.platform !== win32`. Its active local app-server uses private stdio.
Starting another daemon alone therefore will not make this desktop share it.

The separate router supports:

- `thread-follower-start-turn` version 2, with conversationId and turnStart;
- `thread-follower-steer-turn` version 1;
- `thread-stream-state-changed` version 11;
- `thread-stream-following-changed` version 1;
- `query-cache-invalidate` version 0.

Stream messages carry desktop conversation snapshots or revisioned patches,
not raw app-server notifications. A fake turn/completed notification is not
a valid replacement. Desktop patches invalidate canonical reads and request a
fresh snapshot. Repeated following broadcasts were verified to send snapshots.
Empty new tasks must be committed by setting their name, and the creating
app-server must exit to release the active writer before desktop hydration.

## Remaining Work

The request shapes were checked against the installed desktop's handlers.
Live start, steer and interrupt were verified on a dedicated integration task.
A timeout after dispatch must never silently retry through
an independent app-server: the desktop might already have accepted the turn.
Following snapshots and terminal state transitions feed mobile SSE. Treat router availability separately from
shared execution. The protocol is private and version-specific.
