# Growth OS Staging deployment runbook

This runbook is the reproducible path for the frozen V1 Staging candidate. It
does not authorize a Production launch. A reachable page is not release
evidence: the immutable revision, PostgreSQL behavior, link-only content flow,
negative smoke cases and isolated recovery rehearsal must all pass first.

## 1. Required boundary

- Use a dedicated Staging host, database volume, credentials and DNS name.
  Never connect Staging to Production data.
- Public traffic enters Nginx on ports 80 and 443. Port 80 only redirects to
  HTTPS. Django is additionally published on `127.0.0.1:18000` for host-local
  diagnosis and is never exposed by the firewall.
- Nginx overwrites forwarding headers and rate-limits the login endpoint.
- V1 accepts no file uploads. Immutable content versions store external URLs;
  publication proof stores an external URL and/or platform content ID. The
  runtime persists only database state and requires no content-storage service.
- All application deployments use a clean checkout at one exact 40-character
  Git SHA and a separately approved immutable registry digest. The pulled
  repository digest, OCI revision label and `/health/` revision must agree.
- Migrations run once as a release job. Web container startup does not migrate.
- The Secret directory and the non-secret environment file live outside Git.

## 2. Host prerequisites

Install Git, Docker Engine with Compose v2, `curl`, `openssl`, `sha256sum`, and
the `ss` command supplied by `iproute2`. The deployment script treats a missing
`ss` as a hard failure because it cannot otherwise prove the cutover ports are
available before changing the database.
Create restricted host directories for configuration, Secrets and backups. The
exact locations are infrastructure choices and must not be committed.

The deployment account needs only the permissions required to read the checkout
and Secret files, run this Compose project, write the backup directory, and
reload its own Nginx container. Do not use a shared personal cloud credential.

Pin the PostgreSQL and Nginx images by digest. A tag such as `latest`,
`postgres:17`, or `nginx:alpine` is not sufficient deployment evidence. Record
the selected PostgreSQL major version and keep it equal to the planned
Production major version.

## 3. Non-secret Staging environment file

Create an operator-owned file outside the repository, readable only by the
deployment account. It may contain identifiers and paths, but no passwords,
private keys, Secret IDs, tokens or database URLs. Replace every angle-bracket
placeholder locally:

```dotenv
COMPOSE_PROJECT_NAME=growth-os-staging
DEPLOYMENT_STAGE=staging
# Use bootstrap only for the first sanitized import; change to upgrade only in
# an approved later release window.
STAGING_DEPLOY_MODE=bootstrap
STAGING_HOSTNAME=<staging-dns-name>
STAGING_IMAGE_REPOSITORY=<private-registry-repository-without-tag-or-digest>
STAGING_IMAGE_DIGEST=sha256:<64-lowercase-hex-approved-digest>
STAGING_OWNER_USERNAME=<staging-owner-username>
STAGING_ADMIN_USERNAME=<staging-admin-username>
STAGING_OPERATOR_USERNAME=<staging-operator-username>
STAGING_PRODUCT_CODE=<existing-staging-product-code>
STAGING_PUBLISH_ACCOUNT_CODE=<existing-staging-channel-account-code>
GROWTHOS_UID=<numeric-growthos-uid-from-approved-app-image>
GROWTHOS_GID=<numeric-growthos-gid-from-approved-app-image>
POSTGRES_UID=<numeric-postgres-uid-from-approved-db-image>
POSTGRES_GID=<numeric-postgres-gid-from-approved-db-image>
POSTGRES_IMAGE=<postgres-image>@sha256:<64-lowercase-hex>
NGINX_IMAGE=<nginx-image>@sha256:<64-lowercase-hex>
POSTGRES_DB=<staging-database-name>
POSTGRES_USER=<staging-database-user>
SECRETS_DIR=<absolute-protected-secret-directory>
DEPLOY_BACKUP_DIR=<absolute-protected-backup-directory>
# Required only while initializing the new Compose database volume from the
# audited old candidate. Remove these after the first verified restore:
# STAGING_INITIAL_DUMP=<absolute-path-to-custom-format-baseline-dump>
# STAGING_INITIAL_DUMP_SHA256=<64-lowercase-hex>
```

