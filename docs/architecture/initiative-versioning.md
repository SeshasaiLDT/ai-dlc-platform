# Initiative configuration history and rollback (AIDLC-22)

An Initiative Profile's `schema_version` describes the shape of its configuration.
A Registry configuration `revision` identifies one stored snapshot of that
profile for a particular initiative. Revisions start at 1 and increase by one
for each successful update or rollback. `RegisteredInitiative.current_revision`
identifies the current snapshot; `get_revision(id, number)` and
`list_revisions(id)` retrieve immutable history in ascending order.

`update` appends a revision while retaining the previous profile. `rollback`
copies a chosen historical profile into a **new** revision, recording
`source_revision` as the chosen revision. For example, after revisions 1, 2,
and 3, rollback to 1 creates revision 4 with revision 1's content. Revisions
2 and 3 remain queryable. Rollback to the current revision is a no-op with no
new timestamp or event. A missing target raises `InitiativeRevisionNotFoundError`.

Lifecycle status is independent of configuration: `disable` preserves the
current revision, and rollback does not reactivate a disabled initiative.
Readiness validation remains independent and is not automatically run on update
or rollback.

The Initiative-specific repository port combines the current-record and
revision append in one atomic operation, checking the expected current record.
The in-memory adapter locks this operation, preventing duplicate revision
numbers and lost writes in local use. A stale mutation raises
`InitiativeRevisionConflictError`; the caller may reload and retry. Future
PostgreSQL persistence will implement the same port and transaction boundary.
No database storage or distributed locking is implemented here. Mutation events
expose the revision numbers and rollback source, but event delivery is not yet
durably transactional with storage.

Future Workspaces must store the initiative configuration revision they were
created against. Registry updates and rollbacks do not mutate that stored
revision automatically. Workspace creation and migration are later work.
