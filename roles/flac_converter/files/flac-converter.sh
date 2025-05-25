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

# Create the output directory if it doesn't exist
mkdir -p "$output_dir"


get_duration() {
    local file="$1"
    # Get the duration from ffprobe
    duration=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$file")

    # Extract the first digit after the decimal point
    rounded_duration=$(printf "%.0f" "$duration")

    # Return the rounded duration
    echo "$rounded_duration"
}

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

    # Check if the output file exists and is larger than 10KB
    if [ -f "$output_file" ]; then
        flac_duration=$(get_duration "$flac_file")
        converted_duration=$(get_duration "$output_file")

        # Compare the durations
        if [ "$(awk "BEGIN {print ($flac_duration == $converted_duration)}")" -eq 1 ]; then
            return
        else
            echo "Source and converted file have different durations ($flac_duration != $converted_duration), proceeding with conversion."
        fi
    fi

    # Set ffmpeg options based on codec and quality mode
    ffmpeg_opts=("-y" "-loglevel" "quiet" "-i" "$flac_file" "-c:a" "$audio_codec")

    if [[ "$codec" == "opus" ]]; then
        # Opus supports different VBR/CBR modes
        case "$quality_mode" in
            "cbr")         ffmpeg_opts+=("-vbr" "off" "-b:a" "$bitrate") ;;
            "vbr")         ffmpeg_opts+=("-vbr" "on" "-b:a" "$bitrate") ;;
            "constrained") ffmpeg_opts+=("-vbr" "constrained" "-b:a" "$bitrate") ;;
            *)             ffmpeg_opts+=("-vbr" "on" "-b:a" "$bitrate") ;; # Default to VBR
        esac
    else
        # Vorbis is always VBR (no CBR mode)
        ffmpeg_opts+=("-q:a" "$bitrate")  # Adjust quality level (0-10, where 4 ≈ ~128k)
    fi

    # Convert FLAC to selected format
    echo "Converting ${flac_file} to ${output_file}"
    ffmpeg "${ffmpeg_opts[@]}" "$output_file"
}

export -f convert_flac  # Export the function to be available to xargs
export -f get_duration # Export the function to be available to xargs

# Find all FLAC files and prepare the list
find "$input_dir" -type f -name "*.flac" -print0 | \
  # Run the conversion in parallel using xargs with null separator
  xargs -0 -I {} -n 1 -P "$num_threads" bash -c 'convert_flac "$@"' _ {} "$input_dir" "$output_dir" "$codec" "$bitrate" "$quality_mode"

