# Verified implementation progress

Progress changes only when code, migrations and tests are reproducible.

## 2026-08-19 — Local vertical-domain milestone

- [x] Repository source directory created.
- [x] Django 5.2 LTS dependency set pinned.
- [x] Cloud-neutral environment configuration.
- [x] Dockerfile and PostgreSQL Compose definition.
- [x] UUIDv7 application IDs.
- [x] App-owned Principal and explicit PermissionGrant foundation.
- [x] Product and sealed immutable ProductProfileVersion.
- [x] Login, health check and first internal dashboard surface.
- [x] Initial migrations apply in a clean local database.
- [x] Foundation test suite passes.
- [x] Versioned Task contract, immutable DoR/DoD runs and optimistic Task events.
- [x] Immutable ContentAssetVersion, Submission and human ReviewDecision chain.
- [x] Publication release intent, complete deterministic policy evaluation and fail-closed Release Gate.
- [x] Last-mile stale-context detection and manual publication proof event chain.
- [x] Split cross-app migration graph applies cleanly from an empty local database.
- [x] Domain and negative-path tests pass locally.
- [x] AC-01 through AC-05 consolidated local acceptance tests pass (79/79 full suite).
- [x] Central authorization resolves acting role and hierarchical scopes with DENY precedence.
- [x] Docker startup order is bounded database wait → migrations → static collection → Gunicorn.
- [ ] PostgreSQL integration run (Docker is not installed on the current machine).
- [x] Local UI routes cover task creation, DoR/DoD, assignment, submission, human review, fail-closed Gate, manual proof and completion.
- [ ] One retained human Dogfood run through the full UI on PostgreSQL/Staging.
- [ ] AC-01 through AC-05 rerun and evidence pack from the exact Staging candidate image.
- [ ] Staging and recovery gates.

Current verified status: the local domain loop, all five frozen acceptance paths and
the complete minimum UI route set are implemented and protected by 79 tests. A real
human Dogfood run, PostgreSQL/Staging evidence and recovery rehearsal remain before
Dogfood V1 can be called complete.
