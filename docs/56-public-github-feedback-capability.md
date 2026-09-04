# 56. Public GitHub Feedback Capability

Status: **Stage 1 implemented / real read-only capability verified / production activation pending**
Updated: **2026-09-05**

## 1. Purpose

Amadeus may leave her own observations, bug reports, and design suggestions in the public downstream repository `zhaocy02/tele-Amadeus` and later return to those discussions after the public code changes.

This is intentionally **not** general GitHub access. It is a narrow, auditable external-action capability whose initial write target is the public mirror Issues surface only.

The private repository remains the canonical source of truth. Public Issues are feedback and discussion, not an alternate development authority.

## 2. Architectural boundary

```text
Character observation / explicit user request
                        |
                        v
                Feedback Proposal
                 (no side effect)
                        |
          +-------------+-------------+
          |                           |
          v                           v
 Public Repository Grounding      Issue Search
 HEAD / selected files            duplicate/history
          |                           |
          +-------------+-------------+
                        v
              Public Outbound Guard
                        |
                        v
                  Action Policy
             REQUIRE_CONFIRM
                        |
                        v
              GitHub Feedback Client
                        |
                        v
              zhaocy02/tele-Amadeus
                 Issues surface
```

The GitHub feedback capability is **separate from the direct-turn Web Search tool loop**. Web Search is a bounded, read-only evidence tool used inside an ordinary Character turn; GitHub Issue creation/commenting is a persistent external side effect and therefore has its own authorization and execution boundary.

## 3. Initial target and authority

Stage 1 is pinned to:

```text
repository = zhaocy02/tele-Amadeus
branch     = main
```

The Character never receives a freely selectable repository target. Repository ownership is program/configuration-owned, and the private canonical repository is never a Character write target.

## 4. GitHub App permission model

Production identity is a dedicated GitHub App installed only on the public mirror.

Minimum intended repository permissions:

```text
Metadata: Read
Contents: Read
Issues: Read and write
```

Do not grant source-code write, Pull Requests write, Actions write, Administration, Secrets, Deployments, or unrelated privileges.

Credentials remain backend-only. App private key material, installation tokens, and bearer tokens must never enter Character prompts, Character Context, Structured Memory, telemetry payload bodies, GitHub Issue bodies, or normal logs.

## 5. Stage 1 contract

Read operations:

```text
get_public_repo_state()
search_issues(query)
read_public_file(path)  # bounded grounding at exact public HEAD
```

Side-effect-free proposal operations:

```text
prepare/propose issue
prepare/propose comment
```

Persistent write operations require an already prepared proposal plus explicit confirmation:

```text
create_issue(prepared, confirmed=True)
comment_issue(prepared, confirmed=True)
```

A model deciding that an Issue should exist is not equivalent to an external Issue being created.

## 6. Proposal provenance and public-file grounding

A code-specific proposal carries public provenance including:

```text
public_head
affected_files
proposal id / fingerprint
```

Before a code-specific proposal can be prepared, the client reads each declared public file at the proposal's exact public HEAD. Paths are repository-relative, traversal is rejected, at most five files are accepted, and each text file is bounded to 128 KiB.

If an observation currently exists only in private code and the public mirror has not exposed the relevant file, preparation fails closed rather than publishing private-only implementation provenance.

Immediately before execution the runtime re-reads the public HEAD. If it differs from the proposal's `public_head`, the proposal is stale and must be regenerated.

## 7. Duplicate prevention

Before a new Issue is executed, search repository-scoped Issues using a bounded query derived from the proposal topic. Stage 1 exposes duplicate candidates in the preview and preserves a stable proposal fingerprint where available.

If the required duplicate check cannot run, preparation fails closed. A later interaction ledger may persist `fingerprint -> issue_number -> public_head -> status -> last_checked_at`; SQLite is sufficient and no vector database is justified for this capability.

## 8. Public Outbound Guard

Before any write, inspect title/body/comment for high-confidence private or credential-bearing material, including token/private-key patterns, secret assignments, private/local absolute paths, private-network addresses, runtime database/log/backup material, and operator-supplied forbidden fragments.

The guard is defense in depth, not a claim of perfect semantic DLP. Guard failure blocks publication.

## 9. Stage 1 confirmation flow

Stage 1 is explicit-confirmation only:

```text
observation / user request
-> public-grounded proposal
-> duplicate candidates
-> outbound guard
-> bounded preview
-> explicit confirm or cancel
-> exact prepared proposal executes
```

Telegram/operator controls:

```text
/github propose
/github pending
/github confirm <proposal_id>
/github cancel [proposal_id]
```

Pending proposals expire after 15 minutes. Confirmation must match the exact proposal id and does not regenerate content. A changed public HEAD invalidates the pending proposal. `/github` stays outside Character generation and behind the dedicated persistent-side-effect boundary.

## 10. Process gate and production posture

The capability is default-off:

```text
AMADEUS_ENABLE_GITHUB_FEEDBACK=false
```

Enabling composition requires complete backend GitHub App configuration. Even then, the repository and branch remain hard-pinned by the application rather than selected by model output.

Merging the implementation does not by itself activate production GitHub feedback. Production configuration/activation is a separate deliberate operation.

## 11. Later autonomy stages

### Stage 2 — autonomous proposal only

