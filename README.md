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

Optional demo identities are requested with `--operator-username`,
`--reviewer-username`, `--publisher-username`, and
`--rule-evaluator-username`, or together with `--full-demo`. A new human
identity requires its own `BOOTSTRAP_OPERATOR_PASSWORD`,
`BOOTSTRAP_REVIEWER_PASSWORD`, or `BOOTSTRAP_PUBLISHER_PASSWORD`; existing
matching accounts are reused without resetting their passwords. Missing or
reused password values fail the transaction without creating partial staff.
The rule evaluator is a service Principal with an unusable interactive
password.

For the complete local role set, provide four distinct passwords through the
temporary process environment and run:

```powershell
.\.venv\Scripts\python.exe manage.py bootstrap_dogfood --full-demo
```

Clear the four `BOOTSTRAP_*_PASSWORD` environment variables immediately after
the command. Never put their values in source, `.env.example`, chat, screenshots,
logs, or shell scripts.

## Docker/PostgreSQL start

1. Copy `.env.example` to `.env` and replace every placeholder locally.
2. Install Docker Desktop.
3. Run `docker compose up -d --build`, then inspect `docker compose ps` and
   `docker compose logs --tail 200 web db`.

The canonical Compose file is `compose.yaml` (the current Docker Compose default
name). The web port is bound to `127.0.0.1` instead of every network interface.
Open `http://127.0.0.1:8000/`, or change `WEB_PORT` locally if that port is in use.

The container waits for PostgreSQL with a bounded retry, then runs migrations,
collects static files and finally starts Gunicorn. WhiteNoise serves versioned
static assets from the container; user-uploaded media uses the `media_data`
volume. This Compose topology is a single-web-instance bootstrap environment.
Before adding multiple web replicas, migrations must move into a one-off release
job so several instances cannot race the same schema change.

The current frozen source uses the local/media volume storage backend. A Tencent
Cloud object-storage adapter and its credential injection are not implemented yet;
they are a Staging prerequisite.

## Staging / production boundary

The repository is deployable source, not evidence of a completed deployment.
Before a staging or production start:

- set `GROWTH_OS_ENV=production`;
- provide a new high-entropy `DJANGO_SECRET_KEY`;
- set exact `DJANGO_ALLOWED_HOSTS` and HTTPS origins in
  `DJANGO_CSRF_TRUSTED_ORIGINS`;
- set `TRUST_PROXY_SSL_HEADER=1` only when a trusted reverse proxy terminates
  HTTPS and **overwrites** `X-Forwarded-Proto`;
- keep the application port private and let the reverse proxy expose ports
  80/443, HTTPS certificates and public HTTP-to-HTTPS redirects;
- use `POSTGRES_SSLMODE=require` for a managed TLS database. The bundled
  Compose database explicitly uses `disable` because it is private to the
  Compose network and has no TLS listener;
- configure backups and prove restore time before launch. The named Docker
  volume is persistence, not a backup.

The `/health/` endpoint is exempt from Django's HTTPS redirect so Docker can
check the private HTTP listener. It returns service health only and must not
contain secrets or business data.

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
domain, HTTPS certificate path, IAM identities, secret-manager references,
managed database or volume backup plan, monitoring destination and rollback
procedure. The frozen production objectives are **RPO no greater than 1 hour**
and **RTO no greater than 4 hours**. They are acceptance limits, not averages,
and must be demonstrated in a restore rehearsal before Production launch.
