# Enterprise identity and initiative RBAC

Status: Implemented contracts (AIDLC-25)

## Identity

`Principal.subject_id` is the stable enterprise identity key. Display name and email
are descriptive and may change. `provider` identifies the normalizing identity
provider without exposing provider claims to business logic. Authentication and
token validation are separate work in AIDLC-26.

## Initiative membership

```text
Principal
  ├── Initiative A: analyst
  └── Initiative B: reviewer
```

An enabled `(principal_id, initiative_id)` membership is required before an
authorization context can be resolved. Memberships are independent, so a user
may enter multiple initiatives with different roles. Missing and disabled
memberships have distinct errors. Listing memberships includes disabled records
so callers can distinguish them; `get_membership` only returns enabled records.

## Roles and grants

Generic role identifiers are `viewer`, `analyst`, `developer`, `reviewer`,
`initiative_admin`, and `platform_admin`. Role names have no built-in agent
behavior. An injected `RolePolicy` maps each role to explicit capability, tool,
and administrative grants. An absent role grant yields no permissions. The
platform should configure only the grants required for each deployment.

Initiative administration and platform administration are separate permissions.
Neither admin role implicitly grants execution capabilities or tool access.
Administrative operations must still be checked against the selected initiative
or platform boundary by the later decision service.

## Capability authorization

Stable capability IDs are investigation, change impact, code analysis,
implementation, and verification. A resolved context exposes `can_use` for
these explicit grants. Capability agents consume the resolved context and do
not interpret identity claims or role names. LLM output cannot add grants.

## Tool authorization

Tool permission IDs distinguish Jira, Git, and ServiceNow read/write,
knowledge read, and artifact read/write. A capability grant does not grant a
tool. Read never implies write. The AIDLC-27 runtime decision service will
enforce these permissions at operation boundaries.

## Scope narrowing

The Initiative Profile supplies the configured logical scopes. Optional member
or policy restrictions may only narrow them. `None` means no additional
restriction for a scope; an empty set denies that scope. Effective scope is the
intersection, and the context rejects an allowed scope outside the profile.

```text
Initiative Profile configured Jira: NEWPOS, DCTZ
Member policy permitted Jira:       NEWPOS
Effective Jira:                     NEWPOS
```

The example project keys are documentation only. Production identity/RBAC code
contains no initiative project constants. A later service must also evaluate
operation policy, initiative lifecycle, approval requirements, and current
configuration before executing an action; this snapshot alone is not an
authorization decision for a live operation.

## Resource separation

Identity/RBAC does not know pgvector table names, database endpoints, S3 bucket
names, Git credentials, or Jira credentials. Initiative Profiles own logical
resource IDs; Resource Bindings resolve them to physical infrastructure later.
The membership repository port is storage neutral, with a deterministic
in-memory adapter. DynamoDB storage is later work.
