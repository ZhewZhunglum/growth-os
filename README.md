# Growth OS

Growth OS is the internal workflow system for the frozen PUKO Dogfood V1.

The current milestone provides a cloud-neutral Django foundation with:

- app-owned login and role-ready principals;
- explicit scoped permission grants;
- a sealed, versioned product profile;
- versioned task contracts with immutable DoR/DoD checks and state events;
- immutable content assets, submissions, and one final human review per submission;
- a fail-closed release gate that binds the exact approved content, policy set,
  publisher permission, channel account, and runtime environment;
- manual-publication events and proof records (the system never publishes to an
  external platform automatically);
- a recognizable internal dashboard;
- PostgreSQL and Docker configuration for the target runtime;
- a lightweight SQLite mode used only when Docker/PostgreSQL is unavailable locally;
- fail-fast Production configuration and health checks.

The repository carries the frozen specification snapshot in
`docs/spec/v1-freeze-2026-08-18/`. New ideas do not change that runtime boundary;
they move to the post-closure backlog unless they fix a demonstrated P0 issue.

## Local lightweight start

This mode is for UI and unit-test development only. PostgreSQL remains the target database.

```powershell
$env:DATABASE_ENGINE='sqlite'
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Do not run `createsuperuser` before the Dogfood bootstrap unless you intend to
reuse that exact username with `--owner-username`. The bootstrap is the canonical
way to create the first Owner and frozen test context. Open
`http://127.0.0.1:8000/` only after it has created the requested test identities.

After bootstrap and sign-in, an authorized Owner or Operations Admin can create
a DRAFT task from the dashboard. The form only offers the ACTIVE product's
current sealed profile and its latest exact task contract. Employees then use
Today for DoR/DoD and submission, reviewers use Review, and authorized publishers
use Manual publish. The last step records proof of a publication performed by a
human; it never sends content to an external platform.

### Local Dogfood bootstrap

After migrations, `python manage.py bootstrap_dogfood` idempotently creates the
sealed PUKO profile, mandatory policy version, exact task contract and policy
link, Owner product grant, local channel/environment binding, and OPEN manual
publish capability. It deliberately creates no Task. If the Owner does not
already exist, supply its password through `BOOTSTRAP_OWNER_PASSWORD` in the
command process environment; passwords are never accepted as command-line
arguments or embedded in the seed.

The normal `--full-demo` setup creates three human identities—`owner`, `admin`
and `operator`—plus a non-login rule-evaluator service identity. Reviewer and
Publisher are capabilities, not additional staff roles: Admin receives an
explicit product-scoped REVIEW grant, while Operator receives an explicit
account-scoped HIGH-risk PUBLISH grant. A fresh database therefore needs three
distinct temporary values in `BOOTSTRAP_OWNER_PASSWORD`,
`BOOTSTRAP_ADMIN_PASSWORD`, and `BOOTSTRAP_OPERATOR_PASSWORD`. Existing matching
accounts are reused without resetting their passwords. Missing or reused
password values fail the transaction without creating partial staff.

For the normal local role set, provide the three distinct passwords through the
temporary process environment and run:

```powershell
.\.venv\Scripts\python.exe manage.py bootstrap_dogfood --full-demo
```

Clear the three `BOOTSTRAP_*_PASSWORD` environment variables immediately after
the command. Never put their values in source, `.env.example`, chat, screenshots,
logs, or shell scripts.

The legacy `--reviewer-username` and `--publisher-username` options remain for
replaying existing local databases. Use `--strict-separation-demo` only when a
test explicitly needs separate reviewer and publisher identities. It does not
add new role types and it never rewrites existing publication history.

Local development accepts passwords of at least 6 characters **only when a
password is newly set or changed in the local environment**. This does not make
an existing 6-character local password invalid at sign-in, so a local database
must never be promoted, copied, or restored into Staging or Production.
`bootstrap_dogfood` is a local-only fixture command and fails closed outside the
local environment. Staging and Production enforce at least 12 characters for
new or changed passwords and refuse to start if `PASSWORD_MIN_LENGTH` is below
12; their databases and human identities must be provisioned separately through
the approved deployment process.

### Staging staff provisioning

`python manage.py provision_staging_staff` is the controlled Staging-only path
for the three human acceptance-test accounts. It fails closed in Local and
Production, never creates a Django staff/superuser, never seeds Product data,
and refuses a partial Owner/Admin/Operator set. The existing ACTIVE Product must
already point to a sealed profile with a sealed task contract.

The command defaults to a rollback-only dry run. On a fresh Staging database,
mount three distinct temporary password files through the deployment
platform's approved Secret mechanism and point
`STAGING_OWNER_PASSWORD_FILE`, `STAGING_ADMIN_PASSWORD_FILE`, and
`STAGING_OPERATOR_PASSWORD_FILE` at those read-only files. Direct password
environment values are rejected. Follow the ownership, mode, dry-run, apply,
and deletion sequence in `docs/STAGING-RUNBOOK.md`, then run the command there.
Its two logical invocations are:

```text
python manage.py provision_staging_staff --product-code PUKO
python manage.py provision_staging_staff --product-code PUKO --apply
```

The first command validates the complete plan and commits zero writes. The
second atomically creates the three Principals and their 30-day exact grants.
Owner and Admin receive product-scoped task-management and REVIEW grants;
Operator receives product-scoped EDIT. To give that Operator the separately
controlled manual-publish capability, pass an existing account only after its
ACTIVE Staging binding and current OPEN capability have been established:

```text
python manage.py provision_staging_staff --product-code PUKO --publish-account-code puko-us
python manage.py provision_staging_staff --product-code PUKO --publish-account-code puko-us --apply
```

