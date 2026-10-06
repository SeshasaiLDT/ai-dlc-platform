# Identity, Initiative, and Resource Resolution

Status: Draft

## Purpose

AI-DLC must support multiple teams using the same reusable agents while keeping each user's authorized project scope and knowledge sources isolated.

The platform therefore separates three decisions:

1. **Who is the user?**
2. **Which initiatives and logical resources may that user access?**
3. **Where are those logical resources physically hosted in this environment?**

Agents do not answer these questions themselves.

## Resolution chain

```text
Authenticated user (Okta / enterprise IdP)
        ↓
Principal
        ↓
Initiative memberships
        ↓
Selected initiative / workspace
        ↓
Initiative Profile logical scopes
        ↓
Authorization-policy intersection
        ↓
Resolved Resource Context
        ↓
Tool / Knowledge adapters
        ↓
Physical Jira / Git / pgvector / S3 resources
```

### Example

A user may belong to:

- POS initiative
- AI Center of Excellence initiative

The user should not be hardcoded to `NEWPOS` or `AICOE` in agent code.

Instead:

- Membership says the user may enter POS and AICOE.
- POS Initiative Profile declares its allowed Jira projects and logical knowledge sources.
- AICOE Initiative Profile declares its own scopes.
- Authorization intersects membership/tool permissions with the selected initiative.
- Resource bindings resolve the selected initiative's logical resources to environment-specific Jira connections and pgvector locations.

## Jira resolution

Initiative configuration owns allowed Jira project scopes.

Example logical configuration:

```yaml
integrations:
  jira:
    enabled: true
    projects: [NEWPOS, DCTZ]
```

This does not contain Jira credentials.

An environment binding selects the approved Jira connection/site.

At runtime the Jira tool receives a resolved context such as:

```text
initiative_id = pos
allowed_projects = [NEWPOS, DCTZ]
connection_alias = corporate-jira
principal = user-123
permissions = [jira.read]
```

If a user belongs to multiple initiatives, the selected workspace determines which initiative scopes are active. Membership or policy may further narrow those scopes.

## Knowledge / pgvector resolution

Initiative Profiles own logical Knowledge Source IDs, not physical pgvector topology.

Example:

```text
pos-code
central-code
functional-concepts
design-documents
release-notes
```

A Resource Binding Registry maps each logical source to a retrieval binding.

A binding may include non-secret routing metadata such as:

- connection alias
- database/schema alias
- table or collection identifier
- logical namespace
- source type
- mandatory metadata filters
- embedding/index profile
- environment

Credentials and endpoints are resolved through infrastructure/secrets configuration rather than Initiative Profiles or agent prompts.

Examples of valid physical strategies:

### Current-style multi-table deployment

```text
POS initiative
  pos-code             → shared-rag / code table A
  central-code         → shared-rag / code table B
  functional-concepts  → shared-rag / docs table A
  design-documents     → shared-rag / docs table B
  release-notes        → shared-rag / docs table C
```

### Two company-wide pgvector services

```text
Company Code pgvector
  ├── POS code
  ├── Team B code
  └── Team C code

Company Documents pgvector
  ├── POS design docs
  ├── POS functional docs
  ├── Team B docs
  └── Team C docs
```

The same agent request remains:

```text
retrieve(
  initiative_id="pos",
  source_ids=["pos-code", "design-documents"],
  query="..."
)
```

Only resource bindings change.

## Isolation requirements

Every retrieval request must carry resolved initiative/user context.

The retrieval layer must enforce:

- initiative scope
- logical source scope
- repository/source restrictions
- user/tool permission
- mandatory metadata filters

The LLM must not be trusted to remember or invent these filters.

Cross-initiative retrieval must be explicitly authorized rather than occurring because two teams share the same HNSW index or PostgreSQL instance.

## Ownership

### Identity / RBAC layer
Owns:
- authenticated principal
- initiative membership
- role/capability/tool permissions
- optional user-level narrowing of project/tool scopes

### Initiative Profile
Owns:
- logical Jira projects
- repositories
- logical knowledge source IDs
- build profiles
- initiative policy/default declarations

### Resource Binding Registry
Owns:
- mapping logical resources to environment-specific physical resources
- pgvector topology bindings
- Jira connection aliases
- artifact-store aliases
- required retrieval filters

### Knowledge Service
Owns:
- retrieval API
- binding resolution
- pgvector query execution
- provenance and filtering
- hiding physical table/cluster details from agents

### Capability agents
Own:
- domain reasoning and output contracts

They do not own:
- user-to-project mapping
- Jira credentials/site selection
- pgvector instance selection
- table/namespace selection
- database connection logic

## Open infrastructure decision

The enterprise pgvector topology is deliberately unresolved.

Candidates include:

1. team/initiative-specific instances
2. one shared instance with multiple tables/namespaces
3. two company-wide stores: code and documents
4. another hybrid based on scale/security/performance

The platform should support all candidates through bindings. The final choice should be based on measured volume, isolation requirements, operational ownership, query performance, indexing behavior, and cost rather than agent design.
