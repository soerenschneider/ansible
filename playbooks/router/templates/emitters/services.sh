#!/bin/sh

PROCS="unbound dhcpd sshd smtpd ntpd cron syslogd"

print_processes() {
    echo "# TYPE openbsd_service_pid gauge"
    for proc in $(echo $PROCS); do
        PID=$(pgrep -o -P1 -x $proc)
        if [ -z "${PID}" ]; then
            PID="-1"
        fi
        echo "openbsd_service_pid{service=\"${proc}\"} $PID"
    done
    echo "# TYPE openbsd_service_timestamp gauge"
    echo "openbsd_service_timestamp $(date +%s)"
}

print_processes
