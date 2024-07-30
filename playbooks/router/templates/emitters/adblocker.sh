#!/bin/sh

METRIC="openbsd_adblocker_entries_total"
CNT=$(wc -l {{ adblocker_file }} 2> /dev/null | awk '{print $1}')

if [ -z "${CNT}" ]; then
    exit 1
fi

echo "# TYPE ${METRIC} gauge"
echo "${METRIC} ${CNT}"
