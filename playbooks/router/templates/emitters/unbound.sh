#!/bin/sh

QUERIES_TOTAL_METRIC_NAME="openbsd_unbound_num_queries_total"
QUERIES_VIA_TCP_METRIC_NAME="openbsd_unbound_num_queries_tcp_total"
CACHE_HITS_METRIC_NAME="openbsd_unbound_num_cache_hits_total"
CACHE_MISSES_METRIC_NAME="openbsd_unbound_num_cache_misses_total"
CACHE_EXPIRED_METRIC_NAME="openbsd_unbound_expired_entries_total"
CACHE_PREFETCHED_METRIC_NAME="openbsd_unbound_prefetched_total"
UPTIME_METRIC_NAME="openbsd_unbound_uptime_service_seconds"

STATS="$(unbound-control stats 2> /dev/null)"

if [ ! $? -eq 0 ]; then
    exit 0
fi

echo "# TYPE $QUERIES_TOTAL_METRIC_NAME gauge"
echo "$QUERIES_TOTAL_METRIC_NAME $(echo "$STATS" | grep 'total\.num\.queries=' | cut -d'=' -f2)"

echo "# TYPE $QUERIES_VIA_TCP_METRIC_NAME gauge"
echo "$QUERIES_VIA_TCP_METRIC_NAME $(echo "$STATS" | grep 'total\.tcpusage=' | cut -d'=' -f2)"

echo "# TYPE $CACHE_HITS_METRIC_NAME gauge"
echo "$CACHE_HITS_METRIC_NAME $(echo "$STATS" | grep 'total\.num\.cachehits=' | cut -d'=' -f2)"

echo "# TYPE $CACHE_MISSES_METRIC_NAME gauge"
echo "$CACHE_MISSES_METRIC_NAME $(echo "$STATS" | grep 'total\.num\.cachemiss=' | cut -d'=' -f2)"

echo "# TYPE $CACHE_EXPIRED_METRIC_NAME gauge"
echo "$CACHE_EXPIRED_METRIC_NAME $(echo "$STATS" | grep 'total\.num\.expired=' | cut -d'=' -f2)"

echo "# TYPE $CACHE_PREFETCHED_METRIC_NAME gauge"
echo "$CACHE_PREFETCHED_METRIC_NAME $(echo "$STATS" | grep 'total\.num\.prefetch=' | cut -d'=' -f2)"

echo "# TYPE $UPTIME_METRIC_NAME gauge"
echo "$UPTIME_METRIC_NAME $(echo "$STATS" | grep 'time\.up=' | cut -d'=' -f2)"
