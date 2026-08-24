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
[ -n "$config_file" ] && [ -r "$config_file" ] \
    || fail "STAGING_CONFIG_FILE must name a readable, non-secret Compose environment file."

set -a
# shellcheck disable=SC1090
. "$config_file"
set +a

[ -n "${STAGING_HOSTNAME:-}" ] || fail "STAGING_HOSTNAME is required."
[ "${DEPLOYMENT_STAGE:-}" = "staging" ] || fail "DEPLOYMENT_STAGE must be exactly staging."
[ "${STAGING_DEPLOY_MODE:-}" = "$deploy_mode" ] \
    || fail "CLI deployment mode does not match STAGING_DEPLOY_MODE in the reviewed config."
[ -n "${STAGING_IMAGE_REPOSITORY:-}" ] || fail "STAGING_IMAGE_REPOSITORY is required."
[ -n "${STAGING_IMAGE_DIGEST:-}" ] || fail "STAGING_IMAGE_DIGEST is required."
for required_name in STAGING_OWNER_USERNAME STAGING_ADMIN_USERNAME \
    STAGING_OPERATOR_USERNAME STAGING_PRODUCT_CODE STAGING_PUBLISH_ACCOUNT_CODE
do
    eval "required_value=\${${required_name}:-}"
    [ -n "$required_value" ] || fail "$required_name is required."
done
unset required_name required_value
require_digest_image POSTGRES_IMAGE
require_digest_image NGINX_IMAGE
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
case "$STAGING_HOSTNAME" in
    *[!a-z0-9.-]*|.*|*.) fail "STAGING_HOSTNAME must be a lowercase bare DNS hostname." ;;
esac

cd "$repo_root"
[ "$(git rev-parse HEAD)" = "$release_sha" ] || fail "HEAD is not the requested release SHA."
[ -z "$(git status --porcelain)" ] || fail "The checkout is not clean."
export GIT_COMMIT_SHA="$release_sha"

compose() {
    docker compose --env-file "$config_file" -f "$compose_file" "$@"
}

compose config --quiet
command -v ss >/dev/null 2>&1 || fail "The iproute2 ss command is required for edge cutover."
[ -z "$(compose ps -q nginx 2>/dev/null || true)" ] \
    || fail "Candidate Nginx is already running; use the verifier instead of repeating the exposure action."

