#!/bin/sh

METRIC="{{ metrics_emitter_prefix }}_logged_in_users_total"
echo "# TYPE ${METRIC} gauge"
echo "${METRIC} $(w | sed 1,2d | wc -l | tr -d '[:space:]')"
