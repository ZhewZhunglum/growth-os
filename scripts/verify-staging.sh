#!/bin/sh
set -eu

fail() {
    echo "ERROR: $*" >&2
    exit 1
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

release_sha="${1:-}"
[ "${#release_sha}" -eq 40 ] || fail "Usage: $0 <exact-40-character-git-sha>"
case "$release_sha" in
    *[!0-9a-f]*) fail "The release SHA must contain lowercase hexadecimal characters only." ;;
esac

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="${COMPOSE_FILE:-$repo_root/deploy/compose.staging.yaml}"
config_file="${STAGING_CONFIG_FILE:-}"
[ -n "$config_file" ] && [ -r "$config_file" ] || fail "STAGING_CONFIG_FILE must name a readable, non-secret Compose environment file."

set -a
# shellcheck disable=SC1090
. "$config_file"
set +a

[ -n "${STAGING_HOSTNAME:-}" ] || fail "STAGING_HOSTNAME is required."
[ "${DEPLOYMENT_STAGE:-}" = "staging" ] || fail "DEPLOYMENT_STAGE must be exactly staging."
case "${STAGING_DEPLOY_MODE:-}" in
    bootstrap|upgrade) ;;
    *) fail "STAGING_DEPLOY_MODE must be bootstrap or upgrade." ;;
esac
[ -n "${STAGING_IMAGE_REPOSITORY:-}" ] || fail "STAGING_IMAGE_REPOSITORY is required."
[ -n "${STAGING_IMAGE_DIGEST:-}" ] || fail "STAGING_IMAGE_DIGEST is required."
[ -n "${POSTGRES_IMAGE:-}" ] || fail "POSTGRES_IMAGE is required."
[ -n "${NGINX_IMAGE:-}" ] || fail "NGINX_IMAGE is required."
for required_name in STAGING_OWNER_USERNAME STAGING_ADMIN_USERNAME \
    STAGING_OPERATOR_USERNAME STAGING_PRODUCT_CODE STAGING_PUBLISH_ACCOUNT_CODE
do
    eval "required_value=\${${required_name}:-}"
    [ -n "$required_value" ] || fail "$required_name is required."
done
unset required_name required_value
require_digest_image POSTGRES_IMAGE
require_digest_image NGINX_IMAGE
case "$STAGING_HOSTNAME" in
    *[!a-z0-9.-]*|.*|*.) fail "STAGING_HOSTNAME must be a lowercase bare DNS hostname." ;;
esac
case "$STAGING_IMAGE_DIGEST" in
    sha256:*) ;;
    *) fail "STAGING_IMAGE_DIGEST must be sha256:<64 lowercase hex>." ;;
esac
application_digest_hex=${STAGING_IMAGE_DIGEST#sha256:}
[ "${#application_digest_hex}" -eq 64 ] || fail "STAGING_IMAGE_DIGEST must be sha256:<64 lowercase hex>."
case "$application_digest_hex" in
    *[!0-9a-f]*) fail "STAGING_IMAGE_DIGEST must be sha256:<64 lowercase hex>." ;;
esac
case "$STAGING_IMAGE_REPOSITORY" in
    *@*) fail "STAGING_IMAGE_REPOSITORY must omit tags and digests." ;;
esac
image_repository_name=${STAGING_IMAGE_REPOSITORY##*/}
case "$image_repository_name" in
    *:*) fail "STAGING_IMAGE_REPOSITORY must omit a mutable tag." ;;
esac

export GIT_COMMIT_SHA="$release_sha"
compose() {
    docker compose --env-file "$config_file" -f "$compose_file" "$@"
}

compose config --quiet
web_container=$(compose ps -q web)
nginx_container=$(compose ps -q nginx)
db_container=$(compose ps -q db)
[ -n "$web_container" ] || fail "The web container is not running."
[ -n "$nginx_container" ] || fail "The Nginx container is not running."
[ -n "$db_container" ] || fail "The PostgreSQL container is not running."

