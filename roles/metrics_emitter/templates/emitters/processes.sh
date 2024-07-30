#!/bin/sh

METRIC="{{ metrics_emitter_prefix }}_processes_total"
echo "# TYPE ${METRIC} gauge"
echo "${METRIC} $(ps -A | wc -l | tr -d '[:space:]')"
