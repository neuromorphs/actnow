#!/usr/bin/env bash
#
# Generate Verilog from a CHP/ACT source using chp2fpga.
#
# Usage:
#   ./convert_verilog.sh [-p process] [-f act_file] [-o output_dir]
#
#   -p  top-level process name to expand (default: actnow::chips::fpga::soc<4>)
#   -f  ACT source file containing the process, relative to the repo root
#       (default: chips/fpga/soc.act)
#   -o  directory to write the generated Verilog into, relative to this
#       script's directory (default: gen)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PROCESS="actnow::chips::fpga::soc<4>"
ACT_FILE="chips/fpga/soc.act"
OUT_DIR="$SCRIPT_DIR/gen"

usage() {
    grep '^#' "${BASH_SOURCE[0]}" | sed 's/^#//' | sed '1d'
    exit 1
}

while getopts "p:f:o:h" opt; do
    case "$opt" in
        p) PROCESS="$OPTARG" ;;
        f) ACT_FILE="$OPTARG" ;;
        o) OUT_DIR="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done

if [[ "$ACT_FILE" != /* ]]; then
    ACT_FILE="$REPO_ROOT/$ACT_FILE"
fi
if [[ "$OUT_DIR" != /* ]]; then
    OUT_DIR="$SCRIPT_DIR/$OUT_DIR"
fi

if [[ ! -f "$ACT_FILE" ]]; then
    echo "convert_verilog.sh: ACT file not found: $ACT_FILE" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

# chp2fpga needs explicit (possibly empty) template brackets to expand a
# process, e.g. "soc<>" rather than "soc", even with no template params.
if [[ "$PROCESS" != *"<"* ]]; then
    PROCESS="${PROCESS}<>"
fi

# chp2fpga resolves imports relative to its cwd, and segfaults on an absolute
# -o path -- so it must run with cwd=REPO_ROOT (every .act import in this repo
# is repo-root-relative) and both paths passed in relative. `realpath
# --relative-to` is GNU-only, so use python3 (available everywhere this
# script runs) instead of assuming BSD realpath or grealpath.
relative_to() {
    python3 -c 'import os, sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$1" "$2"
}
REL_ACT_FILE="$(relative_to "$ACT_FILE" "$REPO_ROOT")"
REL_OUT_DIR="$(relative_to "$OUT_DIR" "$REPO_ROOT")"

# Default to the patched chp2fpga build: the stock /usr/local/cad build drops
# data for enum/struct-of-enum channel payloads (e.g. mode_mem_t's op/size),
# stalling every op/size-dependent path in the generated RTL. Override with
# CHP2FPGA=... if needed (e.g. no /share mount, as on macOS).
CHP2FPGA="/Users/kamerongano/.local/act/bin/chp2fpga"

# -a always: it emits the round-robin arbiter (arbiter.v), which the
# generated processes instantiate unconditionally regardless of need.
# All flags must come before the positional .act file -- chp2fpga's parser
# stops reading flags at the first non-option argument.
echo "convert_verilog.sh: (cd \"$REPO_ROOT\" && \"$CHP2FPGA\" -a -p \"$PROCESS\" -o \"$REL_OUT_DIR/\" \"$REL_ACT_FILE\")"
(cd "$REPO_ROOT" && "$CHP2FPGA" -a -p "$PROCESS" -o "$REL_OUT_DIR/" "$REL_ACT_FILE")

# Fix a chp2fpga naming bug (stock and patched builds both have it): the
# event_pc guard in core's main loop is emitted as the bare `\event_pc_valid`,
# but the channel is only declared as `\inter.event_pc_valid` (its actual port
# alias). Without this, actnow_core_core*.v fails to compile. core is
# templated on N_EVENTS (actnow_core_core1.v, core2.v, ...), hence the glob.
#
# `-i.bak` + rm, not bare `-i<suffix>`: GNU sed takes the suffix with no
# space, BSD sed requires one (even empty) -- this form works on both.
# shopt -s nullglob
# for f in "$OUT_DIR"/actnow_core_core[0-9]*.v; do
#     sed -i.bak 's/(\\event_pc_valid )/(\\inter.event_pc_valid )/' "$f"
#     rm -f "$f.bak"
# done
# shopt -u nullglob

# # chp2fpga names array-templated modules/files with literal braces, e.g.
# # demux<3,{4,5,6},16> -> demux3{456}16.v. Vivado's Tcl file list rejects '{'
# # '}' in filenames, so sanitize braces to '_' in the module name (both its
# # definition and every instantiation) and the filename.
# shopt -s nullglob
# for f in "$OUT_DIR"/*'{'*.v; do
#     base="$(basename "$f" .v)"                 # e.g. demux3{456}16
#     safe="$(printf '%s' "$base" | tr '{}' '__')"   # e.g. demux3_456_16
#     for vf in "$OUT_DIR"/*.v; do
#         sed -i.bak "s/$base/$safe/g" "$vf"
#         rm -f "$vf.bak"
#     done
#     mv "$f" "$OUT_DIR/$safe.v"
# done
# shopt -u nullglob
