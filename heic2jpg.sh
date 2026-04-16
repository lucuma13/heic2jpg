#!/bin/bash

# heic2jpg - A quick way to convert HEIC images to JPG.
readonly HEIC2JPG_VERSION="1.1"

# Copyright (c) 2026 Luis Gómez Gutiérrez
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

function show_help() {
	echo "heic2jpg v$HEIC2JPG_VERSION. A quick way to convert HEIC images to JPG."
	echo
	echo "Usage: heic2jpg [options] <path>"
	echo
	echo "Options:"
	echo "  -q [1-100]	: Compression quality (default = 30)"
	echo "  -h			: Show this help message"
	echo "  -v			: Verbose"
	echo "  --version	: Print version"
	exit 0
}

function get_abs_path() {
    local user_path="${1:-.}"
    if [[ -d "$user_path" ]]; then
        (cd "$user_path" && pwd)
    elif [[ -f "$user_path" ]]; then
        echo "$(cd "$(dirname "$user_path")" && pwd)/$(basename "$user_path")"
    else
        echo "$user_path"
    fi
}

# Long-format flags
[[ "$1" == "--version" ]] && { echo "$HEIC2JPG_VERSION"; exit 0; }
[[ "$1" == "--help" ]] && show_help

# Short-format flags
quality=30
verbose=false

while getopts "q:vh" option; do
	case $option in
		v) verbose=true ;;
		q)
			# Validate that the input is a number
			if [[ $OPTARG =~ ^[0-9]+$ ]] && [ "$OPTARG" -ge 1 ] && [ "$OPTARG" -le 100 ]; then
				quality=$OPTARG
			else
				echo "Error: Quality must be a number between 1 and 100." >&2
				exit 1
			fi
			;;
		h) show_help ;;
		*) show_help ;;
	esac
done
shift "$((OPTIND-1))"

# Resolve the absolute path (if relative path was provided)
src=$(get_abs_path "${1:-$(pwd)}")

# --- Execution ---

if [[ -f "$src" ]]; then
	[[ $verbose == true ]] && echo "Converting '$src' at $quality% quality..."
	target_jpg="$(dirname "$src")/$(basename "$src" ."${src##*.}").jpg"
	magick "$src" -auto-orient -strip -quality "$quality" "$target_jpg"
	if [ $? -eq 0 ] && [ -f "$target_jpg" ]; then
		rm "$src"
		[[ $verbose == true ]] && echo "Done."
	else
		echo "Error: Conversion failed or output file missing for $src" >&2
		exit 1
	fi
elif [[ -d "$src" ]]; then
	(
		cd "$src" || exit 1

		# Check if any HEIC files exist to avoid "mogrify: pattern not found" errors
		shopt -s nullglob
		files=(*.[hH][eE][iI][cC])
		
		if (( ${#files[@]} > 0 )); then
			[[ $verbose == true ]] && echo "Converting ${#files[@]} files at $quality% quality..."
			if magick mogrify -auto-orient -strip -quality "$quality" -format jpg "${files[@]}"; then
				rm -f "${files[@]}"
				[[ $verbose == true ]] && echo "Done."
			else
				echo "Error: Conversion failed. Originals preserved." >&2
				exit 1
			fi
		else
			echo "No HEIC files found in: $src" >&2
			exit 2
		fi
	)
else
	echo "Error: '$src' is not a valid file or directory." >&2
	exit 1
fi