Export only the path to this file before using the scripts:

```sh
export STAGING_CONFIG_FILE=<absolute-path-to-non-secret-staging-env>
```

## 4. Read-only Secret files

Provision these exact files under the configured `SECRETS_DIR`:

```text
django_secret_key
postgres_password
app_postgres_password
tls_fullchain.pem
tls_privkey.pem
```

The directory should be mode `0700`; every credential and private-key file
should be mode `0400` or `0600`. File-backed Compose Secrets are read-only bind
mounts and retain host ownership on Docker Compose, so `django_secret_key`,
`app_postgres_password` must be owned by the numeric UID of the immutable
image's non-root `growthos` user. `postgres_password` is a separate copy of the
same approved database password and must be owned by the immutable PostgreSQL
image's `postgres` UID. This split avoids making one file readable by two
unrelated container identities. The deploy script checks that the two copies
match without printing them and proves each identity can read only its intended
copy. Never make credential files group/world readable to work around an
ownership error.

Compose mounts the files read-only at `/run/secrets`. Django reads each
configured `*_FILE` directly, so the values are not copied into the container's
configured environment. Do not place their contents in the non-secret
environment file, Compose YAML, shell history, screenshots, chat, image layers,
logs or Git.

Before the first deploy, pull both approved digests and obtain the numeric UIDs
without printing any Secret:

```sh
docker pull "${STAGING_IMAGE_REPOSITORY}@${STAGING_IMAGE_DIGEST}"
growthos_uid=$(docker run --rm --entrypoint id \
  "${STAGING_IMAGE_REPOSITORY}@${STAGING_IMAGE_DIGEST}" -u growthos)
postgres_uid=$(docker run --rm --entrypoint id "$POSTGRES_IMAGE" -u postgres)
growthos_gid=$(docker run --rm --entrypoint id \
  "${STAGING_IMAGE_REPOSITORY}@${STAGING_IMAGE_DIGEST}" -g growthos)
postgres_gid=$(docker run --rm --entrypoint id "$POSTGRES_IMAGE" -g postgres)
test "$growthos_uid" = "$GROWTHOS_UID"
test "$growthos_gid" = "$GROWTHOS_GID"
test "$postgres_uid" = "$POSTGRES_UID"
test "$postgres_gid" = "$POSTGRES_GID"
sudo chown "$growthos_uid:$growthos_gid" "$SECRETS_DIR/django_secret_key" \
  "$SECRETS_DIR/app_postgres_password"
sudo chown "$postgres_uid:$postgres_gid" "$SECRETS_DIR/postgres_password"
sudo chmod 0600 "$SECRETS_DIR/django_secret_key" \
  "$SECRETS_DIR/app_postgres_password" "$SECRETS_DIR/postgres_password"
```

If the deploy stops at either readability check, no migration has run. Correct
ownership while retaining mode `0400` or `0600`, then rerun the same command.
Do not change the TLS private-key owner merely to satisfy the application
container; only Nginx consumes it.

Content records are link-only. A changed deliverable URL must create a new
immutable `ContentAssetVersion` and pass submission and review again; never edit
an already submitted version in place. Review and release-gate rows remain bound
to that exact version, while publication-proof URL/content-ID facts remain
immutable. Operators must use Staging-safe external references and must not
place credentials in URLs.

## 5. DNS, firewall and TLS

Point the Staging DNS name to the candidate host before deployment. Allow public
inbound TCP 80 and 443 only; do not allow public TCP 18000 or PostgreSQL. HTTPS
must use a certificate matching the DNS name. Bare-IP certificates and TLS on
port 80 are not an accepted final topology.

