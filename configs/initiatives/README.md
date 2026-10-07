# Initiative Profiles

An Initiative Profile is the versioned, human-authored contract for onboarding a
software product or operational domain to AI-DLC. It declares identity and team
ownership, integration scopes, repositories, knowledge source identifiers,
optional logical artifact-store identifiers, build/test commands, initiative
policy settings, and logical model-role defaults.
Shared platform code reads the same contract for every initiative. See the
[travel platform](examples/travel-platform.yaml) and
[field operations](examples/field-operations.yaml) profiles for different shapes.

The profile contains **no credentials**, service endpoints, AWS resource names,
database settings, concrete model/provider assignments, or environment-specific
values. Configure those through environment and secret management later. It also
does not define runtime authorization or grant permission to a user or agent:
the control plane must enforce identity, membership, policy, and approvals.
Repository `access` describes an initiative's intended integration scope; a
`read_write` repository may still have Git writes disabled by policy. This is
deliberate because permissions can be narrowed without changing repository
registration.

An `artifacts.stores` entry declares only a stable logical `id`. The
[Resource Binding Registry](../../docs/architecture/resource-resolution.md)
maps it to an environment-specific bucket alias and contained prefix after
authorization. The Profile never contains a bucket or storage credential.

Version `1.0` is the only supported version. The loader rejects other versions
with an explicit error. New optional fields can be added compatibly; removing or
changing the meaning of existing fields requires a later schema version and an
explicit migration/compatibility decision. Unknown fields are rejected so typos
and secret-like fields cannot silently enter the canonical contract.

Install with Python 3.12:

```sh
python3.12 -m pip install '.[dev]'
python3.12 -m ai_dlc.domain.initiative configs/initiatives/examples/travel-platform.yaml
```

The reusable API is `load_initiative_profile(path)`. It returns an
`InitiativeProfile` or raises `ProfileValidationError` with field paths such as
`integrations.git.repositories[0].default_branch`. Generate the current JSON
Schema from the same Pydantic model:

```sh
python3.12 -c 'from ai_dlc.domain.initiative import export_json_schema; export_json_schema("initiative-profile.schema.json")'
```

The JSON Schema describes fields and constrained values. Use the Python loader
for cross-field rules such as unique IDs, ownership references, and enabled
integration requirements.

To onboard another initiative, add a YAML file with a new stable initiative ID,
its teams and primary owner, and only the integrations, sources, profiles, and
policies it needs. Validate the file before passing its typed profile to the
[Initiative Registry](../../docs/architecture/initiative-registry.md). Adding
such a file must not require changes to shared
orchestrator, harness, or capability-agent source code.

The [Atlas Travel sample](samples/atlas-travel.yaml) is a stable synthetic
integration/demo fixture, distinct from the `examples/` schema demonstrations.
Its intended use and synthetic identifiers are documented in the
[sample initiative guide](../../docs/architecture/sample-initiative.md).
