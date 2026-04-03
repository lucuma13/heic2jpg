#!/bin/bash

# heic2jpg - A quick way to convert HEIC images to JPG.
readonly HEIC2JPG_VERSION="1.0"

# Copyright (C) 2026 Luis Gómez Gutiérrez
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

function h2j_usage() {
	echo "heic2jpg v$HEIC2JPG_VERSION. A quick way to convert HEIC images to JPG."
	echo
	echo "Usage: heic2jpg [options] <source>"
	echo
	echo "Options:"
	echo "  -q [1-100]	: Compression quality (default = 30)"
	echo "  -h			: Show this help message"
	echo "  -v			: Verbose"
	echo "  --version	: Print version"
	exit 0
}

function get_abs_path() {
	local h2j_path="${1:-.}"
	
	if [[ -d "$h2j_path" ]]; then
		(cd "$h2j_path" && pwd)
	elif [[ -f "$h2j_path" ]]; then
		echo "$(cd "$(dirname "$h2j_path")" && pwd)/$(basename "$h2j_path")"
	else
		local h2j_dir
		h2j_dir=$(dirname "$h2j_path")
		if [[ -d "$h2j_dir" ]]; then
			echo "$(cd "$h2j_dir" && pwd)/$(basename "$h2j_path")"
		else
			echo "$h2j_path"
		fi
	fi
}

# Long-format flags
[[ "$1" == "--version" ]] && { echo "$HEIC2JPG_VERSION"; exit 0; }
[[ "$1" == "--help" ]] && h2j_usage

# Short-format flags
h2j_quality=30
h2j_verbose=false

while getopts "hvq:" h2j_option; do
	case $h2j_option in
		h) h2j_usage ;;
		v) h2j_verbose=true ;;
		q)
			# Validate that the input is a number
			if [[ $OPTARG =~ ^[0-9]+$ ]] && [ "$OPTARG" -ge 1 ] && [ "$OPTARG" -le 100 ]; then
				h2j_quality=$OPTARG
			else
				echo "Error: Quality must be a number between 1 and 100." >&2
				exit 1
			fi
			;;
		*) h2j_usage ;;
	esac
done
shift "$((OPTIND-1))"

# Resolve the absolute path (if relative path was provided)
h2j_src=$(get_abs_path "${1:-$(pwd)}")

# --- Execution ---

if [[ -f "$h2j_src" ]]; then
	[[ $h2j_verbose == true ]] && echo "Converting '$h2j_src' at $h2j_quality% quality..."
	magick "$h2j_src" -auto-orient -strip -quality "$h2j_quality" "${h2j_src%.*}.jpg"
	if [ $? -eq 0 ] && [ -f "${h2j_src%.*}.jpg" ]; then
		rm "$h2j_src"
		[[ $h2j_verbose == true ]] && echo "Done."
	else
		echo "Error: Conversion failed or output file missing for $h2j_src" >&2
		exit 1
	fi
elif [[ -d "$h2j_src" ]]; then
	(
		cd "$h2j_src" || exit 1

		# Check if any HEIC files exist to avoid "mogrify: pattern not found" errors
		shopt -s nullglob
		h2j_files=(*.[hH][eE][iI][cC])
		
		if (( ${#h2j_files[@]} > 0 )); then
			[[ $h2j_verbose == true ]] && echo "Converting ${#h2j_files[@]} files at $h2j_quality% quality..."
			magick mogrify -auto-orient -strip -quality "$h2j_quality" -format jpg *.[hH][eE][iI][cC]
			h2j_results=(*.jpg)
			if [ $? -eq 0 ] && (( ${#h2j_results[@]} >= ${#h2j_files[@]} )); then
				rm -f *.[hH][eE][iI][cC]
				[[ $h2j_verbose == true ]] && echo "Done."
			else
				echo "Error: Conversion failed or file count mismatch. Originals preserved." >&2
				exit 1
			fi

		else
			echo "No HEIC files found in: $h2j_src" >&2
			exit 2
		fi
	)
else
	echo "Error: '$h2j_src' is not a valid file or directory." >&2
	exit 1
fi