The audited candidate host initially used system Nginx on port 80 and its old
web container used loopback port 8000. The new candidate deliberately uses
loopback port 18000, so the old web process can remain available during the
edge rollback window without blocking the new application. The versioned
topology uses the Compose Nginx service on 80/443, so an operator must schedule
and approve that one-time edge cutover. Verify console/SSH recovery access,
certificate files, DNS and firewall first; test the rendered Compose Nginx
configuration in a one-off container; then stop/disable the old system Nginx.
Phase one never starts the candidate Nginx and therefore leaves the retained old
edge untouched. The explicit exposure script fails while another process owns
80/443. Never let two edge proxies compete for the same ports, and retain the
old host configuration until the new HTTPS endpoint is verified.

With the reviewed SHA and non-secret configuration selected, the pre-cutover
syntax/certificate check can resolve the not-yet-started `web` service to a
loopback placeholder; it opens no public port. Run every check below and retain
the sanitized output **before** stopping the old edge:

```sh
export GIT_COMMIT_SHA=<exact-40-character-reviewed-sha>
set -a
. "$STAGING_CONFIG_FILE"
set +a

# Certificate has at least 24 hours left, covers the exact DNS name, and its
# public key matches the private key. These commands never print private bytes.
openssl x509 -checkend 86400 -noout \
  -in "$SECRETS_DIR/tls_fullchain.pem"
openssl x509 -checkhost "$STAGING_HOSTNAME" -noout \
  -in "$SECRETS_DIR/tls_fullchain.pem"
certificate_key_hash=$(openssl x509 -in "$SECRETS_DIR/tls_fullchain.pem" \
  -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d ' ' -f 1)
private_key_hash=$(openssl pkey -in "$SECRETS_DIR/tls_privkey.pem" \
  -pubout -outform DER 2>/dev/null | sha256sum | cut -d ' ' -f 1)
test "$certificate_key_hash" = "$private_key_hash"
unset certificate_key_hash private_key_hash

docker compose --env-file "$STAGING_CONFIG_FILE" \
  -f deploy/compose.staging.yaml pull nginx
rendered_nginx=$(mktemp)
trap 'rm -f "$rendered_nginx"' EXIT
docker compose --env-file "$STAGING_CONFIG_FILE" \
  -f deploy/compose.staging.yaml run --rm --no-deps \
  --add-host web:127.0.0.1 nginx nginx -T >"$rendered_nginx" 2>/dev/null
test "$(grep -Ec '^[[:space:]]*server[[:space:]]*\{' "$rendered_nginx")" -eq 2
grep -Fq "server_name ${STAGING_HOSTNAME};" "$rendered_nginx"
if grep -Fq '${STAGING_HOSTNAME}' "$rendered_nginx"; then exit 1; fi
rm -f "$rendered_nginx"
trap - EXIT
```

For first `bootstrap`, keep the old system edge running while section 6 checks
the separate loopback-only candidate, then stop it only after the zero-state
gate passes. For every later `upgrade`, stop public writes and the current
candidate Nginx before invoking the deploy script; ports 80/443 must already be
free, and all sessions are invalidated. If the new proxy cannot start, restore
the retained edge only after the post-migration data gate has passed; do not
change the database merely to repair an edge binding.

Configure the approved ACME/certificate manager to renew the two fixed host
certificate files atomically and then run:

```sh
STAGING_CONFIG_FILE=<non-secret-env-path> \
SECRETS_DIR=<protected-secret-directory> \
sh deploy/tls/renew-hook.sh <exact-currently-deployed-40-character-sha>
```

Do not let a root certificate daemon execute an operator-writable checkout.
Have the privileged renewal step replace the certificate files, then invoke the
version-pinned hook as the restricted Docker deployment account through the
approved service boundary. The hook never sources dotenv as shell code: it
strictly parses a fixed key allowlist, rejects duplicate/unknown keys, quoting,
whitespace, command syntax and unsafe values, then gives those parsed values to
Compose. It checks expiry and the exact lowercase hostname, verifies that the
certificate and key match, tests the rendered configuration in a one-off
container, and recreates Nginx so Docker remounts atomically replaced certificate files.
The hook also rechecks that the Secret directory is a real `0700` directory,
that both renewed files are regular non-symlink files, and that the private key
remains `0400` or `0600`; an atomic replacement with broader permissions fails
before any Docker action. Before accepting Staging, run the certificate
provider's dry-run renewal and retain its output. Configure
expiry alerts independently; a renewal hook is not an alerting system.

