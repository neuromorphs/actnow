#!/bin/sh
# Convert a raw little-endian binary into the hardware simulator's memory image:
# one 32-bit word per line, printed big-endian as 32 binary digits ('0'/'1').
# Trailing bytes are zero-padded up to a full word.
od -An -v -tu1 "$1" | awk '
{ for (i = 1; i <= NF; i++) b[n++] = $i }
END {
    while (n % 4) b[n++] = 0
    for (i = 0; i < n; i += 4) {
        w = b[i] + b[i+1]*256 + b[i+2]*65536 + b[i+3]*16777216
        s = ""
        for (bit = 31; bit >= 0; bit--) s = s (int(w / 2^bit) % 2)
        print s
    }
}'
