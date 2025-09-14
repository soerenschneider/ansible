#!/bin/sh

grep "^Unseal"  /tmp/vault-init.txt | awk '{print $4}' | base64 -d | gpg2 -d | pass insert --echo --force vault/prd/unseal-key-1
grep "^Initial" /tmp/vault-init.txt | awk '{print $4}' | base64 -d | gpg2 -d | pass insert --echo --force vault/prd/root-token