## 6. Deploy one immutable candidate

Gate 0 is an approved application image, not a mutable tag. In trusted CI or an
equivalent controlled builder—not on the target host—build the clean reviewed
SHA with the Dockerfile's full `GIT_COMMIT_SHA` build argument, push it to the
private registry, and record the registry-returned `sha256:` digest. A reviewer
must approve the exact tuple `(Git SHA, repository, registry digest)` and put
that digest in `STAGING_IMAGE_DIGEST`. The target host only pulls and runs
`STAGING_IMAGE_REPOSITORY@STAGING_IMAGE_DIGEST`; it never builds or trusts a
locally tagged application image. Deployment remains blocked until this
approved registry artifact exists.

Fetch the remote repository, check out the exact reviewed SHA in detached state,
and verify that the checkout is clean. Do not deploy a branch name:

```sh
git fetch --prune origin
git checkout --detach <exact-40-character-reviewed-sha>
git status --porcelain
```

The status command must print nothing.

The new Compose project intentionally owns a new database volume. Before its
first deployment, stop acceptance writes to the old candidate, take one final
custom-format `pg_dump --no-owner --no-acl`, copy it to a controlled path
outside the checkout, Docker build context, and host-local backup directory,
set mode `0400` or `0600`, copy an encrypted copy off-host, and record its
SHA-256. The source must be a non-symlink regular file. Set `STAGING_INITIAL_DUMP` and
`STAGING_INITIAL_DUMP_SHA256` in the non-secret configuration for that first
run. The deploy script restores it in one transaction only when the new
database has no Django schema and no other public objects; otherwise it fails
closed. This preserves Product/Profile/Contract context and history without
reusing or mutating the old Docker volume. After a verified first restore,
remove the two optional settings and securely destroy the host import copy;
retain only the access-controlled off-host backup under its retention policy.
Later deployments use the already initialized new volume.

The baseline must be a separately audited, Staging-only export. Before approving
it, inventory all Principals, Django staff/superuser flags, Grants, sessions,
and externally referenced content/proof rows. It must contain no Production
data, shared or legacy active human identity, staff/superuser identity, or live
session. Rotating `django_secret_key` alone is not accepted as identity
sanitization. After restore, the deploy script repeats the identity and session
counts and stops with the web service still loopback-only if any forbidden count
is non-zero. It also rejects every `ContentAssetVersion` that is not an explicit
link manifest (`text/uri-list`, `metadata.source=external-url`, and an `http://`
or `https://` object key), so an old file-backed record cannot silently become a
broken link. External URLs and platform content IDs are database references;
the deployment does not copy, fetch, hash or otherwise certify remote bytes.

For the first sanitized import, keep `STAGING_DEPLOY_MODE=bootstrap` and run:

```sh
sh scripts/deploy-staging.sh <exact-40-character-reviewed-sha> bootstrap
```

The script refuses a dirty or mismatched checkout, pulls the exact approved
application digest, checks its registry digest and OCI revision label, starts
PostgreSQL, creates a mode-0600 pre-migration custom-format backup and checksum,
runs the one-off migration job, then replaces only the loopback-bound web
service. Bootstrap requires that no candidate web container already exists and
repeats the fail-closed zero active-human/session gate. It does
**not** start Nginx, delete the prior image, or merge the PR.

If the pre-migration backup fails or is empty, the migration does not run. Copy
the backup to the approved independent backup destination before treating it as
recovery evidence; a file on the same host is not a resilient backup.

For every later release, set `STAGING_DEPLOY_MODE=upgrade`, remove both
`STAGING_INITIAL_DUMP*` settings, stop public writes and candidate Nginx, and
prove ports 80/443 are free. Keep the existing web service on loopback for the
pre-migration gate. Then provide a one-invocation confirmation equal to the
reviewed target SHA. Never store this confirmation in the environment file:

```sh
STAGING_UPGRADE_QUIESCE_CONFIRMATION=<exact-40-character-reviewed-sha> \
  sh scripts/deploy-staging.sh <exact-40-character-reviewed-sha> upgrade
```

Upgrade fails before migration unless the current loopback web is healthy,
the exact three configured active HUMAN accounts and exact bounded Grants pass
the locked provisioning dry run, the exact committed Operator PUBLISH Grant is
present, no staff/superuser exists, and every existing content version passes
the link-only gate. It invalidates every Django session, then applies the
database integrity checks, backs up, migrates once and replaces web. It repeats
the exact identity/Grant, zero-session, and link-only gates against the new code
after migration. Bootstrap runs the same link-only gate after restore and
migration. Existing immutable link and proof history is retained; the deployment
never fetches external content as a release prerequisite. Any failed pre/post
check leaves Nginx stopped and the candidate unexposed.

## 7. Provision acceptance-test staff while still loopback-only

This section is bootstrap-only. Do not recreate, rotate or silently extend the
three accounts during `upgrade`; the upgrade gates verify their existing exact
identities and Grants.

Do this only after bootstrap has reported zero old identities and sessions, and
after confirming that the sealed Product, contract,
ChannelAccount, Staging binding and OPEN CapabilityState are present. The
candidate still has no public Nginx at this point. The five non-secret
`STAGING_*` identity/context values in the protected config are the frozen
values used both here and by the exposure gate.

Use three different temporary password files, each at least 12 characters and
mode `0600`. The host must chown all three files to the exact numeric
`growthos` UID used by the running immutable web image; otherwise the non-root
one-off container cannot read them. Keep their parent directory mode `0700`
under the deployment operator and never pass passwords as command arguments.

```sh
set -a
. "$STAGING_CONFIG_FILE"
set +a
export GIT_COMMIT_SHA=<exact-currently-deployed-40-character-sha>
growthos_uid=$(docker compose --env-file "$STAGING_CONFIG_FILE" \
  -f deploy/compose.staging.yaml exec -T web id -u)
growthos_gid=$(docker compose --env-file "$STAGING_CONFIG_FILE" \
  -f deploy/compose.staging.yaml exec -T web id -g)
test "$growthos_uid" = "$GROWTHOS_UID"
test "$growthos_gid" = "$GROWTHOS_GID"
sudo chown "$growthos_uid:$growthos_gid" <owner-password-file> <admin-password-file> \
  <operator-password-file>
sudo chmod 0600 <owner-password-file> <admin-password-file> \
  <operator-password-file>
stat -c '%u %a %n' <owner-password-file> <admin-password-file> \
  <operator-password-file>
```

The `stat` output must show the `growthos` numeric UID and `600` for every file;
it is safe evidence because it contains paths and modes, not password values.

Use ephemeral read-only mounts and pass only their file paths to the command.
The password values never need to become command arguments or environment
values. Run the rollback-only dry run first, then repeat with `--apply`. Use the
exact config-bound usernames and context; substitute only the three protected
password-file paths locally:

```sh
GIT_COMMIT_SHA=<exact-currently-deployed-40-character-sha> \
docker compose --env-file "$STAGING_CONFIG_FILE" \
  -f deploy/compose.staging.yaml run --rm \
  -v <owner-password-file>:/run/provision/owner:ro \
  -v <admin-password-file>:/run/provision/admin:ro \
  -v <operator-password-file>:/run/provision/operator:ro \
  -e STAGING_OWNER_PASSWORD_FILE=/run/provision/owner \
  -e STAGING_ADMIN_PASSWORD_FILE=/run/provision/admin \
  -e STAGING_OPERATOR_PASSWORD_FILE=/run/provision/operator \
  --entrypoint python web manage.py provision_staging_staff \
      --owner-username "$STAGING_OWNER_USERNAME" \
      --admin-username "$STAGING_ADMIN_USERNAME" \
      --operator-username "$STAGING_OPERATOR_USERNAME" \
      --product-code "$STAGING_PRODUCT_CODE" \
      --publish-account-code "$STAGING_PUBLISH_ACCOUNT_CODE"
```

