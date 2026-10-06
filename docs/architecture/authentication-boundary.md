# Authentication integration boundary

Status: Application contracts implemented (AIDLC-26)

## Authentication responsibility

```text
transient credential
        ↓
provider validation and claim normalization
        ↓
ValidatedIdentityClaims
        ↓
AuthenticationService
        ↓
existing Principal
```

`AuthenticationService` accepts an injected `AuthenticationProvider`. Only that
adapter sees the credential and provider-specific claims. It must validate a
credential server-side **before** returning `ValidatedIdentityClaims`. A
production OIDC adapter must verify signature, trusted issuer, intended
audience, expiry, not-before, and a required stable subject. The application
service also rejects missing/blank subjects and malformed normalized fields.
No token parsing or cryptographic validation is implemented in this story.

The `DeterministicTestProvider` models the result of completed validation for
tests only. It accepts supplied test claims, ignores unrelated claim keys,
can return typed failures, and does not validate real credentials. It must
never serve live traffic.

## Authentication vs authorization

```text
Authentication: "Who are you?"        → Principal
Authorization:  "What may you do?"     → later AIDLC-27 decision service
```

Authentication does not query initiative membership, resolve roles, grant
capabilities or tools, load Initiative Profiles, or resolve Resource Bindings.
`Principal.subject_id` is the stable identity key; display name and email are
descriptive. The provider label identifies the normalized source without
exposing provider-specific claim formats downstream.

## Provider boundary and backend propagation

```text
Okta / another provider
       ↓
provider adapter
       ↓
AI-DLC Principal
       ↓
AuthenticatedRequestContext in backend calls
       ↓
later resolved authorization / execution context
       ↓
agents
```

Backend entrypoints can call `authenticate` for a `Principal`, or
`authenticate_request` for an immutable context containing only that
principal. The later authorization service takes the principal and a selected
initiative. Agents receive trusted platform execution context after
authorization, never raw tokens or provider claim dictionaries.

## Credential handling

`BearerCredential` is a transient input wrapper with a redacted representation.
The service does not store it, and neither `ValidatedIdentityClaims`,
`Principal`, nor `AuthenticatedRequestContext` contains it. Provider adapters
must avoid credential text in exceptions and normal logs. Raw access/refresh
tokens are never written to normal logs, DynamoDB, Initiative Profiles, or
Resource Bindings, and are never sent to agents. Expected authentication
failures use typed, credential-free errors; internal adapter failures remain
distinct from unauthenticated requests.

## Future deployment

A concrete enterprise path may be Okta/OIDC → API Gateway or a control-plane
adapter → this authentication boundary → `Principal`. This contract does not
choose a JWT library, web framework, cloud authorizer, or deployment service.
Live Okta integration, frontend login/logout and redirects, membership storage,
role resolution, authorization decisions, tool enforcement, agents, Resource
Bindings, and AWS infrastructure are separate work.
