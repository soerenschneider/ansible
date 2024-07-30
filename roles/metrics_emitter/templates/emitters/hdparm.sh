#!/bin/sh
DRIVE="{{ hdparm_disk | default('') }}"

if [ -z "${DRIVE}" ]; then
    exit 0
fi

METRIC="{{ metrics_emitter_prefix }}_hdparm_drive_status"

echo "# TYPE $METRIC gauge"

VAL="$(hdparm -C ${DRIVE} 2> /dev/null | grep 'drive state is' | cut -d: -f2 | sed 's/ //g')"
if [ 'standby' == "$VAL" ]; then
    VAL=0
else
    VAL=1
fi
echo "$METRIC{drive=\"$DRIVE\"} $VAL"
