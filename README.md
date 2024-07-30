# ansible

![gitleaks](https://github.com/soerenschneider/ansible/actions/workflows/gitleaks.yaml/badge.svg)
![lint-workflow](https://github.com/soerenschneider/ansible/actions/workflows/lint.yaml/badge.svg)
![security-workflow](https://github.com/soerenschneider/ansible/actions/workflows/security.yaml/badge.svg)

This is my collection of privately used Ansible playbooks and roles which powers [my own hybrid cloud](https://github.com/soerenschneider/soeren.cloud).

## Requirements

1. [Install Ansible](https://docs.ansible.com/ansible/latest/installation_guide/index.html):
   1. Using pip: `pip3 install ansible`

1. Run `git clone https://github.com/soerenschneider/ansible.git ` to clone this repository to your local drive.
1. Run `ansible-galaxy install -r requirements.yml` inside this directory to install required Ansible roles.
1. Define an [Ansible inventory](https://docs.ansible.com/ansible/latest/inventory_guide/intro_inventory.html) that matches your infrastructure.
1. Choose a playbook and run it: `ansible-playbook -i path-to-your-inventory path-to-the-playbook`
