#!/bin/sh
set -eu

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

require_value() {
    variable_name="$1"
    eval "variable_value=\${${variable_name}:-}"
    [ -n "$variable_value" ] || fail "$variable_name is required."
}

require_digest_image() {
    variable_name="$1"
    eval "image_reference=\${${variable_name}:-}"
    case "$image_reference" in
        *@sha256:*) ;;
        *) fail "$variable_name must be an immutable image reference ending in @sha256:<64 lowercase hex>." ;;
    esac
    image_digest=${image_reference##*@sha256:}
    [ "${#image_digest}" -eq 64 ] || fail "$variable_name has an invalid SHA-256 digest length."
    case "$image_digest" in
        *[!0-9a-f]*) fail "$variable_name has a non-lowercase or non-hex digest." ;;
    esac
}

require_sha256_digest() {
    variable_name="$1"
    eval "digest_value=\${${variable_name}:-}"
    case "$digest_value" in
        sha256:*) ;;
        *) fail "$variable_name must be sha256:<64 lowercase hex>." ;;
    esac
    digest_hex=${digest_value#sha256:}
    [ "${#digest_hex}" -eq 64 ] || fail "$variable_name must be sha256:<64 lowercase hex>."
    case "$digest_hex" in
        *[!0-9a-f]*) fail "$variable_name must be sha256:<64 lowercase hex>." ;;
    esac
}

release_sha="${1:-}"
deploy_mode="${2:-}"
[ "${#release_sha}" -eq 40 ] || fail "Usage: $0 <exact-40-character-git-sha> <bootstrap|upgrade>"
case "$release_sha" in
    *[!0-9a-f]*) fail "The release SHA must contain lowercase hexadecimal characters only." ;;
esac
case "$deploy_mode" in
    bootstrap|upgrade) ;;
    *) fail "Deployment mode must be exactly bootstrap or upgrade." ;;
esac

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="${COMPOSE_FILE:-$repo_root/deploy/compose.staging.yaml}"
config_file="${STAGING_CONFIG_FILE:-}"
[ -n "$config_file" ] && [ -r "$config_file" ] || fail "STAGING_CONFIG_FILE must name a readable, non-secret Compose environment file."
if grep -Eq '^[[:space:]]*STAGING_UPGRADE_QUIESCE_CONFIRMATION=' "$config_file"
then
    fail "STAGING_UPGRADE_QUIESCE_CONFIRMATION is one-invocation evidence and must not be persisted in the config file."
fi

# This operator-owned file contains identifiers and paths only, never secrets.
set -a
# shellcheck disable=SC1090
. "$config_file"
set +a

require_value STAGING_HOSTNAME
require_value DEPLOYMENT_STAGE
require_value STAGING_DEPLOY_MODE
require_value STAGING_IMAGE_REPOSITORY
require_sha256_digest STAGING_IMAGE_DIGEST
require_value STAGING_OWNER_USERNAME
require_value STAGING_ADMIN_USERNAME
require_value STAGING_OPERATOR_USERNAME
require_value STAGING_PRODUCT_CODE
require_value STAGING_PUBLISH_ACCOUNT_CODE
require_value GROWTHOS_UID
require_value GROWTHOS_GID
require_value POSTGRES_UID
require_value POSTGRES_GID
require_value POSTGRES_DB
require_value POSTGRES_USER
require_value SECRETS_DIR
require_value DEPLOY_BACKUP_DIR
require_digest_image POSTGRES_IMAGE
require_digest_image NGINX_IMAGE

[ "$DEPLOYMENT_STAGE" = "staging" ] || fail "DEPLOYMENT_STAGE must be exactly staging."
[ "$STAGING_DEPLOY_MODE" = "$deploy_mode" ] \
    || fail "CLI deployment mode does not match STAGING_DEPLOY_MODE in the reviewed config."
[ "$deploy_mode" != "upgrade" ] || [ "${STAGING_UPGRADE_QUIESCE_CONFIRMATION:-}" = "$release_sha" ] \
    || fail "Upgrade requires STAGING_UPGRADE_QUIESCE_CONFIRMATION to equal the exact release SHA after public writes are stopped."

