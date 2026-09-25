#!/usr/bin/env bash
#
# Import OpenBao init output (PGP-encrypted root token and unseal keys)
# into pass under selfhosted/openbao/<cluster>/.
#
# If an entry already exists, you are asked whether to overwrite it:
#   y = overwrite this one, n = skip this one (default),
#   a = overwrite this and all remaining, q = quit
#
set -euo pipefail

usage() {
    echo "Usage: $(basename "$0") <init-output.json> <cluster>" >&2
    exit 1
}

die() {
    echo "Error: $*" >&2
    exit 1
}

[ $# -eq 2 ] || usage
FILE=$1
CLUSTER=$2

[ -f "$FILE" ] || die "file '$FILE' not found"
[ -n "$CLUSTER" ] || die "cluster name must not be empty"

for cmd in jq base64 gpg pass; do
    command -v "$cmd" >/dev/null 2>&1 || die "required command '$cmd' not found"
done

PASS_PREFIX="selfhosted/openbao/${CLUSTER}"
STORE_DIR="${PASSWORD_STORE_DIR:-$HOME/.password-store}"

OVERWRITE_ALL=false
stored=0
skipped=0

# entry_exists <pass entry name>
# Checks the store on disk, so no decryption or passphrase prompt is needed.
entry_exists() {
    [ -f "${STORE_DIR}/${PASS_PREFIX}/$1.gpg" ]
}

# confirm_overwrite <pass entry name>
# Returns 0 to overwrite, 1 to skip. Reads from the terminal directly,
# so it works even when stdin is redirected.
confirm_overwrite() {
    local name=$1 answer
    "$OVERWRITE_ALL" && return 0

    [ -r /dev/tty ] || die "${PASS_PREFIX}/${name} exists and no terminal is available to confirm overwrite"

    while true; do
        read -r -p "${PASS_PREFIX}/${name} already exists. Overwrite? [y/N/a/q] " answer </dev/tty
        case "$answer" in
            [yY] | [yY][eE][sS]) return 0 ;;
            [nN] | [nN][oO] | "") return 1 ;;
            [aA]) OVERWRITE_ALL=true; return 0 ;;
            [qQ]) echo "Aborted. Stored ${stored}, skipped ${skipped}." >&2; exit 1 ;;
            *) echo "Please answer y (yes), n (no), a (all) or q (quit)." >&2 ;;
        esac
    done
}

# store_secret <base64 gpg-encrypted value> <pass entry name>
# Asks before overwriting, and decrypts fully before writing so a failed
# decrypt never leaves an empty or partial entry in pass.
store_secret() {
    local encrypted=$1 name=$2 value

    if entry_exists "$name" && ! confirm_overwrite "$name"; then
        echo "Skipped ${PASS_PREFIX}/${name}"
        skipped=$((skipped + 1))
        return 0
    fi

    value=$(printf '%s' "$encrypted" | base64 -d | gpg --quiet --decrypt) \
        || die "failed to decrypt ${name}"
    [ -n "$value" ] || die "decrypted value for ${name} is empty"
    printf '%s\n' "$value" | pass insert --echo --force "${PASS_PREFIX}/${name}" >/dev/null
    echo "Stored ${PASS_PREFIX}/${name}"
    stored=$((stored + 1))
}

root_token=$(jq -er '.root_token' "$FILE") \
    || die "no root_token found in $FILE"

count=$(jq -er '.unseal_keys_b64 | arrays | length' "$FILE") \
    || die "no unseal_keys_b64 array found in $FILE"
[ "$count" -gt 0 ] || die "unseal_keys_b64 is empty in $FILE"

store_secret "$root_token" "root-token"

for ((i = 0; i < count; i++)); do
    key=$(jq -er --argjson i "$i" '.unseal_keys_b64[$i]' "$FILE") \
        || die "unseal key at index $i is missing"
    store_secret "$key" "unseal-key-$((i + 1))"
done

echo "Done for cluster '${CLUSTER}': stored ${stored}, skipped ${skipped}."