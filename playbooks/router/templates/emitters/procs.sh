#!/bin/sh

PROCS="openbsd_procs_total"

echo "# TYPE $PROCS gauge"
echo "${PROCS} $(ps -A | wc -l | awk '{print $1}')"