expected_image="${STAGING_IMAGE_REPOSITORY}@${STAGING_IMAGE_DIGEST}"
running_image_id=$(docker inspect "$web_container" --format '{{.Image}}')
expected_image_id=$(docker image inspect "$expected_image" --format '{{.Id}}')
[ "$running_image_id" = "$expected_image_id" ] || fail "The running web container is not using the expected immutable image."
expected_repo_digests=$(docker image inspect "$expected_image" --format '{{range .RepoDigests}}{{println .}}{{end}}')
printf '%s\n' "$expected_repo_digests" | grep -Fq "@${STAGING_IMAGE_DIGEST}" \
    || fail "The running application image does not expose the approved repository digest."
image_revision=$(docker image inspect "$expected_image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
[ "$image_revision" = "$release_sha" ] || fail "The OCI revision label does not match the expected SHA."
image_created=$(docker image inspect "$expected_image" --format '{{.Created}}')

running_db_image_id=$(docker inspect "$db_container" --format '{{.Image}}')
expected_db_image_id=$(docker image inspect "$POSTGRES_IMAGE" --format '{{.Id}}')
[ "$running_db_image_id" = "$expected_db_image_id" ] || fail "PostgreSQL is not using the configured immutable image digest."
running_nginx_image_id=$(docker inspect "$nginx_container" --format '{{.Image}}')
expected_nginx_image_id=$(docker image inspect "$NGINX_IMAGE" --format '{{.Id}}')
[ "$running_nginx_image_id" = "$expected_nginx_image_id" ] || fail "Nginx is not using the configured immutable image digest."

published_port=$(docker port "$web_container" 8000/tcp 2>/dev/null || true)
[ "$published_port" = "127.0.0.1:18000" ] || fail "The application listener is not restricted to 127.0.0.1:18000."
db_published_ports=$(docker port "$db_container" 2>/dev/null || true)
[ -z "$db_published_ports" ] || fail "PostgreSQL unexpectedly has a host-published port."

for secret_destination in /run/secrets/django_secret_key /run/secrets/postgres_password
do
    secret_writable=$(docker inspect "$web_container" --format "{{range .Mounts}}{{if eq .Destination \"$secret_destination\"}}{{.RW}}{{end}}{{end}}")
    [ "$secret_writable" = "false" ] || fail "Secret mount is absent or writable: $secret_destination"
done
for secret_destination in /run/secrets/tls_fullchain.pem /run/secrets/tls_privkey.pem
do
    secret_writable=$(docker inspect "$nginx_container" --format "{{range .Mounts}}{{if eq .Destination \"$secret_destination\"}}{{.RW}}{{end}}{{end}}")
    [ "$secret_writable" = "false" ] || fail "TLS Secret mount is absent or writable: $secret_destination"
done

container_environment=$(docker inspect "$web_container" --format '{{range .Config.Env}}{{println .}}{{end}}')
printf '%s\n' "$container_environment" | grep -q '^DJANGO_SECRET_KEY=' && fail "DJANGO_SECRET_KEY was embedded in the container configuration."
printf '%s\n' "$container_environment" | grep -q '^POSTGRES_PASSWORD=' && fail "POSTGRES_PASSWORD was embedded in the container configuration."

compose exec -T web python -c '
from django.conf import settings
from django.db import connection
assert settings.ENVIRONMENT == "staging"
assert settings.PASSWORD_MIN_LENGTH >= 12
assert connection.vendor == "postgresql"
with connection.cursor() as cursor:
    cursor.execute("select current_setting(%s)", ["server_version"])
    version = cursor.fetchone()[0]
print(f"PostgreSQL server version: {version}")
'
compose exec -T web python manage.py migrate --check
compose exec -T web python manage.py check
compose exec -T web python manage.py check --deploy

# Verify the complete exact base Grant plan without committing any writes.
compose exec -T web python manage.py provision_staging_staff \
    --owner-username "$STAGING_OWNER_USERNAME" \
    --admin-username "$STAGING_ADMIN_USERNAME" \
    --operator-username "$STAGING_OPERATOR_USERNAME" \
    --product-code "$STAGING_PRODUCT_CODE" \
    --publish-account-code "$STAGING_PUBLISH_ACCOUNT_CODE"

# The rollback-only dry run can model missing publish authority. Independently
# prove all three current exact Grants are already committed, bounded and
# granted by the Owner before accepting the running candidate.
compose exec -T web python -c '
import sys
from datetime import timedelta
from django.db.models import Q
from django.utils import timezone

from accounts.models import PermissionGrant, Principal

usernames = sys.argv[1:4]
account_ref = sys.argv[4]
principals = {
    principal.username: principal
    for principal in Principal.objects.filter(username__in=usernames)
}
if set(principals) != set(usernames):
    raise SystemExit("VERIFY_PUBLISH_PRINCIPAL_SET_NOT_EXACT")
owner = principals[usernames[0]]
now = timezone.now()
grants = list(PermissionGrant.objects.filter(
    principal__in=principals.values(),
    scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
    product__isnull=True,
    platform_code="",
    account_ref=account_ref,
    surface_ref="",
    action=PermissionGrant.Action.PUBLISH,
    effect=PermissionGrant.Effect.ALLOW,
    risk_level=PermissionGrant.RiskLevel.HIGH,
    grant_status=PermissionGrant.GrantStatus.ACTIVE,
    valid_from__lte=now,
).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now)))
for username in usernames:
    exact = [grant for grant in grants if grant.principal_id == principals[username].pk]
    if len(exact) != 1:
        raise SystemExit(f"VERIFY_EXACT_BOUNDED_PUBLISH_GRANT_MISSING:{username}")
    grant = exact[0]
    if (
        grant.valid_until is None
        or grant.valid_until - grant.valid_from > timedelta(days=31)
        or grant.granted_by_principal_id != owner.pk
    ):
        raise SystemExit(f"VERIFY_PUBLISH_GRANT_NOT_OWNER_GRANTED_OR_BOUNDED:{username}")
