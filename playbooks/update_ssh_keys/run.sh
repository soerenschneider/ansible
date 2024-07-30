#!/bin/bash

ansible-playbook -i ~/src/gitlab/playbooks/inventory/prod/inventory.yml --tags authorized_keys playbook.yml
