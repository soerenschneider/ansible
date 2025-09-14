#!/bin/sh

FILE=/tmp/vault-init.txt
set -o pipefail
set -eu
cluster="$1"

if [ -z "$cluster" ]; then
    echo "supply cluster"
    exit 1
fi

for i in 1 2 3 4 5; do
    if grep "^Unseal Key $i" "${FILE}"; then
        grep "^Unseal Key $i" "${FILE}" | awk -F': ' '{print $2}' | base64 -d | gpg2 -d | pass insert -e -f "vault/${cluster}/unseal-key-$i"
    fi
done
