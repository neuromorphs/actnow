#!/bin/sh
# Stitch a bootloader image and an application blob into a single ROM image:
#
#     [ bootloader.bin ][ 4-byte little-endian length ][ application.bin ]
#
# The bootloader (running XIP from ROM) reads the length at the first ROM
# address past its own image, copies that many bytes into SRAM, and jumps there.
#
# Usage: stitch.sh <bootloader.bin> <application.bin> <out.bin>
set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: stitch.sh <bootloader.bin> <application.bin> <out.bin>" >&2
    exit 2
fi

boot=$1
app=$2
out=$3

cat "$boot" > "$out"
# 4-byte little-endian length header for the application blob.
len=$(wc -c < "$app")
printf '%b' \
    "$(printf '\\%03o' $((len & 255)) $((len >> 8 & 255)) \
                       $((len >> 16 & 255)) $((len >> 24 & 255)))" >> "$out"
cat "$app" >> "$out"