for numeric_identity in GROWTHOS_UID GROWTHOS_GID POSTGRES_UID POSTGRES_GID
do
    eval "numeric_value=\${${numeric_identity}:-}"
    case "$numeric_value" in
        ""|*[!0-9]*) fail "$numeric_identity must be a numeric container identity from the approved image." ;;
    esac
done
unset numeric_identity numeric_value

case "$STAGING_IMAGE_REPOSITORY" in
    *@*) fail "STAGING_IMAGE_REPOSITORY must omit tags and digests." ;;
esac
image_repository_name=${STAGING_IMAGE_REPOSITORY##*/}
case "$image_repository_name" in
    *:*) fail "STAGING_IMAGE_REPOSITORY must omit a mutable tag." ;;
esac

case "$STAGING_HOSTNAME" in
    *[!a-z0-9.-]*|.*|*.) fail "STAGING_HOSTNAME must be a lowercase bare DNS hostname, without a scheme, path, or port." ;;
esac
[ -d "$SECRETS_DIR" ] || fail "SECRETS_DIR does not exist."
[ "$(stat -c '%a' "$SECRETS_DIR")" = "700" ] || fail "SECRETS_DIR must have host mode 0700."

for secret_name in django_secret_key postgres_password app_postgres_password tls_fullchain.pem tls_privkey.pem
do
    [ -s "$SECRETS_DIR/$secret_name" ] || fail "Required Secret file is missing or empty: $secret_name"
done
for secret_name in django_secret_key postgres_password app_postgres_password
do
    secret_mode=$(stat -c '%a' "$SECRETS_DIR/$secret_name")
    case "$secret_mode" in
        400|600) ;;
        *) fail "$secret_name must have host mode 0400 or 0600." ;;
    esac
done
private_key_mode=$(stat -c '%a' "$SECRETS_DIR/tls_privkey.pem")
case "$private_key_mode" in
    400|600) ;;
    *) fail "tls_privkey.pem must have host mode 0400 or 0600." ;;
esac

certificate="$SECRETS_DIR/tls_fullchain.pem"
private_key="$SECRETS_DIR/tls_privkey.pem"
openssl x509 -checkend 86400 -noout -in "$certificate" >/dev/null \
    || fail "The TLS certificate expires within 24 hours."
openssl x509 -checkhost "$STAGING_HOSTNAME" -noout -in "$certificate" >/dev/null \
    || fail "The TLS certificate does not match STAGING_HOSTNAME."
certificate_public_key=$(openssl x509 -in "$certificate" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d ' ' -f 1)
private_public_key=$(openssl pkey -in "$private_key" -pubout -outform DER 2>/dev/null | sha256sum | cut -d ' ' -f 1)
[ -n "$certificate_public_key" ] && [ "$certificate_public_key" = "$private_public_key" ] \
    || fail "The TLS certificate and private key do not match."
unset certificate_public_key private_public_key

cd "$repo_root"
git cat-file -e "${release_sha}^{commit}" 2>/dev/null || fail "The requested SHA is not present in this checkout."
[ "$(git rev-parse HEAD)" = "$release_sha" ] || fail "HEAD is not the requested release SHA. Check out the exact commit first."
[ -z "$(git status --porcelain)" ] || fail "The checkout is not clean. Refusing a non-reproducible build."

export GIT_COMMIT_SHA="$release_sha"
compose() {
    docker compose --env-file "$config_file" -f "$compose_file" "$@"
}

verify_exact_staging_staff_and_grants() {
    compose exec -T web python -c '
import sys
from django.conf import settings
from django.db.models import Q

from accounts.models import Principal

if settings.ENVIRONMENT != "staging":
    raise SystemExit("UPGRADE_ENVIRONMENT_NOT_STAGING")
expected = {
    sys.argv[1]: Principal.Role.OWNER,
    sys.argv[2]: Principal.Role.OPERATIONS_ADMIN,
    sys.argv[3]: Principal.Role.OPERATOR,
}
active = list(Principal.objects.filter(
    principal_type=Principal.PrincipalType.HUMAN_USER,
    is_active=True,
).order_by("username"))
if len(active) != 3 or {principal.username for principal in active} != set(expected):
    raise SystemExit("UPGRADE_ACTIVE_HUMAN_SET_NOT_EXACT")
for principal in active:
    if (
        principal.role != expected.get(principal.username)
        or principal.principal_status != Principal.PrincipalStatus.ACTIVE
        or principal.auth_provider != "internal"
        or principal.is_staff
        or principal.is_superuser
        or not principal.has_usable_password()
    ):
        raise SystemExit("UPGRADE_STAGING_IDENTITY_ATTRIBUTES_INVALID")
if Principal.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).exists():
    raise SystemExit("UPGRADE_STAFF_OR_SUPERUSER_EXISTS")
