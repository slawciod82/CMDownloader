# Engineering Decision Quality Rules

Version: 1.1  
Status: Active  
Scope: Universal, non-domain-specific  
Last updated: 2026-07-18

## Purpose

These rules govern significant engineering decisions made by humans or with AI assistance. They supplement `project_standards.md` and must be copied unchanged between repositories.

## DQ-01 — AI Output Is a Hypothesis
Treat every AI-generated recommendation as a hypothesis to be checked against the current repository, domain rules, operational evidence, security policy, and project constraints. Fluent output is not evidence.

## DQ-02 — Evidence Over Convention
Prefer the option best supported by local constraints and measurable outcomes, not popularity, novelty, or contrarian appeal.

## DQ-03 — Removal Before Addition
Before adding a service, dependency, cache, queue, framework, abstraction, or compatibility layer, test whether removal, consolidation, simplification, or correct use of an existing element solves the problem.

## DQ-04 — Significant Decisions Are Falsifiable
Record assumptions, expected failure mode, available evidence, a falsification test, and an observable revisit trigger.

## DQ-05 — Deliberate Exceptions Require a Contract
Intentional duplication, compatibility code, or local ugliness must name the protected risk, owner, verification method, and removal or review condition.

## DQ-06 — Review Depth Follows Reversibility
Level A uses normal review. Level B uses a Decision Challenge. Level C requires an ADR, pre-mortem, rollback or containment plan, evidence plan, revisit trigger, and owner.

## DQ-07 — Sprint Decision Accounting
Plans record assumptions and complexity deliberately not introduced. Summaries record decisions confirmed or invalidated and complexity removed or avoided.

## DQ-08 — Maintenance Includes Deletion Review
Maintenance work reviews unused dependencies, abstractions, adapters, caches, queues, feature flags, compatibility paths, duplicated workflows, speculative infrastructure, dead configuration, and obsolete documentation.

## DQ-09 — Decision Ownership
AI may assist analysis but cannot own residual risk, production acceptance, or reassessment.

## DQ-10 — AI Data Classification
Classify information before placing it in an AI context.

- `PUBLIC`: public documentation and public source code may be used normally.
- `INTERNAL`: non-public code and architecture may be used only with an approved account and tool.
- `CONFIDENTIAL`: organizational data and identifiable logs require deterministic redaction.
- `RESTRICTED`: personal identifiers, documents, secrets, tokens, credentials, production dumps, medical data, and signing material must not be sent to an external model without an explicitly approved deterministic protection gateway.

Do not publish share links for project conversations containing internal information.

## DQ-11 — Agent Permission Boundary
A prompt is not a security boundary. Agent access must be technically restricted by environment, credentials, filesystem scope, database permissions, network policy, branch protection, and deployment gates.

Development agents do not receive production credentials by default. Destructive, data-changing, tenant-isolating, key-management, restore, cutover, or production deployment actions require explicit human control and a verified runbook.

## DQ-12 — Evidence Verification
An AI statement that a test passed, a command ran, a source exists, a package is safe, a migration is reversible, or data is consistent is not evidence. Verify through actual CI output, command logs, official documentation, reproducible checks, database reconciliation, or another authoritative source.

For risky commands, record purpose, scope, preconditions, worst credible effect, rollback, and before-and-after verification.

## DQ-13 — AI-Suggested Dependency Verification
Before installing a package suggested by AI, verify its exact name, official source, maintainers, release history, ownership, license, known vulnerabilities, install scripts, transitive dependencies, and necessity. Pin or constrain the approved version. Never install a guessed package name directly from model output.

## DQ-14 — AI Near-Miss Learning
Record a near miss when an AI recommendation, generated change, dependency, command, or interpretation could have caused meaningful harm but was stopped by review, tests, policy, permissions, or luck.

A near-miss record contains:

```markdown
## AI Near Miss
### Proposed action or output
### Potential impact
### Detection point
### Existing control that stopped it
### Remaining gap
### Follow-up action
```

Do not create a new rule for every near miss. Strengthen a control only when the event exposes a new risk class or an untested assumption.

## Decision levels

### Level A — Local and easily reversible
Normal review and tests appropriate to risk.

### Level B — Structural but reversible
At least two considered options and a concise Decision Challenge in the sprint, issue, or ADR.

### Level C — Difficult to reverse or high impact
Full ADR, pre-mortem, rollback or containment plan, evidence plan, revisit trigger, and named owner.

## Decision Challenge

```markdown
## Decision Challenge
### Decision
### Default answer
### Local constraints
### Considered alternatives
### Removal or consolidation alternative
### Expected failure mode
### Evidence and assumptions
### Falsification test
### Revisit trigger
### Rollback or containment
### Decision owner
```

## External AI Incident Lens

During an operational-readiness review, maintenance sprint, or before a high-risk deployment, review a small relevant sample of external AI incidents involving coding agents, confidential data, dependencies, production actions, or systems similar to the product.

```markdown
## External AI Incident Review
### Incident and source
### Relevance to this project
### Existing control
### Has the control been tested?
### Remaining gap
### Action: none / test / issue / runbook / architecture change
```

Review at most the incidents relevant to current exposure. The purpose is control validation, not collecting cautionary stories.

## Sprint additions

Plans containing Level B or C decisions may include:

```markdown
## Architectural assumptions to validate
## Complexity deliberately not introduced
## Decision Challenges and ADRs
```

Relevant summaries may include:

```markdown
## Decisions confirmed or invalidated
## Complexity removed or avoided
## Revisit triggers created or changed
## AI near misses and control changes
```

Do not retrofit closed sprint documents solely to add empty sections. Security, audit, retention, migration, and compatibility controls require risk verification before removal.