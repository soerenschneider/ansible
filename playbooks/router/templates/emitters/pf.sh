#!/bin/sh

PREFIX="openbsd_pf"

function scrape {
    PF="$(pfctl -s info)"
    
    PACKETS_IN=$(echo "$PF" | grep 'Packets In$' -A2)
    if [ ! -z "${PACKETS_IN}" ]; then
        echo "# TYPE ${PREFIX}_ipv4_packets_total counter"
        IPV4_PACKETS_IN_PASSED=$(echo "$PACKETS_IN" | grep Passed | awk '{print $2}')
        echo "${PREFIX}_ipv4_packets_total{direction=\"in\",action=\"passed\"} ${IPV4_PACKETS_IN_PASSED}"

        IPV4_PACKETS_IN_BLOCKED=$(echo "$PACKETS_IN" | grep Blocked | awk '{print $2}')
        echo "${PREFIX}_ipv4_packets_total{direction=\"in\",action=\"blocked\"} ${IPV4_PACKETS_IN_BLOCKED}"
    fi

    PACKETS_OUT=$(echo "$PF" | grep 'Packets Out$' -A2)
    if [ ! -z "${PACKETS_OUT}" ]; then
        IPV4_PACKETS_OUT_PASSED=$(echo "$PACKETS_OUT" | grep Passed | awk '{print $2}')
        echo "${PREFIX}_ipv4_packets_total{direction=\"out\",action=\"passed\"} ${IPV4_PACKETS_OUT_PASSED}"

        IPV4_PACKETS_OUT_BLOCKED=$(echo "$PACKETS_OUT" | grep Blocked | awk '{print $2}')
        echo "${PREFIX}_ipv4_packets_total{direction=\"out\",action=\"blocked\"} ${IPV4_PACKETS_OUT_BLOCKED}"
    fi

    STATE_TABLE=$(echo "$PF" | grep '^State Table' -A5 )
    
    STATE_ENTRIES=$(echo "$STATE_TABLE" | grep 'current entries' | awk '{ print $3 }')
    echo "# TYPE ${PREFIX}_state_entries gauge"
    echo "${PREFIX}_state_entries $STATE_ENTRIES"

    STATE_SEARCHES=$(echo "$STATE_TABLE" | grep 'searches' | awk '{ print $2 }')
    echo "# TYPE ${PREFIX}_state_searches counter"
    echo "${PREFIX}_state_searches $STATE_SEARCHES"

    STATE_INSERTS=$(echo "$STATE_TABLE" | grep 'inserts' | awk '{ print $2 }')
    echo "# TYPE ${PREFIX}_state_inserts counter"
    echo "${PREFIX}_state_inserts $STATE_INSERTS"

    STATE_REMOVALS=$(echo "$STATE_TABLE" | grep 'removals' | awk '{ print $2 }')
    echo "# TYPE ${PREFIX}_state_removals counter"
    echo "${PREFIX}_state_removals $STATE_REMOVALS"
}

scrape