print("Exact-three Staging identity gate: PASS")
' "$STAGING_OWNER_USERNAME" "$STAGING_ADMIN_USERNAME" "$STAGING_OPERATOR_USERNAME"

    # Existing identities make this a transactionally locked, zero-write
    # verification of the complete base Grant set.
    compose exec -T web python manage.py provision_staging_staff \
        --owner-username "$STAGING_OWNER_USERNAME" \
        --admin-username "$STAGING_ADMIN_USERNAME" \
        --operator-username "$STAGING_OPERATOR_USERNAME" \
        --product-code "$STAGING_PRODUCT_CODE" \
        --publish-account-code "$STAGING_PUBLISH_ACCOUNT_CODE"

    # The dry run may model a missing PUBLISH Grant and roll it back. Prove one
    # exact bounded HIGH Grant was already committed.
    compose exec -T web python -c '
import sys
from django.db.models import Q
from django.utils import timezone
from accounts.models import PermissionGrant, Principal

operator = Principal.objects.get(username=sys.argv[1])
now = timezone.now()
grants = list(PermissionGrant.objects.filter(
    principal=operator,
    scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
    product__isnull=True,
    platform_code="",
    account_ref=sys.argv[2],
    surface_ref="",
    action=PermissionGrant.Action.PUBLISH,
    effect=PermissionGrant.Effect.ALLOW,
    risk_level=PermissionGrant.RiskLevel.HIGH,
    grant_status=PermissionGrant.GrantStatus.ACTIVE,
    valid_from__lte=now,
).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now)))
if len(grants) != 1 or grants[0].valid_until is None:
    raise SystemExit("UPGRADE_EXACT_BOUNDED_PUBLISH_GRANT_MISSING")
print("Exact bounded Operator PUBLISH Grant: PASS")
' "$STAGING_OPERATOR_USERNAME" "$STAGING_PUBLISH_ACCOUNT_CODE"
}

invalidate_all_sessions() {
    compose exec -T web python -c '
from django.contrib.sessions.models import Session
deleted, _ = Session.objects.all().delete()
if Session.objects.exists():
    raise SystemExit("UPGRADE_SESSION_INVALIDATION_FAILED")
print(f"Staging sessions invalidated: {deleted}")
'
}

verify_link_only_content_versions() {
    compose exec -T web python -c '
from contentops.models import ContentAssetVersion

invalid_count = 0
sample_ids = []
total = 0
for version in ContentAssetVersion.objects.only(
    "id", "mime_type", "metadata", "object_key"
).iterator():
    total += 1
    metadata = version.metadata
    if (
        version.mime_type != "text/uri-list"
        or not isinstance(metadata, dict)
        or metadata.get("source") != "external-url"
        or not version.object_key.startswith(("http://", "https://"))
    ):
        invalid_count += 1
        if len(sample_ids) < 5:
            sample_ids.append(str(version.pk))
if invalid_count:
    rendered_ids = ",".join(sample_ids)
    raise SystemExit(
        "LINK_ONLY_CONTENT_VERSION_GATE_FAILED: "
        f"invalid_count={invalid_count}; sample_ids={rendered_ids}"
    )
print(f"Link-only ContentAssetVersion gate: PASS ({total} versions)")
'
}

verify_bootstrap_zero_state() {
    compose exec -T web python -c '
from django.contrib.sessions.models import Session
from django.db.models import Q
from accounts.models import Principal

counts = {
    "active_human_principals": Principal.objects.filter(
        principal_type=Principal.PrincipalType.HUMAN_USER,
        is_active=True,
    ).count(),
    "staff_or_superuser_principals": Principal.objects.filter(
        Q(is_staff=True) | Q(is_superuser=True),
    ).count(),
    "sessions": Session.objects.count(),
}
if any(counts.values()):
    rendered = ", ".join(f"{name}={value}" for name, value in counts.items())
    raise SystemExit(f"BOOTSTRAP_BASELINE_NOT_SANITIZED: {rendered}")
print("Bootstrap identity/session gate: PASS (all counts zero)")
'
}

