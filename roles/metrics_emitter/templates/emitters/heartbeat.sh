#!/bin/sh

METRIC="{{ metrics_emitter_prefix }}_heartbeat_timestamp_seconds"
echo "# TYPE ${METRIC} gauge"
echo "${METRIC} $(date +%s)"
