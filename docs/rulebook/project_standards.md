# Shared Project Standards

Version: 1.1  
Status: Active  
Scope: All internal software products and business applications  
Nature: Universal, non-domain-specific baseline

---

# Purpose

This document defines the default product, delivery, architecture, security, dependency, documentation, and user-interface standards used across our projects.

It is intended to be copied unchanged between repositories. Project documentation may extend it, but must not silently contradict it. Any exception must be explicit, justified, and recorded in the repository.

Detailed rulebooks such as `universal_project_rules.md`, `architecture_rules.md`, `ui_rules.md`, or `dependency_rules.md` may expand this baseline.

---

# 1. Product and Delivery

## PS-01 — Business Flow First

Build the complete business flow first, improve operator convenience second, and expand functionality third.

## PS-02 — Current Goal Filter

Accept work into the active sprint only when it directly advances the current milestone or removes a blocker.

## PS-03 — Backlog Instead of Scope Creep

Record useful out-of-scope ideas in the backlog instead of silently expanding active work.

## PS-04 — End-to-End Slices

Prefer small, complete, testable vertical slices over large unfinished horizontal layers.

## PS-05 — Incremental Delivery

Each implementation step should produce a working state that can be independently verified.

## PS-06 — Definition of Done

Work is complete only when the intended behavior works, relevant tests pass, the primary flow has been manually checked, and documentation reflects the implemented state.

## PS-07 — Boy Scout Rule

Leave touched code slightly cleaner when a small, safe improvement can be made without losing task focus.

## PS-08 — Continuous Debt Control

Resolve small technical debt during feature work and plan a maintenance or stabilization sprint approximately every two to three feature sprints.

## PS-09 — No Premature Optimization

Do not optimize code, infrastructure, dependencies, or abstractions without a measured or clearly demonstrated problem.

## PS-10 — Backward Compatibility by Default

Prefer evolutionary changes and controlled migrations unless a deliberate breaking change has been planned.

---

# 2. Repository and Documentation

## PS-11 — GitHub Is the Source of Truth

The repository, its current default branch, issues, commits, tags, pull requests, and documentation are the authoritative record of project state.

## PS-12 — Standard Documentation Structure

Projects should use the following structure as needed:

```text
docs/
├── rulebook/
├── sprints/
├── adr/
├── architecture/
└── operations/
```

Do not create empty directories merely to satisfy the structure.

## PS-13 — Rulebook Location

Universal standards belong in `docs/rulebook/`. Domain rules and project decisions belong in separate, clearly named documents.

## PS-14 — Sprint Documentation

Each sprint should have a plan and a summary under `docs/sprints/`, using consistent names such as `sprint_01_plan.md` and `sprint_01_summary.md`.

## PS-15 — Delivery Lifecycle

Use the default lifecycle:

```text
Plan → GitHub Issues → Local Implementation → Test → Commit/Tag → Summary → Next Plan
```

External archive snapshots are optional and should be created only for an explicit operational reason. Git history remains the primary version record.

## PS-16 — Decisions Are Recorded When Made

Record significant architecture, security, data-model, integration, and compatibility decisions when they are adopted.

## PS-17 — Documentation Reflects Reality

Documentation must clearly distinguish implemented behavior, planned work, known limitations, and technical debt.

## PS-18 — Canonical Scaffold Source

New projects should start from the current approved scaffold or structure guidance rather than from an arbitrary older repository copy.

---

# 3. Architecture

## PS-19 — Clear Responsibility Ownership

Every business concept, workflow, and application layer should have one clearly defined responsibility and owner.

## PS-20 — Separation of Concerns

Separate request handling, business operations, data retrieval, presentation preparation, persistence, and rendering.

## PS-21 — Thin Delivery Layer

HTTP routes, blueprints, CLI handlers, and API endpoints coordinate input and output but do not own business logic.

## PS-22 — Query Layer

Complex reads, filtering, aggregation, queues, reports, and dashboard data retrieval belong to dedicated query functions or query services.

## PS-23 — Service Layer

Business operations, validations, state changes, and transaction boundaries belong to services.

## PS-24 — Workflow Layer When Justified

Introduce a workflow layer only for genuinely multi-step processes coordinating several operations or entities.

## PS-25 — Presentation Services

