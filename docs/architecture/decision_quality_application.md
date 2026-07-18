# Decision Quality Application — CMDownloader

## Why it matters here

CMDownloader is a focused utility. Its primary risk is not insufficient architecture but unnecessary expansion into a platform.

## Level B decisions

Use a Decision Challenge for download scheduling, retry policy, archive naming, metadata persistence, browser automation, and adding a database or background worker.

## Level C decisions

Use a full ADR only for credential storage, access to protected recordings, destructive archive migration, externally consumed interfaces, or deployment as a shared multi-user service.

## Complexity deliberately not introduced

Prefer a small local tool. Do not add Redis, Celery, a message broker, plugin framework, distributed storage, or multi-tenant architecture without measured operational need.

## Revisit triggers

Reopen decisions when download volume, failure rate, concurrent users, retention requirements, or external-system changes exceed the documented operating assumptions.

## Deletion Review focus

Remove obsolete selectors, provider-specific workarounds, unused download formats, stale retry branches, duplicate filename logic, and dependencies that can be replaced with clear standard-library code.