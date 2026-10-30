pre-commit-init:
	pre-commit install
	pre-commit install --hook-type commit-msg

pre-commit-update:
	pre-commit autoupdate

# ansible-core does not support python 3.15 yet
PYTHON ?= python3.14
VENV ?= venv
# roles that have molecule scenarios, override with ROLE=<name>
ROLE ?= $(notdir $(patsubst %/molecule,%,$(wildcard roles/*/molecule)))
MOLECULE = cd roles/$$role && PATH="$(CURDIR)/$(VENV)/bin:$$PATH" molecule

$(VENV)/bin/molecule: requirements-molecule.txt
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements-molecule.txt
	touch $@

.PHONY: molecule molecule-converge molecule-verify molecule-destroy
molecule: $(VENV)/bin/molecule
	@set -e; for role in $(ROLE); do ($(MOLECULE) test --all); done

molecule-converge: $(VENV)/bin/molecule
	@set -e; for role in $(ROLE); do ($(MOLECULE) converge); done

molecule-verify: $(VENV)/bin/molecule
	@set -e; for role in $(ROLE); do ($(MOLECULE) verify); done

molecule-destroy: $(VENV)/bin/molecule
	@set -e; for role in $(ROLE); do ($(MOLECULE) destroy --all); done
