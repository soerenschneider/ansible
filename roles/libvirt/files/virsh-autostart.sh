#!/usr/bin/env bash

virsh list --autostart --state-shutoff --name | xargs -r -n1 virsh start
