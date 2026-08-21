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
