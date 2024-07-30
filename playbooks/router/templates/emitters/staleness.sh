#!/bin/sh

STALENESS="openbsd_emitter_timestamp_seconds"

echo "# TYPE $STALENESS gauge"
echo "${STALENESS} $(date +%s)"
