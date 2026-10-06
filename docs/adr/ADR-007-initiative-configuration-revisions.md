# ADR-007: Immutable Initiative Configuration Revisions

Status: Accepted

## Context

The Registry previously retained only the current Initiative Profile. Configuration
changes and rollback must remain auditable, and future Workspaces need a stable
identifier for the configuration they started with. Lifecycle disablement is a
separate concern from configuration changes.

## Decision

- Store every successful configuration change as an immutable, append-only
  `ConfigurationRevision`, numbered from 1 and increasing per initiative.
- Keep `current_revision` on the Registry registration; never place Registry
  revision metadata in the AIDLC-19 Initiative Profile schema.
- An update appends a revision. A rollback to a historical revision copies that
  profile into a **new** revision and records its `source_revision`. Rolling back
  to the current revision is a no-op.
- Disabling an initiative changes lifecycle state but adds no configuration
  revision. A rollback preserves lifecycle state, including disabled state.
- The repository port must atomically append history and replace the current
  registration after checking the expected current record. Historical revisions
  cannot be overwritten.
- Future Workspaces must store the initiative configuration revision they were
  created against. Registry updates and rollbacks do not mutate that stored
  revision automatically.

## Alternatives considered

- Move the current pointer backward on rollback: rejected because the rollback
  action and intervening configuration would disappear from the revision sequence.
- Use timestamps or schema semantic versions as revision IDs: rejected because
  neither provides a simple, per-initiative monotonic configuration identifier.
- Store lifecycle changes as configuration revisions: rejected because disablement
  does not change the Initiative Profile.
- Build a general event store: rejected as unnecessary for this boundary.

## Consequences

The Registry can retrieve historical profiles by a stable `(initiative_id,
revision)` pair. Rollback is auditable through both the new revision and its
mutation event. The in-memory adapter uses a lock to enforce the append check.
A future PostgreSQL adapter must implement the same append/current-record
operation in one transaction with an expected-current check. Event publication
remains outside that transaction until durable audit delivery is implemented.

## Revisit triggers

Revisit sequencing or retry behavior if concurrent persistent writers require
automatic retries, or if durable audit requirements change the transaction
boundary. Do not weaken append-only history or workspace revision pinning.