After a successful dry run, add `--apply` after the final account-code argument.
Delete all three temporary password files immediately after successful account
creation using the host's approved secure deletion/Secret lifecycle process.
If dry-run/apply is abandoned or fails, delete all three anyway and issue three
new values before retrying; never leave them on the host for a later attempt.
Do not create a Django superuser. Owner, Admin and Operator remain three distinct
normal Principals; REVIEW and PUBLISH remain separately scoped grants.

These test Grants expire after 30 days. This V1 command is not a renewal path:
at expiry, disable the temporary accounts and revoke/expire their Grants, then
open a new controlled change to create a new independent test-account set with
new credentials and bounded Grants. Never extend `valid_until`, reuse the old
password files, or silently renew authority in place.

## 8. Expose and verify the running candidate

After bootstrap provisioning succeeds—or immediately after an upgrade's
post-migration gates pass—confirm `ss` shows 80 and 443 free, then expose the
new edge with the same reviewed mode explicitly:

```sh
set -a
. "$STAGING_CONFIG_FILE"
set +a
sh scripts/expose-staging-edge.sh <exact-40-character-reviewed-sha> \
  "$STAGING_DEPLOY_MODE"
```

This command requires the exact three config-bound active HUMAN Principals,
their exact roles, internal usable credentials, no staff/superuser flags, no
fourth active human, no sessions, successful transactionally locked
provisioning dry-run verification, and an already-committed exact bounded
Operator PUBLISH Grant. Existing immutable external-link history is retained
without fetching remote content. The exposure command also repeats the running digest, loopback
binding, health, certificate, rendered-template, and no-extra-vhost checks
before binding 80/443. It performs no migration.

Immediately run the verifier using the same exact SHA:

```sh
sh scripts/verify-staging.sh <exact-40-character-reviewed-sha>
```

It verifies, without printing Secrets or creating external content:

- the approved application registry digest, running image ID and matching OCI
  revision label;
- application binding at `127.0.0.1:18000`;
- read-only Secret mounts and absence of plaintext Secret environment entries;
- explicit Staging mode, password minimum of 12, PostgreSQL engine and version;
- no pending migration, Django checks, deploy checks, link-only content-version
  records and Nginx syntax;
- login throttling and overwritten forwarding headers;
- trusted HTTPS, exact port-80 canonical redirects (including `Host:
  localhost`), healthy database and exact `/health/` SHA.

Archive the command output with the tested SHA and time. Warnings from
`manage.py check --deploy` must be evaluated; exit status alone is not approval.

## 9. PostgreSQL and smoke acceptance

After provisioning, run the full test suite and the PostgreSQL-only two-
connection tests against Staging PostgreSQL. Preserve the raw output and the
checkout SHA. A SQLite pass or a PostgreSQL test marked SKIP is not evidence.

The manual smoke test must use different Principals and cover the full path:
login/RBAC, sealed Product Profile, Task/Contract, exact ContentAssetVersion,
DoR/DoD, Submission, Admin review, fail-closed Release Gate and Operator manual
publication proof. Also prove these negative cases:

- a submitter cannot review their own Submission;
- an Operator without REVIEW cannot review;
- a Principal without the exact PUBLISH grant cannot publish;
- closed CapabilityState blocks publication;
- a withdrawn/old Submission cannot be reviewed or reused;
- an expired or context-stale Gate cannot be reused;
- duplicate/conflicting requests leave no partial records.

After the new candidate passes the complete smoke test, close the old-stack
rollback window on the approved, time-boxed schedule (normally no more than 24
hours). Record the old project name, container IDs, image digests and volume
names; stop and disable every old web/database container so
`restart: unless-stopped` cannot bring it back. Do not use
`docker compose down -v`, and do not delete the old data volumes here. Retain those stopped
volumes under the explicit data-retention decision, access-control them, and
dispose of them only through a later approved destruction change. This removes
the old environment and obsolete credential attack surface without
destroying the rollback evidence.

