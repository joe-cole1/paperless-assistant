# ADR 0008: Default to a Docker-managed data volume

- Status: Accepted
- Date: 2026-07-26
- Supersedes: the default bind-mount deployment choice in ADR 0007
- Issue: #20

## Context

ADR 0007 required a restricted writable bind mount so operators could back up SQLite state.
The hardened container runs directly as non-root UID/GID `10001:10001` with a read-only root
filesystem and no Linux capabilities. A host directory created by Docker or Synology Container
Manager is commonly owned by root, so the assistant cannot enforce mode `0700` and fails closed
at startup until an operator fixes numeric host ownership manually.

This ownership ritual is unsuitable for the default household installation. Starting as root to
change the bind mount and then dropping privileges would add startup privilege and capabilities
solely to accommodate host storage.

## Decision

Create an empty `/data` directory owned by `10001:10001` with mode `0700` in the image. Mount an
explicitly named Docker-managed volume, `paperless-assistant-data`, there by default. Docker
initializes a new volume from the image directory, so the application can start as its final
non-root identity without changing host filesystem ownership.

Keep a host bind mount as an advanced operator option for installations that require direct
host-managed backup. Those operators must create a local Unix filesystem directory and assign it
to UID/GID `10001:10001` before startup.

## Consequences

The default Compose deployment requires no data-directory preparation and preserves the existing
read-only, capability-free, non-root trust boundary. The explicit volume name remains stable when
the Compose project directory or project name changes.

The volume is less visible to host file-management tools than a bind mount. Container recreation
and `docker compose down` preserve it, but explicit volume removal, including
`docker compose down -v`, deletes SQLite recovery, idempotency, context, warning, and audit state
plus transient files. Documents already accepted by Paperless remain in Paperless. Backing up the
SQLite database is optional operator policy and uses a short service stop for a consistent copy.

## Alternatives considered

- Start as root, repair ownership, and drop privileges: rejected because it expands runtime
  privilege and still fails on filesystems that do not support normal Unix ownership.
- Keep the bind mount default with clearer instructions: rejected because first startup would
  still require SSH and numeric ownership knowledge.
- Make state entirely ephemeral: rejected because upload recovery and event idempotency are
  safety behavior, not merely historical logging.
