#!/bin/sh

DEFAULT_ROUTE_INTERFACE="openbsd_route_default"
DEFAULT_ROUTE_INTERFACE_VALUE="$(route -n show | grep ^default | head -n1 | awk '{print $NF}')"

ROUTE_ENTRIES_CNT="openbsd_route_entries_total"
ROUTE_ENTRIES_IPV4="$(route -n show -inet | wc -l | awk '{print $1 - 4}')"
ROUTE_ENTRIES_IPV6="$(route -n show -inet6 | wc -l | awk '{print $1 - 4}')"

echo "# TYPE ${DEFAULT_ROUTE_INTERFACE} gauge"
echo "${DEFAULT_ROUTE_INTERFACE}{interface=\"${DEFAULT_ROUTE_INTERFACE_VALUE}\"} 1"

echo "# TYPE ${ROUTE_ENTRIES_CNT} gauge"
echo "${ROUTE_ENTRIES_CNT}{ip=\"ipv4\"} ${ROUTE_ENTRIES_IPV4}"
echo "${ROUTE_ENTRIES_CNT}{ip=\"ipv6\"} ${ROUTE_ENTRIES_IPV6}"
