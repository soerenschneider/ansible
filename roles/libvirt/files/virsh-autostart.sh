#!/usr/bin/env bash

# Only run one instance at a time, wait for a running instance to finish.
exec 9>/run/lock/virsh-autostart.lock
flock -w 300 9 || exit 1

virsh list --autostart --state-shutoff --name | xargs -r -n1 virsh start