print("Running exact bounded Owner/Admin/Operator PUBLISH Grants: PASS")
' "$STAGING_OWNER_USERNAME" "$STAGING_ADMIN_USERNAME" "$STAGING_OPERATOR_USERNAME" \
    "$STAGING_PUBLISH_ACCOUNT_CODE"

compose exec -T web python -c '
import hashlib
from urllib.parse import urlsplit

from contentops.models import ContentAssetVersion

invalid_count = 0
sample_ids = []
total = 0
legacy_count = 0
external_url_count = 0
inline_text_count = 0
for version in ContentAssetVersion.objects.only(
    "id", "payload_schema_version", "representation_kind", "object_key",
    "inline_content", "byte_size", "content_sha256"
).iterator():
    total += 1
    valid = False
    if version.payload_schema_version == ContentAssetVersion.PayloadSchemaVersion.V1:
        # Historical V1 rows keep their original object-key payload and hashes.
        # They are compatibility-only; current code can create V2 rows only.
        legacy_count += 1
        valid = (
            version.representation_kind == ContentAssetVersion.RepresentationKind.EXTERNAL_URL
            and bool(version.object_key)
            and not version.inline_content
        )
    elif version.payload_schema_version == ContentAssetVersion.PayloadSchemaVersion.V2:
        if version.representation_kind == ContentAssetVersion.RepresentationKind.EXTERNAL_URL:
            parsed = urlsplit(version.object_key)
            external_url_count += 1
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.netloc)
                and not parsed.username
                and not parsed.password
                and not version.inline_content
            )
        elif version.representation_kind == ContentAssetVersion.RepresentationKind.INLINE_TEXT:
            encoded = version.inline_content.encode("utf-8")
            inline_text_count += 1
            valid = (
                not version.object_key
                and bool(version.inline_content.strip())
                and version.byte_size == len(encoded)
                and version.content_sha256 == hashlib.sha256(encoded).hexdigest()
            )
    if not valid:
        invalid_count += 1
        if len(sample_ids) < 5:
            sample_ids.append(str(version.pk))
