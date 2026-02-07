#!/bin/bash

set -euo pipefail

# Configuration
DIR="${1:-/home/soeren/bla}"
HASHDEEP_DIR="${HASHDEEP_DIR:-/var/lib/hashdeep}"
NODE_EXPORTER_TEXTFILE_DIR="${NODE_EXPORTER_TEXTFILE_DIR:-/var/lib/node_exporter}"
TEMP_DIR="/tmp/hashdeep_$$"

# Generate baseline filename from directory path
# Replace / with _ and remove leading _
BASELINE_NAME=$(echo "$DIR" | sed 's/\//_/g' | sed 's/^_//')
BASELINE="${HASHDEEP_DIR}/${BASELINE_NAME}.txt"

# Generate metrics filename
METRICS_NAME="$BASELINE_NAME"
METRICS_FILE="${NODE_EXPORTER_TEXTFILE_DIR}/hashdeep_${METRICS_NAME}.prom"

# Track status for trap
SCRIPT_STATUS=0
NEW_FILES_COUNT=0
TOTAL_FILES_COUNT=0
VERIFICATION_STATUS=0
MODIFIED_FILES_COUNT=0
MOVED_FILES_COUNT=0
MISSING_FILES_COUNT=0

# Logging
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

error() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2
    SCRIPT_STATUS=0
}

