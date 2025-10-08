FROM ghcr.io/getsops/sops:v3.11.0-alpine AS sops

FROM python:3.14.0-alpine3.21

COPY --from=sops /usr/local/bin/sops /usr/bin/sops

COPY ./requirements-docker.txt /requirements.txt
RUN pip install -r /requirements.txt

COPY ./requirements.yml /requirements.yml
RUN ansible-galaxy install -r /requirements.yml

COPY ./playbooks /data/playbooks
COPY ./roles /data/roles

RUN addgroup -S ansible && \
    adduser -S ansible -G ansible && \
    mkdir /data/inventory && \
    chown ansible:ansible /data/inventory

ENV ANSIBLE_INVENTORY=/data/inventory/inventory.yml
USER ansible
WORKDIR /data/playbooks
