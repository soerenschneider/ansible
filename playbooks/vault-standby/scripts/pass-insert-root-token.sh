#!/bin/sh

FILE=/tmp/vault-init.txt
cluster="$1"
set -ue

if [ -z "$cluster" ]; then
    echo "supply cluster"
    exit 1
fi

grep "^Initial Root Token" "${FILE}" | awk -F': ' '{print $2}' | base64 -d | gpg2 -d | pass insert -e -f vault/"${cluster}"/root-token