web_container=$(compose ps -q web)
[ -n "$web_container" ] || fail "The loopback-only web candidate is not running."
expected_image="${STAGING_IMAGE_REPOSITORY}@${STAGING_IMAGE_DIGEST}"
running_image_id=$(docker inspect "$web_container" --format '{{.Image}}')
expected_image_id=$(docker image inspect "$expected_image" --format '{{.Id}}')
[ "$running_image_id" = "$expected_image_id" ] || fail "The running web container is not the approved digest."
image_revision=$(docker image inspect "$expected_image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
[ "$image_revision" = "$release_sha" ] || fail "The approved image label does not match the release SHA."
[ "$(docker port "$web_container" 8000/tcp 2>/dev/null || true)" = "127.0.0.1:18000" ] \
    || fail "The candidate web service is not restricted to 127.0.0.1:18000."
[ "$(docker inspect "$web_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')" = "healthy" ] \
    || fail "The loopback-only web candidate is not healthy."

# Phase one required zero restored human identities. Provisioning then creates
# exactly three bounded Staging identities. Repeat the fail-closed data gate
# immediately before public exposure and accept no fourth active human.
compose exec -T web python -c '
import sys

from django.contrib.sessions.models import Session
from django.db.models import Q

from accounts.models import Principal

expected = {
    sys.argv[1]: Principal.Role.OWNER,
    sys.argv[2]: Principal.Role.OPERATIONS_ADMIN,
    sys.argv[3]: Principal.Role.OPERATOR,
}
active_humans = list(
    Principal.objects.filter(
        principal_type=Principal.PrincipalType.HUMAN_USER,
        is_active=True,
    ).order_by("username")
)
problems = []
if len(active_humans) != 3 or {principal.username for principal in active_humans} != set(expected):
    problems.append("active human set is not the exact approved three usernames")
for principal in active_humans:
    if (
        principal.role != expected.get(principal.username)
        or principal.principal_status != Principal.PrincipalStatus.ACTIVE
        or principal.auth_provider != "internal"
        or principal.is_staff
        or principal.is_superuser
        or not principal.has_usable_password()
    ):
        problems.append(f"identity attributes are invalid for {principal.username}")
if Principal.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).exists():
    problems.append("a staff or superuser Principal exists")
if Session.objects.exists():
    problems.append("a live Django session exists")
if problems:
    raise SystemExit("PRE_EDGE_IDENTITY_OR_SESSION_GATE_FAILED: " + "; ".join(problems))
print("Pre-edge exact-three identity/session gate: PASS")
' "$STAGING_OWNER_USERNAME" "$STAGING_ADMIN_USERNAME" "$STAGING_OPERATOR_USERNAME"

# Reuse the command's transactionally locked exact-grant verifier. Because all
# three Principals already exist, no password file is read and this remains a
# dry run with zero committed writes.
compose run --rm --no-deps --entrypoint python web \
    manage.py provision_staging_staff \
    --owner-username "$STAGING_OWNER_USERNAME" \
    --admin-username "$STAGING_ADMIN_USERNAME" \
    --operator-username "$STAGING_OPERATOR_USERNAME" \
    --product-code "$STAGING_PRODUCT_CODE" \
    --publish-account-code "$STAGING_PUBLISH_ACCOUNT_CODE"

# The provisioning dry run is permitted to model creation of a missing
# high-risk PUBLISH grant and then roll it back. Prove a current exact grant was
# already committed before exposing the edge.
compose exec -T web python -c '
import sys
from django.db.models import Q
from django.utils import timezone

from accounts.models import PermissionGrant, Principal

operator = Principal.objects.get(username=sys.argv[1])
now = timezone.now()
grants = list(
    PermissionGrant.objects.filter(
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
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
)
if len(grants) != 1 or grants[0].valid_until is None:
    raise SystemExit("PRE_EDGE_EXACT_BOUNDED_PUBLISH_GRANT_MISSING")
print("Pre-edge exact bounded Operator PUBLISH grant: PASS")
' "$STAGING_OPERATOR_USERNAME" "$STAGING_PUBLISH_ACCOUNT_CODE"

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

compose pull nginx
candidate_nginx_configuration=$(compose run --rm --no-deps nginx nginx -T 2>/dev/null)
candidate_server_block_count=$(printf '%s\n' "$candidate_nginx_configuration" | grep -Ec '^[[:space:]]*server[[:space:]]*\{')
[ "$candidate_server_block_count" = "2" ] || fail "The candidate Nginx image rendered an unexpected additional virtual host."
printf '%s\n' "$candidate_nginx_configuration" | grep -Fq "server_name ${STAGING_HOSTNAME};" \
    || fail "The candidate Nginx template did not render STAGING_HOSTNAME."
if printf '%s\n' "$candidate_nginx_configuration" | grep -Fq '${STAGING_HOSTNAME}'
then
    fail "The candidate Nginx configuration still contains an unrendered hostname placeholder."
fi
unset candidate_nginx_configuration

occupied_edge_ports=$(ss -H -ltn | awk '{print $4}' | grep -E ':(80|443)$' || true)
[ -z "$occupied_edge_ports" ] \
    || fail "Host ports 80/443 are still occupied. Stop the retained old edge in the approved cutover window, then retry."

compose up -d --no-deps --force-recreate nginx
compose exec -T nginx nginx -t
unexpected_host_result=$(curl --silent --show-error --output /dev/null --max-time 20 \
    --header 'Host: localhost' --write-out '%{http_code} %{redirect_url}' "http://127.0.0.1/")
[ "$unexpected_host_result" = "308 https://${STAGING_HOSTNAME}/" ] \
    || fail "The new default HTTP virtual host did not canonicalize Host: localhost exactly."

echo "Staging $deploy_mode edge exposed for approved immutable SHA: $release_sha"
echo "Next: run scripts/verify-staging.sh $release_sha immediately."