Prepare labels, badges, navigation, timelines, status metadata, and UI-ready view models outside templates.

## PS-26 — Presentation-Only Templates

Templates render prepared data. They do not query the database, calculate business state, or decide workflow rules.

## PS-27 — Models Stay Focused

Models define persistence, relationships, and simple invariants without becoming containers for unrelated orchestration.

## PS-28 — Dashboard First

A major operator-facing entity should normally expose a dashboard as its central work surface, with state, context, actions, relationships, and timeline.

## PS-29 — Framework Threshold Rule

Do not introduce shared frameworks, base classes, infrastructure layers, or cross-project packages until at least three independent use cases demonstrate stable common behavior.

## PS-30 — Local Solution First

Keep the first and usually the second implementation local. Extract proven behavior, not hypothetical reuse or visual similarity.

## PS-31 — Prefer Composition

Prefer small services and components with clear contracts over deep inheritance trees.

## PS-32 — Explicit Business Actions

Important state changes occur through named operations with validated preconditions and explicit side effects, not arbitrary field mutation.

## PS-33 — No Magic Domain Values

Stable statuses, event codes, source types, and controlled values use central constants, enums, or dictionaries. User-facing labels remain separate.

## PS-34 — Controlled Schema Evolution

Every persistent schema change requires an explicit migration or equivalent controlled mechanism. Preserve data through staged migrations where necessary.

## PS-35 — Time Standard

Store persistent timestamps in UTC. Evaluate business-day boundaries and schedules in the configured business timezone. Centralize parsing, conversion, and display formatting.

## PS-36 — Integration Boundaries

External systems are accessed through adapters or providers. External payloads are untrusted, explicitly mapped, and handled idempotently when retries or duplicates are possible.

---

# 4. Security and Audit

## PS-37 — Secrets Outside Source Code

Credentials, API keys, tokens, certificates, and deployment secrets must remain in environment variables or approved secret-management mechanisms.

## PS-38 — Backend Authorization

UI visibility never replaces backend authorization. Protected operations enforce access rules at request and/or service boundaries.

## PS-39 — State-Changing Request Protection

Browser-originated mutations use CSRF protection or an explicitly approved equivalent.

## PS-40 — Validation Is Not Authorization

Input validation, workflow preconditions, authentication, and authorization are separate responsibilities.

## PS-41 — Secure Defaults

Prefer least privilege, default deny, explicit permissions, safe error handling, and minimal exposed surface.

## PS-42 — Audit First

Meaningful operator actions and important workflow transitions generate stable audit events with actor, entity, action, time, and relevant context.

## PS-43 — Audit and Timeline Are Distinct

Technical audit preserves evidence. A business timeline is a presentation of selected events and never replaces the underlying audit trail.

## PS-44 — Do Not Reimplement Security Protocols

Do not write custom cryptography, password hashing, CSRF, OAuth/OIDC, signature validation, or security-sensitive parsers when mature, maintained implementations exist.

---

# 5. Dependency and Supply-Chain Standards

## PS-45 — Capability-Driven Dependencies

Add a dependency only to provide a named application capability with a clear owner and use case.

## PS-46 — Standard Library First

Before adding a package, check whether the language standard library already provides a sufficient, readable, and maintained solution.

## PS-47 — Avoid Microdependencies

Do not add a third-party package for trivial, stable logic that can be implemented clearly and safely in a small, well-tested local function.

## PS-48 — Safety Overrides Convenience

Use mature libraries for complex standards, protocols, cryptography, authentication, parsing, database migrations, and other security- or correctness-sensitive areas.

## PS-49 — Assess the Full Dependency Tree

Evaluate direct and transitive dependencies, maintainers, release practices, install scripts, permissions, licenses, known vulnerabilities, and abandonment risk.

## PS-50 — Dependency Scopes Are Explicit

Separate runtime, development, test, build, migration, and production-only dependencies. Development tools must not enter the final production image without a runtime reason.

## PS-51 — Reproducible Builds

Use controlled version ranges, lockfiles or constraints, repeatable container builds, and documented upgrade procedures.

## PS-52 — Minimal Production Image

Production images contain only application code, runtime assets, and dependencies required to serve the product.

## PS-53 — Dependency Exit Rule

Critical external packages should be isolated behind small application boundaries so they can be replaced without rewriting domain logic.

