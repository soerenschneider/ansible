#!/usr/bin/env bash

# Directory containing your FLAC files
input_dir="$1"
# Directory to store the converted Opus files
output_dir="$2"
# "opus" or "vorbis"
codec="${3:-opus}"
bitrate="${4:-128k}"
quality_mode="${5:-vbr}"
# Number of threads to use (parallel jobs)
num_threads="${6:-$(nproc --all)}"

# Config
LAST_RUN_FILE="${input_dir}/.flac_convert_timestamp"

# Step 1: Load the last run time, or default to epoch if not available
if [[ -f "${LAST_RUN_FILE}" ]]; then
    LAST_RUN=$(cat "${LAST_RUN_FILE}")
else
    LAST_RUN=0
fi

# Step 2: Check if there are any files newer than the last run time
echo "Checking for files newer than $(date -d "@${LAST_RUN}")..."
NEW_FILES=$(find "${input_dir}" -type f -newermt "@${LAST_RUN}" ! -path "${LAST_RUN_FILE}")

if [[ -z "${NEW_FILES}" ]]; then
    echo "No new files since last run at $(date -d "@$LAST_RUN")"
    exit 0
else
  echo "Found new files ${NEW_FILES}"
fi

# Create the output directory if it doesn't exist
mkdir -p "$output_dir"

# Function to convert FLAC to Opus and copy the cover if it doesn't exist
convert_flac() {
    flac_file="$1"
    input_dir="$2"
    output_dir="$3"
    codec="${4}"
    bitrate="${5}"
    quality_mode="${6}"

    # Validate codec input
    if [[ "$codec" != "opus" && "$codec" != "vorbis" ]]; then
        echo "Error: Unsupported codec '$codec'. Use 'opus' or 'vorbis'."
        return 1
    fi

    # Set output extension based on codec
    if [[ "$codec" == "opus" ]]; then
        output_ext="opus"
        audio_codec="libopus"
    else
        output_ext="ogg"
        audio_codec="libvorbis"
    fi

    relative_path="${flac_file#$input_dir}"
    output_file="$output_dir${relative_path%.flac}.$output_ext"
    output_dir_path=$(dirname "$output_file")

    # Create necessary output directories
    mkdir -p "$output_dir_path"

    # Look for cover.* file in the same directory as the FLAC file and copy it if it doesn't already exist
    cover_file_src="$(dirname "$flac_file")"
    cover_file_dst="$output_dir_path"

    if [[ $(basename "$flac_file") == *"01"* ]]; then
        if find "$cover_file_src" -type f -iname "cover.*" -print -quit | grep -q .; then
            # Only copy if the cover file does not already exist in the destination
            if ! find "$cover_file_dst" -type f -iname "cover.*" -print -quit | grep -q .; then
                # Copy the first found cover file from source to destination
                find "$cover_file_src" -type f -iname "cover.*" -exec cp -v {} "$cover_file_dst/" \; 
            fi
        fi
    fi

    # if the output file exists, compare length of both files
    if [ -f "$output_file" ]; then
        # explanation: capture stdout and stderr in two different variables, flac_duration and stderr_content.
        # this is needed as ffprobe may print some errors when a flac file has invalid metadata
        {
            IFS=$'\n' read -r -d '' stderr_content;
            IFS=$'\n' read -r -d '' flac_duration;
        } < <((printf '\0%s\0' "$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "${flac_file}")" 1>&2) 2>&1)

        if [[ -n "$stderr_content" ]]; then
            echo "⚠️ Warning in $flac_file: $stderr_content" >&2
        fi

        # get duration of already converted file
        converted_duration=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$output_file")

        # Calculate absolute difference and compare with threshold
        diff=$(awk -v a="$flac_duration" -v b="$converted_duration" 'BEGIN {
            diff = a - b;
            if (diff < 0) diff = -diff;
            print diff
        }')

        # Set a threshold
        threshold=1.0

        if awk -v d="${diff}" -v t="${threshold}" 'BEGIN { exit !(d <= t) }'; then
            # Durations are close enough
            return
        else
            echo "Durations differ (flac: ${flac_duration} vs. ${codec}: ${converted_duration}, diff=${diff}), proceeding with conversion."
        fi
    fi

    # Set ffmpeg options based on codec and quality mode
    ffmpeg_opts=("-y" "-loglevel" "quiet" "-i" "${flac_file}" "-c:a" "${audio_codec}")

    if [[ "${codec}" == "opus" ]]; then
        # Opus supports different VBR/CBR modes
        case "$quality_mode" in
            "cbr")         ffmpeg_opts+=("-vbr" "off" "-b:a" "${bitrate}") ;;
            "vbr")         ffmpeg_opts+=("-vbr" "on" "-b:a" "${bitrate}") ;;
            "constrained") ffmpeg_opts+=("-vbr" "constrained" "-b:a" "${bitrate}") ;;
            *)             ffmpeg_opts+=("-vbr" "on" "-b:a" "${bitrate}") ;; # Default to VBR
        esac
    else
        # Vorbis is always VBR (no CBR mode)
        ffmpeg_opts+=("-q:a" "${bitrate}")  # Adjust quality level (0-10, where 4 ≈ ~128k)
    fi

    # Convert FLAC to selected format
    echo "Converting ${flac_file} to ${output_file}"
    ffmpeg "${ffmpeg_opts[@]}" "$output_file"
}

export -f convert_flac  # Export the function to be available to xargs

# Find all FLAC files and prepare the list
find "${input_dir}" -type f -name "*.flac" -print0 | \
  # Run the conversion in parallel using xargs with null separator
  xargs -0 -I {} -n 1 -P "${num_threads}" bash -c 'convert_flac "$@"' _ {} "${input_dir}" "${output_dir}" "${codec}" "${bitrate}" "${quality_mode}"

# Step 4: Save current time as last run time (UNIX timestamp)
date +%s > "${LAST_RUN_FILE}"
