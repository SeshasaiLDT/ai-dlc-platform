# Initiative Registry (AIDLC-20)

The Initiative Registry is the application boundary for registered initiative
configuration. Future control-plane, agent, policy, and workspace consumers use
`InitiativeRegistry`; they do not read profile YAML or storage records directly.
The Registry operates on the AIDLC-19 `InitiativeProfile` contract without
changing that contract.

## Profile and registration

An `InitiativeProfile` describes an initiative's versioned configuration.
`RegisteredInitiative` wraps the immutable profile with Registry-owned
`status`, `created_at`, `updated_at`, and `current_revision`. The stable external ID is always
`profile.initiative.id`; it cannot change on update. Timestamps are aware UTC.

New registrations are `active`. `disable(id)` retains the profile and marks the
registration `disabled`. Disabling an already disabled initiative returns the
same record, leaving its timestamp unchanged and emitting no second event.
Updating a disabled registration replaces its profile while keeping it
disabled. This service does not re-enable or delete registrations. Configuration
revision history is separate from lifecycle state.

## Application API and storage boundary

`InitiativeRegistry` offers `create(profile)`, `get(id)`, `list(status=None)`,
`update(id, profile)`, and `disable(id)`. It also offers `get_revision(id, number)`,
`list_revisions(id)`, and `rollback(id, target_revision)`. It returns typed, immutable
`RegisteredInitiative` values. `list` orders by stable ID. Duplicate creation,
missing IDs, and identity-changing updates raise explicit Registry exceptions.

The service depends on an Initiative-specific `InitiativeRepository` protocol:
`insert`, `find_by_id`, `list_all`, and lifecycle-only `replace` operations. The revision
extension of this port atomically appends configuration history with the current
registration and checks the expected current record. The current
`InMemoryInitiativeRepository` is suitable for tests and local development. A
future PostgreSQL adapter can implement the same port without changing Registry
consumers. There are no database models or migrations in AIDLC-20.

At a local composition boundary, a caller may load a YAML profile and register
it. Consumers receive the Registry instance, not the YAML path:

```python
from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.application.initiatives import InitiativeRegistry
from ai_dlc.domain.initiative import load_initiative_profile

registry = InitiativeRegistry(InMemoryInitiativeRepository(), InMemoryRegistryEventSink())
profile = load_initiative_profile("configs/initiatives/examples/travel-platform.yaml")
registry.create(profile)
registered = registry.get(profile.initiative.id)
```

The loader is for bootstrap/local composition. Production registration and
authorization flows belong to later stories.

## Mutation events

Every successful create and update emits one `RegistryMutationEvent`. A disable
emits one event only when it changes active to disabled. Reads, failed
mutations, and repeated disables emit none. Events contain type, stable ID,
the operation's UTC timestamp, profile schema version, and before/after status.
The event sink is an injected `RegistryEventSink`; the in-memory sink supplies
an immutable event snapshot for tests and local work. Rollback emits
`initiative_rolled_back` with new, prior, and source revision metadata. Create,
update, and disable events identify the current revision.

This is an observable application boundary, not the immutable enterprise audit
trail. Event publication follows repository mutation and is not yet
transactional with storage. AIDLC-66 must address durable, reliable audit
delivery when persistent storage is introduced.

The original Registry boundary and lifecycle separation were established in
AIDLC-20. [ADR-007](../adr/ADR-007-initiative-configuration-revisions.md) records
the additional append-only revision and rollback semantics.
