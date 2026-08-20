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

require_sha256_digest() {
    digest_value="${1:-}"
    case "$digest_value" in
        sha256:*) ;;
        *) fail "STAGING_IMAGE_DIGEST must be sha256:<64 lowercase hex>." ;;
    esac
    digest_hex=${digest_value#sha256:}
    [ "${#digest_hex}" -eq 64 ] || fail "STAGING_IMAGE_DIGEST must be sha256:<64 lowercase hex>."
    case "$digest_hex" in
        *[!0-9a-f]*) fail "STAGING_IMAGE_DIGEST must be sha256:<64 lowercase hex>." ;;
    esac
}

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
compose_file="${COMPOSE_FILE:-$repo_root/deploy/compose.staging.yaml}"
config_file="${STAGING_CONFIG_FILE:-}"
release_sha="${1:-${GIT_COMMIT_SHA:-}}"

if [ -z "$config_file" ] || [ ! -r "$config_file" ]; then
    echo "STAGING_CONFIG_FILE must name a readable non-secret Compose environment file." >&2
    exit 1
fi
# Parse, never source, the operator configuration. Certificate hooks are often
# launched by a privileged manager; treating dotenv text as shell code would
# turn a writable configuration file into root command execution.
COMPOSE_PROJECT_NAME=
DEPLOYMENT_STAGE=
STAGING_DEPLOY_MODE=
STAGING_HOSTNAME=
STAGING_IMAGE_REPOSITORY=
STAGING_IMAGE_DIGEST=
STAGING_OWNER_USERNAME=
STAGING_ADMIN_USERNAME=
STAGING_OPERATOR_USERNAME=
STAGING_PRODUCT_CODE=
STAGING_PUBLISH_ACCOUNT_CODE=
GROWTHOS_UID=
GROWTHOS_GID=
POSTGRES_UID=
POSTGRES_GID=
POSTGRES_IMAGE=
NGINX_IMAGE=
POSTGRES_DB=
POSTGRES_USER=
SECRETS_DIR=
DEPLOY_BACKUP_DIR=
MEDIA_STORAGE_BACKEND=
TENCENT_COS_BUCKET=
TENCENT_COS_REGION=
STAGING_INITIAL_DUMP=
STAGING_INITIAL_DUMP_SHA256=
seen_config_keys=" "
carriage_return=$(printf '\r')
while IFS= read -r config_line || [ -n "$config_line" ]
do
    case "$config_line" in
        ""|\#*) continue ;;
        *"$carriage_return"*) fail "STAGING_CONFIG_FILE must use Unix line endings." ;;
        *=*) ;;
        *) fail "STAGING_CONFIG_FILE contains a non-dotenv line." ;;
    esac
    config_key=${config_line%%=*}
    config_value=${config_line#*=}
    [ -n "$config_key" ] && [ -n "$config_value" ] \
        || fail "STAGING_CONFIG_FILE contains an empty key or value."
    case "$config_value" in
        *[!A-Za-z0-9_./:@+-]*) fail "STAGING_CONFIG_FILE contains quoting, whitespace, or command syntax." ;;
    esac
    case " $seen_config_keys " in
        *" $config_key "*) fail "STAGING_CONFIG_FILE contains a duplicate key: $config_key" ;;
    esac
    seen_config_keys="$seen_config_keys$config_key "
    case "$config_key" in
        COMPOSE_PROJECT_NAME) COMPOSE_PROJECT_NAME=$config_value ;;
        DEPLOYMENT_STAGE) DEPLOYMENT_STAGE=$config_value ;;
        STAGING_DEPLOY_MODE) STAGING_DEPLOY_MODE=$config_value ;;
        STAGING_HOSTNAME) STAGING_HOSTNAME=$config_value ;;
        STAGING_IMAGE_REPOSITORY) STAGING_IMAGE_REPOSITORY=$config_value ;;
        STAGING_IMAGE_DIGEST) STAGING_IMAGE_DIGEST=$config_value ;;
        STAGING_OWNER_USERNAME) STAGING_OWNER_USERNAME=$config_value ;;
        STAGING_ADMIN_USERNAME) STAGING_ADMIN_USERNAME=$config_value ;;
        STAGING_OPERATOR_USERNAME) STAGING_OPERATOR_USERNAME=$config_value ;;
        STAGING_PRODUCT_CODE) STAGING_PRODUCT_CODE=$config_value ;;
        STAGING_PUBLISH_ACCOUNT_CODE) STAGING_PUBLISH_ACCOUNT_CODE=$config_value ;;
        GROWTHOS_UID) GROWTHOS_UID=$config_value ;;
        GROWTHOS_GID) GROWTHOS_GID=$config_value ;;
        POSTGRES_UID) POSTGRES_UID=$config_value ;;
        POSTGRES_GID) POSTGRES_GID=$config_value ;;
        POSTGRES_IMAGE) POSTGRES_IMAGE=$config_value ;;
        NGINX_IMAGE) NGINX_IMAGE=$config_value ;;
        POSTGRES_DB) POSTGRES_DB=$config_value ;;
        POSTGRES_USER) POSTGRES_USER=$config_value ;;
        SECRETS_DIR) SECRETS_DIR=$config_value ;;
        DEPLOY_BACKUP_DIR) DEPLOY_BACKUP_DIR=$config_value ;;
        MEDIA_STORAGE_BACKEND) MEDIA_STORAGE_BACKEND=$config_value ;;
        TENCENT_COS_BUCKET) TENCENT_COS_BUCKET=$config_value ;;
        TENCENT_COS_REGION) TENCENT_COS_REGION=$config_value ;;
        STAGING_INITIAL_DUMP) STAGING_INITIAL_DUMP=$config_value ;;
        STAGING_INITIAL_DUMP_SHA256) STAGING_INITIAL_DUMP_SHA256=$config_value ;;
        *) fail "STAGING_CONFIG_FILE contains an unknown key: $config_key" ;;
    esac