cleanup() {
    # Write metrics on exit
    write_metrics "$SCRIPT_STATUS" "$NEW_FILES_COUNT" "$TOTAL_FILES_COUNT" "$VERIFICATION_STATUS" "$MODIFIED_FILES_COUNT" "$MOVED_FILES_COUNT" "$MISSING_FILES_COUNT"

    # Clean up temp files
    if [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
}

write_metrics() {
    local status=$1
    local new_files=${2:-0}
    local total_files=${3:-0}
    local verification_status=${4:-0}
    local modified_files=${5:-0}
    local moved_files=${6:-0}
    local missing_files=${7:-0}
    local timestamp$(date +%s)

    # Skip if textfile directory doesn't exist
    if [ ! -d "$NODE_EXPORTER_TEXTFILE_DIR" ]; then
        return
    fi

    cat > "${METRICS_FILE}.tmp" <<EOF
# HELP hashdeep_last_run_timestamp_seconds Timestamp of last hashdeep run
# TYPE hashdeep_last_run_timestamp_seconds gauge
hashdeep_last_run_timestamp_seconds{directory="$DIR"} $timestamp

# HELP hashdeep_last_run_success Whether the last run was successful (1=success, 0=failure)
# TYPE hashdeep_last_run_success gauge
hashdeep_last_run_success{directory="$DIR"} $status

# HELP hashdeep_new_files_added Number of new files added in last run
# TYPE hashdeep_new_files_added gauge
hashdeep_new_files_added{directory="$DIR"} $new_files

# HELP hashdeep_total_files_tracked Total number of files in baseline
# TYPE hashdeep_total_files_tracked gauge
hashdeep_total_files_tracked{directory="$DIR"} $total_files

# HELP hashdeep_verification_success Whether verification passed (1=pass, 0=fail, -1=not run)
# TYPE hashdeep_verification_success gauge
hashdeep_verification_success{directory="$DIR"} $verification_status

# HELP hashdeep_modified_files Number of files that failed hash verification
# TYPE hashdeep_modified_files gauge
hashdeep_modified_files{directory="$DIR"} $modified_files

# HELP hashdeep_moved_files Number of files that were moved
# TYPE hashdeep_moved_files gauge
hashdeep_moved_files{directory="$DIR"} $moved_files

# HELP hashdeep_missing_files Number of files that are missing
# TYPE hashdeep_missing_files gauge
hashdeep_missing_files{directory="$DIR"} $missing_files
EOF

    mv "${METRICS_FILE}.tmp" "$METRICS_FILE"
}

trap cleanup EXIT INT TERM

# Validate inputs
if [ ! -d "$DIR" ]; then
    error "Directory does not exist: $DIR"
    exit 1
fi

if [ ! -r "$DIR" ]; then
    error "Directory is not readable: $DIR"
    exit 1
fi

# Check for hashdeep
if ! command -v hashdeep &> /dev/null; then
    error "hashdeep is not installed"
    exit 1
fi

# Create hashdeep directory if it doesn't exist
if [ ! -d "$HASHDEEP_DIR" ]; then
    log "Creating hashdeep directory: $HASHDEEP_DIR"
    if ! mkdir -p "$HASHDEEP_DIR"; then
        error "Failed to create hashdeep directory: $HASHDEEP_DIR"
        exit 1
    fi
fi

# Check if hashdeep directory is writable
if [ ! -w "$HASHDEEP_DIR" ]; then
    error "Hashdeep directory is not writable: $HASHDEEP_DIR"
    exit 1
fi

# Check node_exporter textfile directory
if [ ! -d "$NODE_EXPORTER_TEXTFILE_DIR" ]; then
    log "Warning: Node exporter textfile directory does not exist: $NODE_EXPORTER_TEXTFILE_DIR"
    log "Metrics will not be exported"
fi

# Create temp directory
mkdir -p "$TEMP_DIR"

log "Using baseline file: $BASELINE"

# Create initial baseline if it doesn't exist
if [ ! -f "$BASELINE" ]; then
    log "Creating initial baseline for $DIR..."
    if hashdeep -c md5 -r "$DIR" > "$BASELINE"; then
        TOTAL=$(grep -v '^#' "$BASELINE" | grep -v '^%' | wc -l)
        log "Initial baseline created successfully with $TOTAL files"
        SCRIPT_STATUS=1
        NEW_FILES_COUNT=$TOTAL
        TOTAL_FILES_COUNT=$TOTAL
        VERIFICATION_STATUS=-1
        exit 0
    else
        error "Failed to create initial baseline"
        exit 1
    fi
fi

# Validate baseline file
if [ ! -r "$BASELINE" ]; then
    error "Baseline file is not readable: $BASELINE"
    exit 1
fi

if ! grep -q "^%%%% HASHDEEP-1.0" "$BASELINE"; then
    error "Baseline file does not appear to be a valid hashdeep file"
    exit 1
fi

log "Scanning for new files in $DIR..."

# Extract existing file paths from baseline
if ! grep -v '^#' "$BASELINE" | grep -v '^%' | awk -F',' '{print $3}' | sort -u > "$TEMP_DIR/baseline_files.txt"; then
    error "Failed to extract filenames from baseline"
    exit 1
fi

# Get all current files
if ! find "$DIR" -type f -print 2>/dev/null | sort > "$TEMP_DIR/current_files.txt"; then
    error "Failed to list files in directory"
    exit 1
fi

# Find new files
comm -13 "$TEMP_DIR/baseline_files.txt" "$TEMP_DIR/current_files.txt" > "$TEMP_DIR/new_files.txt"

NEW_COUNT=$(wc -l < "$TEMP_DIR/new_files.txt")
TOTAL=$(wc -l < "$TEMP_DIR/baseline_files.txt")

if [ "$NEW_COUNT" -eq 0 ]; then
    log "No new files found"
    NEW_FILES_COUNT=0
else
    log "Found $NEW_COUNT new file(s), computing hashes..."

    # Create backup of baseline
    BACKUP="${BASELINE}.backup.$(date +'%Y%m%d_%H%M%S')"
    if ! cp "$BASELINE" "$BACKUP"; then
        error "Failed to create backup of baseline"
        exit 1
    fi

    log "Created backup: $BACKUP"

    # Hash new files and append to baseline
    if xargs -d '\n' hashdeep -c md5 < "$TEMP_DIR/new_files.txt" | grep -v '^#' | grep -v '^%' >> "$BASELINE"; then
        log "Successfully added $NEW_COUNT file(s) to baseline"

        # Remove backup if successful
        rm "$BACKUP"

        NEW_FILES_COUNT=$NEW_COUNT
    else
        error "Failed to hash new files, restoring backup"
        mv "$BACKUP" "$BASELINE"
        exit 1
    fi
fi

# Count total files after potential additions
TOTAL=$(grep -v '^#' "$BASELINE" | grep -v '^%' | wc -l)
TOTAL_FILES_COUNT=$TOTAL

log "Running verification against baseline..."

# Run audit mode to verify integrity
if hashdeep -c md5 -r -a -k "$BASELINE" "$DIR" > "$TEMP_DIR/audit_output.txt" 2>&1; then
    log "Verification completed successfully - no issues found"
    VERIFICATION_STATUS=1
    MODIFIED_FILES_COUNT=0
    MOVED_FILES_COUNT=0
    MISSING_FILES_COUNT=0
    SCRIPT_STATUS=1
else
    log "Verification found issues:"
    cat "$TEMP_DIR/audit_output.txt"

    # Parse audit output for different types of issues (redirect to /dev/null to avoid stdout)
    MODIFIED_FILES_COUNT=$(grep -c "Hash mismatch" "$TEMP_DIR/audit_output.txt" 2>/dev/null || true)
    MOVED_FILES_COUNT=$(grep -c "moved to" "$TEMP_DIR/audit_output.txt" 2>/dev/null || true)
    MISSING_FILES_COUNT=$(grep -c "No such file" "$TEMP_DIR/audit_output.txt" 2>/dev/null || true)

    # Ensure they're numbers (in case grep returns empty)
    MODIFIED_FILES_COUNT=${MODIFIED_FILES_COUNT:-0}
    MOVED_FILES_COUNT=${MOVED_FILES_COUNT:-0}
    MISSING_FILES_COUNT=${MISSING_FILES_COUNT:-0}

    log "Modified files: $MODIFIED_FILES_COUNT"
    log "Moved files: $MOVED_FILES_COUNT"
    log "Missing files: $MISSING_FILES_COUNT"

    VERIFICATION_STATUS=0
    SCRIPT_STATUS=1  # Script ran successfully even if verification failed
fi

log "Update complete - Total files tracked: $TOTAL_FILES_COUNT"