if invalid_count:
    rendered_ids = ",".join(sample_ids)
    raise SystemExit(
        "CONTENT_REPRESENTATION_GATE_FAILED: "
        f"invalid_count={invalid_count}; sample_ids={rendered_ids}"
    )
print(
    "ContentAssetVersion representation gate: PASS "
    f"(total={total}, legacy_v1={legacy_count}, external_url_v2={external_url_count}, "
    f"inline_text_v2={inline_text_count})"
)
'

compose exec -T nginx nginx -t

nginx_configuration=$(compose exec -T nginx nginx -T 2>/dev/null)
printf '%s\n' "$nginx_configuration" | grep -Fq 'limit_req zone=login_per_ip burst=5 nodelay;' || fail "Login rate limiting is absent."
printf '%s\n' "$nginx_configuration" | grep -Fq 'proxy_set_header Host $server_name;' || fail "The upstream Host header is not overwritten."
printf '%s\n' "$nginx_configuration" | grep -Fq 'proxy_set_header X-Forwarded-For $remote_addr;' || fail "Forwarding headers are not overwritten at the edge."
server_block_count=$(printf '%s\n' "$nginx_configuration" | grep -Ec '^[[:space:]]*server[[:space:]]*\{')
[ "$server_block_count" = "2" ] || fail "Nginx has an unexpected additional virtual host."

health_json=$(curl --fail --silent --show-error --max-time 20 --proto '=https' --tlsv1.2 "https://${STAGING_HOSTNAME}/health/")
printf '%s' "$health_json" | compose exec -T web python -c '
import json, sys
payload = json.load(sys.stdin)
expected = sys.argv[1]
assert payload.get("status") == "ok", payload
assert payload.get("database") == "ok", payload
deployment = payload.get("deployment", {})
assert deployment.get("stage") == "staging-candidate", deployment
assert deployment.get("revision") == expected, deployment
' "$release_sha"

redirect_result=$(curl --silent --show-error --output /dev/null --max-time 20 --write-out '%{http_code} %{redirect_url}' "http://${STAGING_HOSTNAME}/health/")
[ "$redirect_result" = "308 https://${STAGING_HOSTNAME}/health/" ] || fail "Port 80 does not redirect exactly to the Staging HTTPS endpoint."

unexpected_host_result=$(curl --silent --show-error --output /dev/null --max-time 20 --header 'Host: localhost' --write-out '%{http_code} %{redirect_url}' "http://127.0.0.1/")
[ "$unexpected_host_result" = "308 https://${STAGING_HOSTNAME}/" ] || fail "The default HTTP virtual host did not canonicalize Host: localhost exactly."

security_headers=$(curl --fail --silent --show-error --head --max-time 20 "https://${STAGING_HOSTNAME}/accounts/login/")
printf '%s\n' "$security_headers" | tr -d '\r' | grep -iq '^x-content-type-options: nosniff$' || fail "The live nosniff header is absent."
printf '%s\n' "$security_headers" | tr -d '\r' | grep -iq '^x-frame-options: DENY$' || fail "The live frame-denial header is absent."
printf '%s\n' "$security_headers" | tr -d '\r' | grep -iq '^referrer-policy: same-origin$' || fail "The live referrer policy is absent."
printf '%s\n' "$security_headers" | tr -d '\r' | grep -iq '^permissions-policy:' || fail "The live permissions policy is absent."

echo "Application image ID: $expected_image_id"
echo "Application image created: $image_created"
echo "Staging deployment verification passed for immutable SHA: $release_sha"