done <"$config_file"
unset config_line config_key config_value seen_config_keys carriage_return

for required_value in COMPOSE_PROJECT_NAME DEPLOYMENT_STAGE STAGING_DEPLOY_MODE \
    STAGING_HOSTNAME STAGING_IMAGE_REPOSITORY \
    STAGING_IMAGE_DIGEST STAGING_OWNER_USERNAME STAGING_ADMIN_USERNAME \
    STAGING_OPERATOR_USERNAME STAGING_PRODUCT_CODE STAGING_PUBLISH_ACCOUNT_CODE \
    GROWTHOS_UID GROWTHOS_GID POSTGRES_UID POSTGRES_GID \
    POSTGRES_IMAGE NGINX_IMAGE POSTGRES_DB POSTGRES_USER \
    SECRETS_DIR DEPLOY_BACKUP_DIR MEDIA_STORAGE_BACKEND TENCENT_COS_BUCKET \
    TENCENT_COS_REGION
do
    eval "parsed_value=\${${required_value}:-}"
    [ -n "$parsed_value" ] || fail "STAGING_CONFIG_FILE is missing $required_value."
done
unset parsed_value required_value
export COMPOSE_PROJECT_NAME DEPLOYMENT_STAGE STAGING_DEPLOY_MODE \
    STAGING_HOSTNAME STAGING_IMAGE_REPOSITORY \
    STAGING_IMAGE_DIGEST STAGING_OWNER_USERNAME STAGING_ADMIN_USERNAME \
    STAGING_OPERATOR_USERNAME STAGING_PRODUCT_CODE STAGING_PUBLISH_ACCOUNT_CODE \
    GROWTHOS_UID GROWTHOS_GID POSTGRES_UID POSTGRES_GID \
    POSTGRES_IMAGE NGINX_IMAGE POSTGRES_DB POSTGRES_USER \
    SECRETS_DIR DEPLOY_BACKUP_DIR MEDIA_STORAGE_BACKEND TENCENT_COS_BUCKET \
    TENCENT_COS_REGION STAGING_INITIAL_DUMP STAGING_INITIAL_DUMP_SHA256

