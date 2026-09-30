#!/usr/bin/env bash
#
# Unseal an OpenBao cluster with the unseal keys stored in pass under
# selfhosted/openbao/<cluster>/unseal-key-N (see the import script).
#
# Usage: openbao-unseal.sh [node-address ...]
#   Every node has to be unsealed separately. Without arguments, $BAO_ADDR
#   is used (default https://127.0.0.1:8200). Set BAO_CACERT for a custom CA.
#
set -euo pipefail

die() {
    echo "Error: $*" >&2
    exit 1
}

for cmd in curl jq pass; do
    command -v "$cmd" >/dev/null 2>&1 || die "required command '$cmd' not found"
done

STORE_DIR="${PASSWORD_STORE_DIR:-$HOME/.password-store}"

read -r -p "Cluster name: " CLUSTER
[ -n "$CLUSTER" ] || die "cluster name must not be empty"
case "$CLUSTER" in */*) die "cluster name must not contain '/'" ;; esac

PASS_PREFIX="selfhosted/openbao/${CLUSTER}"
[ -f "${STORE_DIR}/${PASS_PREFIX}/unseal-key-1.gpg" ] \
    || die "no unseal keys found under ${PASS_PREFIX}"

[ $# -gt 0 ] || set -- "${BAO_ADDR:-$VAULT_ADDR}"

curl_opts=(-fsS)
[ -n "${BAO_CACERT:-}" ] && curl_opts+=(--cacert "$BAO_CACERT")

# unseal_node <address>
unseal_node() {
    local addr=${1%/} status sealed i key

    status=$(curl "${curl_opts[@]}" "${addr}/v1/sys/seal-status") \
        || die "${addr}: cannot reach OpenBao"
    jq -e '.initialized' <<<"$status" >/dev/null || die "${addr}: not initialized"

    if [ "$(jq -r '.sealed' <<<"$status")" = "false" ]; then
        echo "${addr}: already unsealed"
        return 0
    fi

    # Discard a half-finished unseal attempt so already-used keys don't collide.
    if [ "$(jq -r '.progress' <<<"$status")" -gt 0 ]; then
        curl "${curl_opts[@]}" -X PUT --data '{"reset":true}' "${addr}/v1/sys/unseal" >/dev/null
    fi

    sealed=true
    i=1
    while [ "$sealed" = "true" ]; do
        [ -f "${STORE_DIR}/${PASS_PREFIX}/unseal-key-${i}.gpg" ] \
            || die "${addr}: ran out of unseal keys after $((i - 1)), still sealed"

        key=$(pass show "${PASS_PREFIX}/unseal-key-${i}" | head -n 1)
        # printf is a builtin and the body goes via stdin, so the key never
        # shows up in the process list.
        status=$(printf '{"key":"%s"}' "$key" \
            | curl "${curl_opts[@]}" -X PUT --data @- "${addr}/v1/sys/unseal") \
            || die "${addr}: unseal request with key ${i} failed"

        sealed=$(jq -r '.sealed' <<<"$status")
        # progress resets to 0 once the threshold is reached, so only show it while sealed
        [ "$sealed" = "true" ] \
            && echo "${addr}: key ${i} accepted ($(jq -r '"\(.progress)/\(.t)"' <<<"$status"))"
        i=$((i + 1))
    done

    echo "${addr}: unsealed"
}

for addr in "$@"; do
    unseal_node "$addr"
done