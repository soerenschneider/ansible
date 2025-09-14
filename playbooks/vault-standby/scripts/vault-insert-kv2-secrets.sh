#!/usr/bin/env bash

set -eu
set -opipefail

vault kv put secret/occult/nas gocryptfs_media="$(pass hw/gocryptfs/media | head -n1)"