secret_dir=$SECRETS_DIR
staging_hostname=$STAGING_HOSTNAME
[ "$DEPLOYMENT_STAGE" = "staging" ] || fail "DEPLOYMENT_STAGE must be exactly staging."
case "$STAGING_DEPLOY_MODE" in
    bootstrap|upgrade) ;;
    *) fail "STAGING_DEPLOY_MODE must be bootstrap or upgrade." ;;
esac
if [ -z "$secret_dir" ] || [ -z "$staging_hostname" ]; then
    echo "SECRETS_DIR and STAGING_HOSTNAME must be present in STAGING_CONFIG_FILE." >&2
    exit 1
fi
require_digest_image POSTGRES_IMAGE
require_digest_image NGINX_IMAGE
require_sha256_digest "$STAGING_IMAGE_DIGEST"
case "$STAGING_IMAGE_REPOSITORY" in
    *@*) fail "STAGING_IMAGE_REPOSITORY must be a repository without a tag or digest." ;;
esac
image_repository_name=${STAGING_IMAGE_REPOSITORY##*/}
case "$image_repository_name" in
    *:*) fail "STAGING_IMAGE_REPOSITORY must omit a mutable tag." ;;
esac
case "$staging_hostname" in
    *[!a-z0-9.-]*|.*|*.)
        echo "STAGING_HOSTNAME must be a lowercase bare DNS hostname." >&2
        exit 1
        ;;
esac
[ "${#release_sha}" -eq 40 ] || {
    echo "Pass the exact currently deployed 40-character Git SHA to the renewal hook." >&2
    exit 1
}
case "$release_sha" in
    *[!0-9a-f]*)
        echo "The deployed Git SHA must be lowercase hexadecimal." >&2
        exit 1
        ;;
esac
export GIT_COMMIT_SHA="$release_sha"

certificate="$secret_dir/tls_fullchain.pem"
private_key="$secret_dir/tls_privkey.pem"
if [ ! -d "$secret_dir" ] || [ -L "$secret_dir" ]; then
    echo "SECRETS_DIR must be a real protected directory." >&2
    exit 1
fi
if [ "$(stat -c '%a' "$secret_dir")" != "700" ]; then
    echo "SECRETS_DIR must retain host mode 0700 after renewal." >&2
    exit 1
fi
if [ ! -f "$certificate" ] || [ -L "$certificate" ] \
    || [ ! -f "$private_key" ] || [ -L "$private_key" ]; then
    echo "The renewed certificate and private key must be non-symlink regular files." >&2
    exit 1
fi
private_key_mode=$(stat -c '%a' "$private_key")
case "$private_key_mode" in
    400|600) ;;
    *)
        echo "The renewed TLS private key must retain host mode 0400 or 0600." >&2
        exit 1
        ;;
esac
if [ ! -r "$certificate" ] || [ ! -r "$private_key" ]; then
    echo "The renewed certificate or private key is unavailable." >&2
    exit 1
fi

openssl x509 -checkend 86400 -noout -in "$certificate" >/dev/null
openssl x509 -checkhost "$staging_hostname" -noout -in "$certificate" >/dev/null
certificate_public_key=$(openssl x509 -in "$certificate" -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d ' ' -f 1)
private_public_key=$(openssl pkey -in "$private_key" -pubout -outform DER 2>/dev/null | sha256sum | cut -d ' ' -f 1)
if [ -z "$certificate_public_key" ] || [ "$certificate_public_key" != "$private_public_key" ]; then
    echo "The renewed certificate and private key do not match." >&2
    exit 1
fi
unset certificate_public_key private_public_key

# A certificate manager normally replaces files atomically. Recreate the Nginx
# container so Docker remounts the new Secret-file inodes; a signal-only reload
# could otherwise keep an earlier bind-mounted inode. Validate in a one-off
# container before replacing the live proxy.
docker compose --env-file "$config_file" -f "$compose_file" run --rm --no-deps nginx nginx -t
docker compose --env-file "$config_file" -f "$compose_file" up -d --no-deps --force-recreate nginx
docker compose --env-file "$config_file" -f "$compose_file" exec -T nginx nginx -t
echo "The renewed certificate was validated and Nginx was safely recreated."
