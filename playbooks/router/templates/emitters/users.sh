#!/bin/sh

SYSTEM_USERS="openbsd_users_total"

echo "# TYPE $SYSTEM_USERS gauge"
echo "${SYSTEM_USERS}{users=\"sshd\"} $(groupinfo sshd | grep members | awk -F' ' '{print NF-1; exit}')"
echo "${SYSTEM_USERS}{users=\"wheel\"} $(groupinfo wheel | grep members | awk -F' ' '{print NF-1; exit}')"
echo "${SYSTEM_USERS}{users=\"all\"} $(wc -l /etc/master.passwd | awk '{print $1}')"
