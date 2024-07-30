#!/bin/sh

METRIC="{{ metrics_emitter_prefix }}_users_total"
echo "# TYPE ${METRIC} gauge"
echo "${METRIC} $(wc -l /etc/passwd | sed 's/[^0-9]*//g')"

METRIC="{{ metrics_emitter_prefix }}_groups_total"
echo "# TYPE ${METRIC} gauge"
echo "${METRIC} $(wc -l /etc/group | sed 's/[^0-9]*//g')"
