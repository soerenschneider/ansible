#!/bin/sh

SYSTEM_USERS="openbsd_users_logged_in_total"

echo "# TYPE $SYSTEM_USERS gauge"
echo "${SYSTEM_USERS} $(who | wc -l | awk '{print $1}')"
