# ADR 0016: Serialize SQLite work off the event loop

Status: Accepted

Date: 2026-07-30

Issue: #71

## Context

The repository ports are asynchronous, but the SQLite adapter previously executed synchronous
connection, migration, query, transaction, and filesystem-permission work directly on the Discord
event-loop thread. A slow lock, migration, commit, or storage operation could therefore delay
heartbeats and every unrelated Discord interaction.

Moving individual calls to a generic thread pool would permit concurrent operations, weaken the
single-worker ordering assumptions, and risk accidentally sharing SQLite objects across threads.
The adapter must preserve WAL mode, busy timeout, transaction boundaries, compare-and-swap job
transitions, lease behavior, encryption, and fail-closed migration errors.

## Decision

Each `SQLiteRepository` owns one dedicated single-thread executor. Every database operation,
including initialization and migrations, runs to completion on that worker. Connections remain
operation-scoped and are created, used, committed or rolled back, and closed on the same worker
thread. Repository calls are therefore serialized in submission order without exposing SQLite
objects outside the worker.

The public repository ports remain asynchronous. Their adapter methods use one typed boundary to
submit synchronous implementations to the worker. The event loop observes worker completion with
a short asynchronous poll. This avoids relying on delayed cross-thread loop wakeups, which are not
reliable in the supported managed Python runtime, while keeping the event loop available for
heartbeats and other tasks.

Cancellation of an awaiting task does not interrupt an in-flight SQLite transaction. The worker
finishes that operation so atomicity is preserved. Shutdown stops accepting new operations,
places a barrier behind already-submitted work, waits asynchronously for the barrier, and then
joins the idle worker. Repeated shutdown is safe; new calls after shutdown fail explicitly.

## Consequences

- Slow SQLite work no longer blocks Discord heartbeats or unrelated interactions.
- One repository intentionally processes only one SQLite operation at a time.
- Existing WAL, busy-timeout, lease, migration, encryption, and transaction behavior is retained.
- Callers must await `close()` during normal shutdown. A finalizer is only a best-effort fallback
  for abandoned repository instances.
- Horizontal replicas remain unsupported and require a different coordination and storage design.

## Alternatives considered

- Keep synchronous SQLite on the event loop: rejected because storage latency can stall Discord.
- Use the event loop's generic executor per call: rejected because ordering and thread ownership
  would be implicit and completion wakeups can hang in the supported runtime.
- Share one long-lived SQLite connection across arbitrary threads: rejected because it weakens
  thread confinement and complicates transaction ownership.
- Replace SQLite with a network database: rejected as outside issue scope and unnecessary for the
  supported single-worker deployment.