## 10. Backup and isolated recovery rehearsal

The pre-migration dump and an hourly `pg_dump` are temporary Staging safeguards,
not the frozen Production recovery design. Before promotion, PostgreSQL must
have encrypted base backups plus continuous WAL/PITR copied to an
access-controlled destination off the database and application host, with
archive delay no more than 15 minutes and an isolated recovery window of at
least 14 days. A Docker volume or dump retained only on the candidate host is
not a backup. Monitor backup success, WAL delay, capacity, off-host replication
and restore failures, with Owner/Admin alerts before the one-hour RPO can be
exceeded. Database backups cover Growth OS audit records and external-link
metadata; the remote content behind those links remains outside Growth OS's
recovery boundary.

Run restoration under a different Compose project name and on a separate host,
VM, or isolated Docker volume. Never restore over the candidate database. A
typical isolated rehearsal is:

1. Record the backup timestamp, Git SHA, image digest and start time.
2. Create an isolated database volume/project using the same PostgreSQL major.
3. Verify the dump checksum before `pg_restore`.
4. Restore with `pg_restore --clean --if-exists --no-owner --no-acl` into the
   isolated database.
5. Start the exact application image against the restored database.
6. Run `/health/`, record counts/checksums for acceptance records, and inspect
   representative content-version and publication-proof link rows.
7. Record the recovered point and finish time.

The rehearsal passes only if measured data loss is at most one hour and service
recovery is at most four hours. Store the commands, sanitized output, timing,
checksums and operator decision as the recovery evidence/runbook addendum.

## 11. Rollback

Do not reverse migrations automatically. If the database remains compatible,
restore the previously approved `(Git SHA, repository digest)` in the protected
Staging configuration, pull that exact digest, recreate only web, leave the
validated Nginx proxy in place, and rerun verification against that SHA. Never
roll back by selecting a tag.

```sh
set -a
. "$STAGING_CONFIG_FILE"
set +a
export GIT_COMMIT_SHA=<previous-approved-40-character-sha>
export STAGING_IMAGE_DIGEST=sha256:<previous-approved-64-hex-digest>
docker pull "${STAGING_IMAGE_REPOSITORY}@${STAGING_IMAGE_DIGEST}"
docker compose --env-file "$STAGING_CONFIG_FILE" \
  -f deploy/compose.staging.yaml up -d --no-deps --force-recreate web
sh scripts/verify-staging.sh "$GIT_COMMIT_SHA"
```

Before running the scripts, update the protected config's
`STAGING_IMAGE_DIGEST` to that same approved rollback digest; the exported line
above is only an operator guard for the direct Compose command. The retained
repository digest and its OCI revision label must match before this command.
If the old container does not become healthy, stop and use the isolated database
restore path; do not repeatedly restart it against a potentially incompatible
schema.

If a migration is not backward-compatible, stop new writes, take another dump,
restore the pre-migration backup into an isolated replacement database, verify
it, and only then switch the old image to that restored database. Never overwrite
the current database in place. Record the failed SHA,
reason, database decision, operator, start/end time and verification evidence.

## 12. Promotion gate

Keep the PR unmerged and label the environment `staging-candidate` until all of
the following exist: the approved `(Git SHA, repository digest)` and matching
running/health evidence; zero-count pre-edge identity/session evidence;
link-only content and publication-proof acceptance evidence; PostgreSQL full
and concurrency passes; structured positive/negative smoke evidence;
certificate renewal evidence; documented retirement of the old stack;
encrypted off-host PostgreSQL PITR backup and alerting evidence; and an isolated
recovery rehearsal with RPO no greater than one hour and RTO no greater than
four hours. Production
requires a separate approved deployment decision and Production-specific
Secrets, domain, database backup and HSTS validation.