compose config --quiet
command -v ss >/dev/null 2>&1 || fail "The iproute2 ss command is required for edge-port preflight."
existing_project_nginx=$(compose ps -q nginx 2>/dev/null || true)
[ -z "$existing_project_nginx" ] \
    || fail "The candidate Nginx service must be stopped before phase-one deployment; no new application may be public before the pre-edge gate."
existing_project_web=$(compose ps -q web 2>/dev/null || true)
if [ "$deploy_mode" = "upgrade" ]
then
    [ -n "$existing_project_web" ] \
        || fail "Upgrade mode requires the currently approved loopback web container for pre-migration verification."
    occupied_edge_ports=$(ss -H -ltn | awk '{print $4}' | grep -E ':(80|443)$' || true)
    [ -z "$occupied_edge_ports" ] \
        || fail "Upgrade mode requires public edge ports 80/443 to be stopped before any session or database change."
else
    [ -z "$existing_project_web" ] \
        || fail "Bootstrap mode requires no existing candidate web container; use upgrade for an initialized Staging stack."
fi
if [ -z "$existing_project_web" ]
then
    occupied_application_port=$(ss -H -ltn | awk '{print $4}' | grep -E ':18000$' || true)
    [ -z "$occupied_application_port" ] \
        || fail "Host port 18000 is already owned outside this Compose project. Refusing to migrate before the application binding is available."
fi
image_reference="${STAGING_IMAGE_REPOSITORY}@${STAGING_IMAGE_DIGEST}"
docker pull "$image_reference"
pulled_repo_digests=$(docker image inspect "$image_reference" --format '{{range .RepoDigests}}{{println .}}{{end}}')
printf '%s\n' "$pulled_repo_digests" | grep -Fq "@${STAGING_IMAGE_DIGEST}" \
    || fail "The pulled application image does not expose the approved repository digest."
