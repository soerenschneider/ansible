#!/bin/sh

METRIC="openbsd_cpu_temp_celsius"
TEMP=$(sysctl hw.sensors | grep temp | head -n1 | cut -d"=" -f2 | cut -d" " -f1)

if [ "${TEMP}" == "0" ]; then
    exit 0
fi

echo "# TYPE ${METRIC} gauge"
echo "${METRIC} ${TEMP}"
