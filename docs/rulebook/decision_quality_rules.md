# Engineering Decision Quality Rules

Version: 1.0  
Status: Active  
Scope: Universal, non-domain-specific  
Last updated: 2026-07-18

## Purpose

These rules govern significant engineering decisions made by humans or with AI assistance. They supplement `project_standards.md` and must be copied unchanged between repositories.

## DQ-01 — AI Output Is a Hypothesis
Treat every AI-generated recommendation as a hypothesis to be checked against the current repository, domain rules, operational evidence, security policy, and project constraints. Fluent output is not evidence.

## DQ-02 — Evidence Over Convention
Do not accept or reject a solution merely because it is popular, conventional, unfashionable, or unusual. Prefer the option best supported by the project's actual constraints and measurable outcomes.

## DQ-03 — Removal Before Addition
Before adding a service, framework, dependency, cache, queue, abstraction, or compatibility layer, check whether the problem can be solved by removing, consolidating, simplifying, or correctly using an existing element.

## DQ-04 — Significant Decisions Are Falsifiable
A significant decision identifies its assumptions, expected failure mode, available evidence, falsification test, and an observable trigger for reopening the decision.

## DQ-05 — Deliberate Exceptions Require a Contract
Deliberately non-ideal, duplicated, compatibility-oriented, or locally ugly code is allowed only when it controls a named risk. Record the reason, protected behavior, owner, removal or review condition, and related ADR or issue.

## DQ-06 — Review Depth Follows Reversibility
Use lightweight review for easy-to-reverse local choices, a Decision Challenge for structural choices, and a full ADR with rollback and pre-mortem for security-critical, data-critical, externally contracted, or difficult-to-reverse decisions.

## DQ-07 — Sprint Decision Accounting
New sprint plans identify architectural assumptions to validate and complexity deliberately not introduced. Sprint summaries record decisions confirmed or invalidated and complexity removed or avoided.

## DQ-08 — Maintenance Includes Deletion Review
Every maintenance or stabilization sprint reviews unused dependencies, abstractions, adapters, caches, feature flags, compatibility paths, duplicate workflows, and speculative infrastructure for removal or consolidation.

## DQ-09 — Decision Ownership
A significant decision has a named owner responsible for collecting evidence, accepting residual risk, and reopening the decision when its trigger occurs. AI may assist analysis but cannot own the decision.

## Decision levels

### Level A — Local and easily reversible
Required: rulebook compliance, tests appropriate to risk, and normal review.

### Level B — Structural but reversible
Required: at least two considered options and a concise Decision Challenge recorded in the sprint, issue, or ADR.

### Level C — Difficult to reverse or high impact
Required: full ADR, pre-mortem, rollback or containment plan, evidence plan, revisit trigger, and named owner.

## Decision Challenge template

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

## Sprint plan additions

```markdown
## Architectural assumptions to validate
## Complexity deliberately not introduced
## Decision Challenges and ADRs
```

Do not rewrite closed sprint plans solely to add empty sections.

## Sprint summary additions

```markdown
## Decisions confirmed or invalidated
## Complexity removed or avoided
## Revisit triggers created or changed
```

## Maintenance Deletion Review

Review unused dependencies, abstractions with fewer than three stable uses, inactive adapters, unmeasured caches, unjustified queues/workers, stale feature flags, fulfilled compatibility paths, duplicated workflows, speculative infrastructure, dead configuration, and obsolete documentation.

Security, audit, retention, migration, and compatibility controls must not be removed merely to reduce code volume. Verify their protected risk and removal preconditions first.