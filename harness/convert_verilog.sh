#!/usr/bin/env bash
#
# Generate Verilog from a CHP/ACT source using chp2fpga.
#
# Usage:
#   ./convert_verilog.sh [-p process] [-f act_file] [-o output_dir]
#
#   -p  top-level process name to expand (default: soc)
#   -f  ACT source file containing the process, relative to the repo root
#       (default: core/soc.act)
#   -o  directory to write the generated Verilog into, relative to this
#       script's directory (default: gen)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PROCESS="soc"
ACT_FILE="core/soc.act"
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

# chp2fpga only expands processes given with explicit (possibly empty)
# template angle brackets, e.g. "soc<>" rather than just "soc", even
# when the process itself takes no template parameters.
if [[ "$PROCESS" != *"<"* ]]; then
    PROCESS="${PROCESS}<>"
fi

# chp2fpga resolves imports relative to its cwd (not the ACT file's own
# directory) and segfaults if given an absolute -o path, so both the ACT
# file and the output dir must be passed as relative paths. Every .act file
# in this repo writes its own imports relative to the repo root (e.g.
# core/soc.act's "import core/globals.act" -- see the top-level Makefile's
# own comment on this), so chp2fpga must be run with cwd=REPO_ROOT, not the
# ACT file's own directory -- cd'ing into core/ instead would make
# core/soc.act's "core/globals.act" import resolve to core/core/globals.act.
REL_ACT_FILE="$(realpath --relative-to="$REPO_ROOT" "$ACT_FILE")"
REL_OUT_DIR="$(realpath --relative-to="$REPO_ROOT" "$OUT_DIR")"

# -a emits the round-robin arbiter (module rr) into <out>/arbiter.v. It is
# only needed for non-deterministic selection, but the generated processes
# instantiate rr unconditionally, so pass it always to avoid a missing-module
# error at synth/sim time. arbiter.v must be added to the downstream file list.
echo "convert_verilog.sh: (cd \"$REPO_ROOT\" && chp2fpga -a -p \"$PROCESS\" \"$REL_ACT_FILE\" -o \"$REL_OUT_DIR/\")"
(cd "$REPO_ROOT" && chp2fpga -a -p "$PROCESS" "$REL_ACT_FILE" -o "$REL_OUT_DIR/")
