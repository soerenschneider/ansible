#!/bin/sh

PPPOE_UPTIME="openbsd_uplink_uptime_seconds"

OUTPUT="$(ifconfig pppoe0 | grep time | cut -d':' -f5-)"
UPTIME=$(echo "${OUTPUT}" | cut -d'd' -f2 | awk -F: '{ print ($1 * 3600) + ($2 * 60) + $3 }')

if echo "${OUTPUT}" | grep -q "d"; then
    DAYS=$(echo "${OUTPUT}" | cut -d'd' -f1)
fi

if [ -z "${UPTIME}" ]; then
    exit 0
fi

if [ ! -z "${DAYS}" ]; then
    UPTIME=$((UPTIME + DAYS * 86400 ))
fi

echo "# TYPE $PPPOE_UPTIME gauge"
echo "${PPPOE_UPTIME}{link=\"pppoe0\"} $UPTIME"
