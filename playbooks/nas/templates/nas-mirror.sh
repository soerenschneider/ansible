#!/usr/bin/env bash
#
# nas-nas-mirror.sh — biweekly rsync of selected subdirs of /srv/files
#               from nas.dc1 to the local mountpoint.
#
# Behaviour:
#   - Mirrors each subdir in $SUBDIRS from $SRC_BASE to $DST_BASE.
#   - Files deleted or changed on the source are moved into a dated
#     tree under $BACKUP_ROOT (inside $DST_BASE — only place with space).
#   - Excludes are split between a global $EXCLUDES array and per-subdir
#     $EXCLUDES_<subdir> arrays. Both apply.
#   - SAFETY: refuses to run if either $DST_BASE (local) or
#     $REMOTE_MOUNTPOINT (remote) isn't actually a mountpoint.
#   - SAFETY: rsync aborts a subdir if it would delete more than
#     $MAX_DELETE files in one run.
#   - Single-instance via flock.
#   - One SSH connection reused across all subdir syncs.
#
# Cron (closest to "every two weeks" — 1st & 15th, 03:00):
#   0 3 1,15 * *  /usr/local/sbin/nas-nas-mirror.sh
#
# Exit codes:
#   0  all subdirs synced cleanly
#   1  another instance is running
#   2  $DST_BASE is not a mountpoint
#   3  one or more subdir syncs failed (rsync error, max-delete hit, etc.)
#   4  remote mountpoint check failed (source disk not mounted on NAS)
#
# Requires: bash >= 4.3, rsync, flock, ssh, findmnt, mountpoint.

set -uo pipefail

# ---------- configuration ----------
SRC_BASE="nas.dd.soeren.cloud:/srv/files"       # host:path, no trailing slash
DST_BASE="/srv/files"                  		# no trailing slash, must be a mountpoint
SUBDIRS=("private" "media" "photos")

BACKUP_ROOT="${DST_BASE}/.rsync-trash"
RETENTION_DAYS=60

LOG_DIR="/var/log/nas-sync"
LOCK_FILE="/var/lock/nas-sync.lock"
BWLIMIT=""

# ----- safety knobs -----
# Verify this path is a mountpoint on the source host before syncing.
# Catches the catastrophic case: source disk not mounted -> source appears
# empty -> rsync --delete would wipe the destination.
# Set to empty string to skip (e.g. if the NAS exports a regular directory).
REMOTE_MOUNTPOINT="/srv/files"

# Hard cap on deletions per subdir per run. If rsync would delete more
# than this, it aborts with exit 25 and the script flags that subdir
# as failed. Tune up if you legitimately churn more in a 2-week window.
MAX_DELETE=1000
# ------------------------

SSH_OPTS="-o BatchMode=yes \
-o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
-o ControlMaster=auto -o ControlPath=/tmp/.nas-sync-%r@%h:%p \
-o ControlPersist=60s"

# ---------- excludes ----------
EXCLUDES=(
    ".cache/"
    ".Trash-*/"
    "lost+found/"
    "*.tmp"
    "*~"
)

EXCLUDES_private=()
EXCLUDES_media=(
    "/.incoming/"
    "/music-opus-96k/"
)
EXCLUDES_photos=()
# -----------------------------------

TS="$(date +%Y-%m-%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/sync_${TS}.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1
log() { echo "[$(date -Is)] $*"; }

log "starting nas-sync (pid $$)"
log "  src_base=$SRC_BASE"
log "  dst_base=$DST_BASE"
log "  subdirs: ${SUBDIRS[*]}"
log "  backup_root=$BACKUP_ROOT"
log "  retention_days=$RETENTION_DAYS"
log "  max_delete=$MAX_DELETE per subdir per run"
log "  remote_mountpoint=${REMOTE_MOUNTPOINT:-<disabled>}"
log "  global excludes (${#EXCLUDES[@]}): ${EXCLUDES[*]:-<none>}"

# Single-instance lock.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another nas-sync is already running, exiting"
    exit 1
fi

# Local mount check.
if ! mountpoint -q "$DST_BASE"; then
    log "ERROR: $DST_BASE is not a mountpoint — refusing to sync"
    exit 2
fi
log "local mountpoint OK: $(findmnt -no SOURCE,FSTYPE "$DST_BASE")"

