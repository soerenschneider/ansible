FROM ghcr.io/getsops/sops:v3.10.0-alpine AS sops

FROM python:3.13.2-alpine3.21

COPY --from=sops /usr/local/bin/sops /usr/bin/sops

COPY ./requirements-docker.txt /requirements.txt
RUN pip install -r /requirements.txt

COPY ./requirements.yml /requirements.yml
RUN ansible-galaxy install -r /requirements.yml

COPY ./playbooks /data/playbooks
COPY ./roles /data/roles

WORKDIR /data/playbooks