image_revision=$(docker image inspect "$image_reference" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
[ "$image_revision" = "$release_sha" ] || fail "The approved image revision label does not match the requested SHA."
actual_growthos_uid=$(docker run --rm --network none --entrypoint id "$image_reference" -u growthos)
actual_growthos_gid=$(docker run --rm --network none --entrypoint id "$image_reference" -g growthos)
[ "$actual_growthos_uid" = "$GROWTHOS_UID" ] && [ "$actual_growthos_gid" = "$GROWTHOS_GID" ] \
    || fail "Configured growthos UID/GID does not match the approved application image."
actual_postgres_uid=$(docker run --rm --network none --entrypoint id "$POSTGRES_IMAGE" -u postgres)
actual_postgres_gid=$(docker run --rm --network none --entrypoint id "$POSTGRES_IMAGE" -g postgres)
[ "$actual_postgres_uid" = "$POSTGRES_UID" ] && [ "$actual_postgres_gid" = "$POSTGRES_GID" ] \
    || fail "Configured postgres UID/GID does not match the approved PostgreSQL image."
unset actual_growthos_uid actual_growthos_gid actual_postgres_uid actual_postgres_gid

# Compare the separately owned database-password copies inside an isolated
# root container. The deployment user need not be able to read either host
# file, and no password bytes are emitted.
docker run --rm --network none --read-only --user 0 \
    --mount "type=bind,src=$SECRETS_DIR/postgres_password,dst=/run/compare/postgres_password,readonly" \
    --mount "type=bind,src=$SECRETS_DIR/app_postgres_password,dst=/run/compare/app_postgres_password,readonly" \
    --entrypoint python "$image_reference" -c '
from pathlib import Path
assert Path("/run/compare/postgres_password").read_bytes() == Path("/run/compare/app_postgres_password").read_bytes()
'

# Local Compose implements file-backed Secrets as read-only bind mounts on
# some engines, so host ownership can still affect the non-root image user.
# Prove readability without printing or copying any Secret value.
docker run --rm --network none --read-only \
    --mount "type=bind,src=$SECRETS_DIR/django_secret_key,dst=/run/secrets/django_secret_key,readonly" \
    --mount "type=bind,src=$SECRETS_DIR/app_postgres_password,dst=/run/secrets/postgres_password,readonly" \
    --entrypoint python "$image_reference" -c '
from pathlib import Path
paths = (
    "/run/secrets/django_secret_key",
    "/run/secrets/postgres_password",
)
assert all(Path(path).read_bytes() for path in paths)
'

# Prove the separately owned database copy is readable by the image's postgres
# identity as well. This emits no password bytes.
docker run --rm --network none --read-only --user postgres \
    --mount "type=bind,src=$SECRETS_DIR/postgres_password,dst=/run/secrets/postgres_password,readonly" \
    --entrypoint sh "$POSTGRES_IMAGE" -ec 'test -s /run/secrets/postgres_password'

if [ "$deploy_mode" = "upgrade" ]
then
    current_health=$(docker inspect "$existing_project_web" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')
    [ "$current_health" = "healthy" ] \
        || fail "The current loopback web container must be healthy for pre-migration upgrade verification."
    verify_exact_staging_staff_and_grants
    verify_link_only_content_versions
    invalidate_all_sessions
    echo "Pre-migration upgrade data gate: PASS"
fi

compose up -d db
db_container=$(compose ps -q db)
[ -n "$db_container" ] || fail "The PostgreSQL container did not start."
db_health_attempt=1
while [ "$db_health_attempt" -le 30 ]
do
    db_health_status=$(docker inspect "$db_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')
    [ "$db_health_status" = "healthy" ] && break
    [ "$db_health_status" = "unhealthy" ] && fail "The PostgreSQL container became unhealthy."
    sleep 2
    db_health_attempt=$((db_health_attempt + 1))
done
[ "${db_health_status:-missing}" = "healthy" ] || fail "PostgreSQL did not become healthy in time."

# A new Compose project owns a new PostgreSQL volume. It must be initialized
# from the audited candidate dump rather than silently migrating an empty DB.
database_has_migrations=$(compose exec -T db sh -ec '
    export PGPASSWORD="$(cat /run/secrets/postgres_password)"
    psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align \
      --command="select (to_regclass('"'"'public.django_migrations'"'"') is not null)::int"
' | tr -d '[:space:]')
if [ "$deploy_mode" = "upgrade" ] && { [ -n "${STAGING_INITIAL_DUMP:-}" ] || [ -n "${STAGING_INITIAL_DUMP_SHA256:-}" ]; }
then
    fail "Upgrade mode forbids bootstrap dump settings. Remove STAGING_INITIAL_DUMP and its checksum."
fi
case "$database_has_migrations" in
    1) ;;
    0)
        [ "$deploy_mode" = "bootstrap" ] \
            || fail "Upgrade mode requires an already initialized Django database."
        public_relation_count=$(compose exec -T db sh -ec '
            export PGPASSWORD="$(cat /run/secrets/postgres_password)"
            psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align \
              --command="select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='"'"'public'"'"' and c.relkind in ('"'"'r'"'"','"'"'p'"'"','"'"'S'"'"','"'"'v'"'"','"'"'m'"'"')"
        ' | tr -d '[:space:]')
        [ "$public_relation_count" = "0" ] \
            || fail "The database has objects but no Django migration history; refusing a partial baseline restore."
        require_value STAGING_INITIAL_DUMP
        require_value STAGING_INITIAL_DUMP_SHA256
        case "$STAGING_INITIAL_DUMP" in
            /*) ;;
            *) fail "STAGING_INITIAL_DUMP must be an absolute path outside the checkout." ;;
        esac
        [ -f "$STAGING_INITIAL_DUMP" ] && [ ! -L "$STAGING_INITIAL_DUMP" ] \
            || fail "STAGING_INITIAL_DUMP must be a non-symlink regular file."
        [ -r "$STAGING_INITIAL_DUMP" ] || fail "STAGING_INITIAL_DUMP is not readable."
        initial_dump_mode=$(stat -c '%a' "$STAGING_INITIAL_DUMP")
        case "$initial_dump_mode" in
            400|600) ;;
            *) fail "STAGING_INITIAL_DUMP must have host mode 0400 or 0600." ;;
        esac
        initial_dump_directory=$(CDPATH= cd -- "$(dirname -- "$STAGING_INITIAL_DUMP")" && pwd)
        initial_dump_path="$initial_dump_directory/$(basename -- "$STAGING_INITIAL_DUMP")"
        case "$initial_dump_path" in
            "$repo_root"|"$repo_root"/*) fail "STAGING_INITIAL_DUMP must remain outside the repository and Docker build context." ;;
        esac
        [ "${#STAGING_INITIAL_DUMP_SHA256}" -eq 64 ] || fail "STAGING_INITIAL_DUMP_SHA256 must be 64 lowercase hex characters."
        case "$STAGING_INITIAL_DUMP_SHA256" in
            *[!0-9a-f]*) fail "STAGING_INITIAL_DUMP_SHA256 must be 64 lowercase hex characters." ;;
        esac
        actual_initial_dump_sha=$(sha256sum "$STAGING_INITIAL_DUMP" | cut -d ' ' -f 1)
        [ "$actual_initial_dump_sha" = "$STAGING_INITIAL_DUMP_SHA256" ] \
            || fail "The initial Staging dump checksum does not match."
        compose exec -T db sh -ec '
            export PGPASSWORD="$(cat /run/secrets/postgres_password)"
            exec pg_restore --exit-on-error --single-transaction --no-owner --no-acl \
              --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"
        ' <"$STAGING_INITIAL_DUMP"
        database_has_migrations=$(compose exec -T db sh -ec '
            export PGPASSWORD="$(cat /run/secrets/postgres_password)"
            psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align \
              --command="select (to_regclass('"'"'public.django_migrations'"'"') is not null)::int"
        ' | tr -d '[:space:]')
        [ "$database_has_migrations" = "1" ] || fail "The restored baseline has no Django migration history."
        ;;
    *) fail "Unable to determine whether the Staging database has a baseline schema." ;;
esac

umask 077
mkdir -p -m 700 "$DEPLOY_BACKUP_DIR"
[ "$(stat -c '%a' "$DEPLOY_BACKUP_DIR")" = "700" ] || fail "DEPLOY_BACKUP_DIR must have host mode 0700."
backup_timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_file="$DEPLOY_BACKUP_DIR/pre-migration-${backup_timestamp}-${release_sha}.dump"

compose exec -T db sh -ec '
    export PGPASSWORD="$(cat /run/secrets/postgres_password)"
    exec pg_dump --format=custom --no-owner --no-acl --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"
' >"$backup_file"
[ -s "$backup_file" ] || fail "The pre-migration database backup is empty."
sha256sum "$backup_file" >"$backup_file.sha256"

# Migrations are an explicit one-off release action; web startup never races it.
compose --profile tools run --rm migrate
compose up -d --no-deps --force-recreate web

web_container=$(compose ps -q web)
[ -n "$web_container" ] || fail "The web container did not start."
health_attempt=1
while [ "$health_attempt" -le 30 ]
do
    health_status=$(docker inspect "$web_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')
    [ "$health_status" = "healthy" ] && break
    [ "$health_status" = "unhealthy" ] && fail "The new web container became unhealthy."
    sleep 2
    health_attempt=$((health_attempt + 1))
done
[ "${health_status:-missing}" = "healthy" ] || fail "The new web container did not become healthy in time."

# The replacement web remains loopback-only. Bootstrap retains the strict zero
# legacy-data gate. Upgrade revalidates the exact live identities/Grants and
# invalidates sessions again before the edge may return.
verify_link_only_content_versions
if [ "$deploy_mode" = "bootstrap" ]
then
    verify_bootstrap_zero_state
else
    verify_exact_staging_staff_and_grants
    invalidate_all_sessions
    echo "Post-migration upgrade data gate: PASS"
fi

echo "Staging $deploy_mode phase deployed on loopback only from immutable SHA: $release_sha"
echo "Pre-migration backup: $backup_file"
echo "Next: retain the data-gate evidence, then run scripts/expose-staging-edge.sh $release_sha $deploy_mode in the approved edge window."