This adds only an account-scoped HIGH-risk PUBLISH grant. Replaying the command
with all three matching accounts verifies/reuses them without reading or
resetting passwords. For an existing three-account set, every base Product
grant must already be complete and exact; the command refuses to silently add
missing ordinary authority. Only an explicit `--publish-account-code` may add
the separate PUBLISH grant. Password values must never be placed in `.env`, Compose,
Git, chat, screenshots, logs, shell history, or command arguments. Remove the
three temporary Secret injections immediately after the applied command.

## Docker/PostgreSQL start

1. Copy `.env.example` to `.env` and replace every placeholder locally.
2. Set `GIT_COMMIT_SHA` in `.env` to the exact 40-character output of
   `git rev-parse HEAD`. The build deliberately rejects a branch name, `latest`,
   an abbreviated SHA, and the example placeholder.
3. Install Docker Desktop.
4. Run `docker compose up -d --build`, then inspect `docker compose ps` and
   `docker compose logs --tail 200 web db`.

The canonical Compose file is `compose.yaml` (the current Docker Compose default
name). The web port is bound to `127.0.0.1` instead of every network interface.
Open `http://127.0.0.1:8000/`, or change `WEB_PORT` locally if that port is in use.

The container waits for PostgreSQL with a bounded retry, then runs migrations,
collects static files and finally starts Gunicorn. WhiteNoise serves versioned
static assets from the container. Growth OS V1 does not accept or store uploaded
files, so only PostgreSQL data requires a persistent volume. This Compose
topology is a single-web-instance bootstrap environment.
Before adding multiple web replicas, migrations must move into a one-off release
job so several instances cannot race the same schema change.

Content delivery is link-first in every environment. An immutable
`ContentAssetVersion` stores an external URL rather than file bytes. A changed
link creates a new version and must be submitted and reviewed again; existing
review and release-gate records remain bound to the exact version they approved.
Publication proof records an external publication URL or platform content ID.
Growth OS is not a file asset store, and V1 requires no separate content-storage
backend or related credentials.

## Staging / production boundary

The repository is deployable source, not evidence of a completed deployment.
Before a staging or production start:

- set `GROWTH_OS_ENV=staging` for Staging and `GROWTH_OS_ENV=production`
  for Production; both non-Local profiles force HTTPS and Secure cookies,
  while long-lived HSTS remains Production-only;
- provide a new high-entropy Django secret through the read-only
  `DJANGO_SECRET_KEY_FILE` mount;
- set exact `DJANGO_ALLOWED_HOSTS` and HTTPS origins in
  `DJANGO_CSRF_TRUSTED_ORIGINS`;
- set `TRUST_PROXY_SSL_HEADER=1` only when a trusted reverse proxy terminates
  HTTPS and **overwrites** `X-Forwarded-Proto`;
- keep the application port private and let the reverse proxy expose ports
  80/443, HTTPS certificates and public HTTP-to-HTTPS redirects;
- use `POSTGRES_SSLMODE=require` for a managed TLS database. The bundled
  Compose database explicitly uses `disable` because it is private to the
  Compose network and has no TLS listener; provide its password through the
  read-only `POSTGRES_PASSWORD_FILE` mount;
- configure backups and prove restore time before launch. The named Docker
  volume is persistence, not a backup; database backups must be encrypted and
  copied to an access-controlled destination off the application host.

The `/health/` endpoint is exempt from Django's HTTPS redirect so Docker can
check the private HTTP listener. It returns database reachability plus a small,
non-sensitive deployment identity:

```json
{
  "status": "ok",
  "database": "ok",
  "deployment": {
    "stage": "staging-candidate",
    "revision": "c91160c3b8baec39d955c38e41ee1995af900c3e"
  }
}
```

`GROWTH_OS_DEPLOYMENT_STAGE` is limited to `local`, `staging`,
`staging-candidate`, or `production`. The revision is baked into the image from
the full build-time `GIT_COMMIT_SHA`; a source checkout that was not built as a
traceable image reports the honest default `unknown`. The same revision is in
the image's `org.opencontainers.image.revision` OCI label, while
`org.opencontainers.image.source` identifies this repository. Verify both the
running endpoint and image label during deployment; neither contains secrets or
business data. Health responses use `Cache-Control: no-store`.

For example, compare the running image and endpoint without printing any
environment variables:

```powershell
docker image inspect <image-id> --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
Invoke-RestMethod https://staging.example.test/health/
```

Do not add environment dumps, hostnames, database connection details, keys,
tokens, user data, or build URLs containing credentials to `/health/` or OCI
labels.

No real secret belongs in source control, screenshots, chat, logs, or `.env.example`.

## Verification

```powershell
$env:DATABASE_ENGINE='sqlite'
.\scripts\verify-local.ps1
```

The full test suite currently exercises identity, sealed profiles, task checks,
content review, idempotency, optimistic locking, stale-context detection, and
manual release-gate behavior. SQLite passing is a local development checkpoint;
PostgreSQL/Staging evidence is still required before release.

## Deployment handoff checklist

The infrastructure owner still needs to supply the cloud account, region,
domain, HTTPS certificate path, deployment identity, secret-manager references,
managed PostgreSQL and off-host database backup plan, monitoring destination
and rollback procedure. The frozen production objectives are **RPO no greater than 1 hour**
and **RTO no greater than 4 hours**. They are acceptance limits, not averages,
and must be demonstrated in a restore rehearsal before Production launch.

The versioned Staging topology, immutable-SHA deploy/verification commands,
certificate reload hook, staff-provisioning pattern, rollback boundary and
isolated recovery procedure are documented in [`docs/STAGING-RUNBOOK.md`](docs/STAGING-RUNBOOK.md).
