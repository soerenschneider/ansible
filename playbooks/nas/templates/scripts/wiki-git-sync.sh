#!/usr/bin/env bash

set -u

WIKI_DIR="{{ wiki_git_sync_dir }}"
NODEEXPORTER_DIR="{{ node_exporter_textfile_dir }}"
NODEEXPORTER_METRIC_PREFIX="wiki_git_sync"
NODEEXPORTER_METRIC_FILE="${NODEEXPORTER_DIR}/wiki_git_sync.prom"
NODEEXPORTER_METRIC_FILE_TMP="${NODEEXPORTER_DIR}/wiki_git_sync.prom.tmp"

sync_wiki_dir() {
	if [ ! -d "${WIKI_DIR}" ]; then
	  return 1
	fi

	cd "${WIKI_DIR}"
	if [ $? -ne 0 ]; then
	  return 1
  fi

	if git-sync -n; then
	  return 0
	fi

	return 1
}

TIME=$(date +%s)
SUCCESS=0

if sync_wiki_dir; then
  SUCCESS=1
fi

echo "${NODEEXPORTER_METRIC_PREFIX}_timestamp_seconds ${TIME}" > "${NODEEXPORTER_METRIC_FILE_TMP}"
echo "${NODEEXPORTER_METRIC_PREFIX}_success_bool ${SUCCESS}"   >> "${NODEEXPORTER_METRIC_FILE_TMP}"

mv "${NODEEXPORTER_METRIC_FILE_TMP}" "${NODEEXPORTER_METRIC_FILE}"

if [ ${SUCCESS} -eq 0 ]; then
  exit 1
fi