## PS-54 — Regular Dependency Review

During maintenance work, remove unused packages, inspect transitive changes, review vulnerability and license reports, and verify that manifests match actual imports and runtime needs.

## PS-55 — Frontend Assets Are Controlled

Pin frontend library versions. Prefer locally controlled assets when offline operation, availability, privacy, or reproducibility requires it. Optimize bundles only when measurement justifies the added build complexity.

## PS-56 — Internal Shared Packages Need the Same Discipline

A private common package is still a dependency. Create one only after repeated use proves a stable contract, and version it deliberately.

---

# 6. User Interface and Experience

## PS-57 — Operator First

Design screens around the operator's task and decisions rather than database structure.

## PS-58 — KISS UX

Keep common information and frequent actions visible. Move secondary complexity and uncommon filters behind deliberate controls.

## PS-59 — Three-Level Action Architecture

Keep Navigation Actions, Object Actions, and Business Actions visually and conceptually separate.

## PS-60 — Back and Up Have Different Meaning

Back returns to the previous context when available. Up always navigates to the logical parent collection or higher-level view.

## PS-61 — Primary Action Is Obvious

The most important next action should be identifiable without searching through unrelated controls.

## PS-62 — Dashboard Information Hierarchy

Present current state, exceptions, readiness, and next actions before secondary details and history.

## PS-63 — KPI Matches Context

KPI cards describe the same entity or aggregation level as the current dashboard and may link to relevant filtered views.

## PS-64 — Coherent Partials

A template partial represents a coherent business UI fragment that can be understood in isolation. Do not split templates by length alone.

## PS-65 — Consistent Components and Icons

Use the approved component system and a consistent Bootstrap Icons vocabulary for comparable actions. Icon-only controls require tooltips and accessible names.

## PS-66 — Accessibility by Default

Maintain keyboard access, visible focus, semantic controls, accessible names, and redundant status signals beyond color alone.

## PS-67 — Business-Oriented Feedback

Messages describe the business outcome and actionable validation problem rather than only saying that data was saved or exposing technical exceptions.

## PS-68 — Empty and Future States Are Honest

Empty sections explain themselves. Planned functionality is shown as information, not as fake disabled controls.

## PS-69 — Server Remains Source of Truth

HTMX or other partial-update mechanisms may improve interaction, but business validation and workflow transitions remain server-side.

---

# 7. Testing and Quality

## PS-70 — Inspect Before Coding

Identify and inspect the exact relevant files, existing patterns, tests, and documentation before proposing or implementing changes.

## PS-71 — Tests Follow Risk

Test business transitions, permissions, validation boundaries, migrations, integration contracts, and critical read models according to their risk.

## PS-72 — Manual Verification Before Commit

Manually verify the primary user flow for each increment before committing it.

## PS-73 — Failures Are Actionable

Errors and logs should provide enough technical context for diagnosis without leaking sensitive implementation details to users.

## PS-74 — Refactoring Preserves Behavior

Refactoring should be small, test-backed, and behavior-preserving unless the behavior change is explicit.

---

# 8. AI-Assisted Development

## PS-75 — Exact File Scope First

Before each coding step, identify the files that must be inspected and change only the files required for the task.

## PS-76 — Small Testable Steps

Implementation guidance should use small steps with clear verification checkpoints.

## PS-77 — Brief Architectural Rationale

Explain decisions affecting ownership, architecture, security, compatibility, dependencies, or future extensibility.

## PS-78 — Task-Oriented Communication

Keep development communication concise, technical, and focused on the current objective.

## PS-79 — No Unrequested Repository Changes

Repository changes require explicit user authorization. Analysis alone does not authorize writes.

---

# Rule Hierarchy

When rules conflict, apply them in this order:

1. explicit, documented project exception,
2. documented domain rules,
3. architecture and security decisions,
4. UI rules,
5. dependency rules,
6. this shared baseline,
7. sprint plan,
8. local implementation notes.

Existing technical debt is not an implicit exception.

---

# Golden Rules

**Build the business flow first, improve the workflow second, and expand the system third.**

**Keep responsibilities explicit, workflows owned, and operator behavior predictable.**

**Use external dependencies deliberately and treat every dependency as part of the trusted supply chain.**

**Do not create shared infrastructure before repeated use proves that it is needed.**