# Remote mount check — opens the SSH ControlMaster connection as a side effect.
if [[ -n "$REMOTE_MOUNTPOINT" ]]; then
    remote_host="${SRC_BASE%%:*}"
    log "checking remote mountpoint: ${remote_host}:${REMOTE_MOUNTPOINT}"
    if ! ssh $SSH_OPTS "$remote_host" "mountpoint -q ${REMOTE_MOUNTPOINT@Q}"; then
        log "ERROR: ${REMOTE_MOUNTPOINT} is not a mountpoint on ${remote_host}"
        log "       refusing to sync — source disk may not be mounted, and"
        log "       rsync --delete would otherwise mirror the empty source"
        log "       onto the destination."
        exit 4
    fi
    remote_info="$(ssh $SSH_OPTS "$remote_host" "findmnt -no SOURCE,FSTYPE ${REMOTE_MOUNTPOINT@Q}" 2>/dev/null || true)"
    log "remote mountpoint OK: ${remote_info:-<unknown>}"
fi

mkdir -p "$BACKUP_ROOT"

BW_ARGS=()
[[ -n "$BWLIMIT" ]] && BW_ARGS=(--bwlimit="$BWLIMIT")

# Sync each subdir.
overall_rc=0
for sub in "${SUBDIRS[@]}"; do
    log "==== syncing $sub ===="
    src="${SRC_BASE}/${sub}/"
    dst="${DST_BASE}/${sub}/"
    backup_dir="${BACKUP_ROOT}/${TS}/${sub}"

    mkdir -p "$dst"

    EXCLUDE_ARGS=()
    for pat in "${EXCLUDES[@]}"; do
        EXCLUDE_ARGS+=(--exclude="$pat")
    done

    sub_var="EXCLUDES_${sub}"
    if declare -p "$sub_var" &>/dev/null; then
        declare -n sub_excludes_ref="$sub_var"
        for pat in "${sub_excludes_ref[@]}"; do
            EXCLUDE_ARGS+=(--exclude="$pat")
        done
        log "  $sub excludes: global (${#EXCLUDES[@]}) + subdir (${#sub_excludes_ref[@]}): ${sub_excludes_ref[*]:-<none>}"
        unset -n sub_excludes_ref
    else
        log "  $sub excludes: global only (${#EXCLUDES[@]})"
    fi

    set +e
    rsync \
        -aHAX \
        --numeric-ids \
        --delete \
        --max-delete="$MAX_DELETE" \
        --backup \
        --backup-dir="$backup_dir" \
        --partial-dir=".rsync-partial" \
        --info=name,stats2,progress2 \
        --human-readable \
        -e "ssh $SSH_OPTS" \
        "${EXCLUDE_ARGS[@]}" \
        "${BW_ARGS[@]}" \
        "$src" "$dst"
    rc=$?
    set -e

    case $rc in
        0)  log "  $sub: OK" ;;
        24) log "  $sub: OK (some source files vanished mid-transfer; non-fatal)" ;;
        25) log "  $sub: FAILED — would have deleted more than $MAX_DELETE files (safety abort)"
            overall_rc=3 ;;
        *)  log "  $sub: FAILED with code $rc"
            overall_rc=3 ;;
    esac

    [[ -d "$backup_dir" ]] && [[ -z "$(ls -A "$backup_dir" 2>/dev/null)" ]] \
        && rmdir "$backup_dir" 2>/dev/null || true
done

[[ -d "${BACKUP_ROOT}/${TS}" ]] && [[ -z "$(ls -A "${BACKUP_ROOT}/${TS}" 2>/dev/null)" ]] \
    && rmdir "${BACKUP_ROOT}/${TS}" 2>/dev/null \
    && log "no deletions this run; removed empty ${BACKUP_ROOT}/${TS}"

log "pruning backups older than ${RETENTION_DAYS} days under $BACKUP_ROOT"
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" \
    -print -exec rm -rf {} +

find "$LOG_DIR" -type f -name 'sync_*.log'    -mtime +7   -exec gzip {} \;
find "$LOG_DIR" -type f -name 'sync_*.log.gz' -mtime +180 -delete

log "done (overall exit $overall_rc)"
exit $overall_rc