Retrospective or Autonomy may later generate candidates and perform public grounding, but execution still requires confirmation.

### Stage 3 — bounded low-risk autonomous writes

Only after sufficient operating evidence. Expected controls include explicit opt-in, a low daily creation cap, per-fingerprint cooldown, mandatory grounding/duplicate checks, allow-listed labels, comments limited to Amadeus-owned or explicitly allow-listed Issues, content-light audit telemetry, and an immediate disable control.

Stage 3 is not part of the current implementation.

## 12. Labels and identity

Any labels are restricted to an operator-owned allow-list, for example:

```text
from-amadeus
observation
idea
possible-bug
character
memory
```

The model cannot create arbitrary repository labels. GitHub authorship uses the dedicated GitHub App identity rather than impersonating a human developer.

## 13. Failure semantics

The capability fails closed for writes:

```text
missing credentials       -> no write
wrong repository/scope    -> reject
missing public file       -> reject code-specific proposal
stale public HEAD          -> reject / re-ground
outbound guard hit         -> reject
confirmation absent        -> reject
GitHub auth/API failure    -> no fabricated success
duplicate check unavailable -> reject
```

## 14. Implementation slices

### Slice A — bounded feedback core — implemented

- repository-pinned client;
- public HEAD lookup and Issue search;
- exact-HEAD public-file grounding;
- proposal dataclasses;
- deterministic outbound guard;
- explicit confirmation gate;
- stale-HEAD rejection;
- mock-HTTP regression tests.

### Slice B — GitHub App authentication — implemented and real-read verified

- backend-only App private-key configuration;
- RS256 short-lived App JWT;
- repository/permission-narrowed installation token;
- cached installation token before expiry;
- fail-closed scope validation;
- non-writing real capability smoke.

### Slice C — operator/Telegram confirmation — implemented

- bounded proposal preview and duplicate candidates;
- `/github propose|pending|confirm|cancel`;
- 15-minute proposal TTL;
- exact prepared-proposal execution;
- stale public-HEAD invalidation;
- default-off application composition.

### Slice D — ledger / autonomous proposal source — deferred

No interaction ledger, autonomous proposal generation, follow-up polling, or autonomous publication is enabled in Stage 1.

## 15. Validation evidence

Deterministic tests cover repository pinning, confirmation requirements, stale proposals, outbound guarding, duplicate-search scoping, public-file grounding, credential isolation, configuration gating, Telegram confirmation flow, and disabled-path non-interference.

Executable Stage 1 head validated on Server Dev:

```text
4bb6ce4f24659e21919f7858fedb78c6a0bbc759
```

Task-local dependency/install validation passed, including import-path verification, shell helpers, Ruff, strict mypy, pytest, and package build.

The server's standalone Python initially lacked a usable default CA file while curl could validate the same HTTPS endpoint. Validation was completed by explicitly pointing Python/pip to the same already-trusted CA bundle used successfully by curl. TLS verification remained enabled; no `trusted-host`, certificate bypass, or insecure package-index exception was introduced.

Real dedicated-GitHub-App **non-writing** capability smoke passed against:

```text
repository = zhaocy02/tele-Amadeus
branch     = main
public HEAD observed during smoke
  2c203e8f2384cbc5a35e803364c737246d4d0939
probe Issue matches = 0
GitHub App auth      = PASS
read-only feedback   = PASS
```

The smoke only minted the repository-scoped installation token, read public HEAD, and searched Issues. It did **not** create an Issue/comment and did not start Telegram polling.

After this evidence was collected, synchronization with repository ops closeout #115 changes governance/documentation and CI policy only relative to the verified GitHub-feedback executable tree. Per the Actions-budget rule, that non-executable synchronization does not require repeating the Server Dev capability smoke; the final coherent PR head still receives the normal PR CI gate once.

## 16. GitHub Actions budget interaction

GitHub Actions is a constrained project resource. This capability follows the repository-wide rule in `docs/57-doubao-provider-and-actions-budget-closeout.md`:

```text
pull_request       -> full Python 3.11 + 3.12 CI
workflow_dispatch  -> explicit/manual CI
push to main       -> no duplicate routine matrix
```

Iterative fixes should run locally/in the task worktree and be batched before updating an open PR. The CI workflow still syntax-checks and exercises help/import surfaces for the GitHub feedback smoke helper, but real GitHub credentials are never placed in Actions.

## 17. Explicit non-goals

- no writes to the private canonical repository;
- no source-code commits or PR creation through this capability;
- no branch or Actions control;
- no Secrets or repository-admin access;
- no arbitrary repository selection;
- no general browser agent;
- no webhook server initially;
- no autonomous Issue publication initially;
- no Persona Core change merely to expose the capability.

## 18. Relationship to the public mirror

`zhaocy02/tele-Amadeus` remains a sanitized downstream mirror with independent history. Public feedback cannot bypass private development authority.

The supported loop remains:

```text
Amadeus/public contributor Issue
-> human/developer review
-> private focused task branch
-> private CI/evaluation/Server Dev gates as required
-> merge private main
-> sanitized public export
-> public CI
-> Amadeus may re-ground and follow up on the public Issue
```

This preserves one canonical source of truth while allowing Amadeus to participate in the public development conversation.
