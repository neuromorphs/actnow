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
# assets/core/core.act's "import assets/core/globals.act" -- see the top-level Makefile's
# own comment on this), so chp2fpga must be run with cwd=REPO_ROOT, not the
# ACT file's own directory -- cd'ing into assets/core/ instead would make
# assets/core/core.act's "assets/core/globals.act" import resolve to assets/core/assets/core/globals.act.
# realpath --relative-to is a GNU coreutils extension, not available in
# macOS's BSD realpath (and this repo can't assume grealpath is installed),
# so compute the relative path with python3 instead -- present everywhere
# this script otherwise needs to run.
relative_to() {
    python3 -c 'import os, sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$1" "$2"
}
REL_ACT_FILE="$(relative_to "$ACT_FILE" "$REPO_ROOT")"
REL_OUT_DIR="$(relative_to "$OUT_DIR" "$REPO_ROOT")"

# Use the patched chp2fpga build by default: the stock /usr/local/cad build
# drops the data of channels whose payload is an enum/struct-of-enum (e.g.
# mode_mem_t's op/size), so every op/size-dependent path -- the demux's
# read/write routing, mem's size masking -- stalls in the generated RTL. The
# patched build emits those data buses (\*_mode.op / \*_mode.size). Override
# with CHP2FPGA=... if needed.
CHP2FPGA="${CHP2FPGA:-/share/fpga_proto/chp/chp2fpga.x86_64_linux6_17_0}"

# -a emits the round-robin arbiter (module rr) into <out>/arbiter.v. It is
# only needed for non-deterministic selection, but the generated processes
# instantiate rr unconditionally, so pass it always to avoid a missing-module
# error at synth/sim time. arbiter.v must be added to the downstream file list.
#
# chp2fpga's own argument parser isn't a real getopt -- it stops reading
# flags at the first non-option argument (the .act file), so any flag placed
# after it (e.g. -o) is silently missed and it falls back to "no act file
# given". All flags must come before the positional .act file.
echo "convert_verilog.sh: (cd \"$REPO_ROOT\" && \"$CHP2FPGA\" -a -p \"$PROCESS\" -o \"$REL_OUT_DIR/\" \"$REL_ACT_FILE\")"
(cd "$REPO_ROOT" && "$CHP2FPGA" -a -p "$PROCESS" -o "$REL_OUT_DIR/" "$REL_ACT_FILE")

# Post-generation fix for a chp2fpga naming bug (present in both the stock and
# patched builds): the internal `event_pc` channel probe in core's main loop is
# emitted as the bare name `\event_pc_valid`, but the channel is aliased to the
# `inter` subinstance port and only declared as `\inter.event_pc_valid`. Left
# unfixed, actnow_core_core*.v fails to compile ("event_pc_valid is not
# declared"). Only the guard expression uses the bare name; the port
# connection below it is correct. core is templated on N_EVENTS and lives in
# the actnow::core namespace, so chp2fpga names its output after the
# ::-joined-with-_ process path + template args (e.g. actnow_core_core1.v for
# actnow::core::core<1>) -- match any actnow_core_coreN.v instead of assuming
# an unqualified/untemplated name.
#
# sed -i's argument is a GNU/BSD portability landmine: GNU takes the suffix
# attached with no space (or none at all), BSD *requires* one (even empty).
# `-i.bak` + cleanup is the one form both accept identically.
shopt -s nullglob
for f in "$OUT_DIR"/actnow_core_core[0-9]*.v; do
    sed -i.bak 's/(\\event_pc_valid )/(\\inter.event_pc_valid )/' "$f"
    rm -f "$f.bak"
done
shopt -u nullglob

# chp2fpga derives module/file names from template array params, e.g.
# demux<3,{4,5,6},16> -> module \demux3{456}16 in demux3{456}16.v. Vivado's Tcl
# file list rejects '{' '}' in filenames (add_files: "Illegal file or directory
# name"), so sanitize braces to '_' in both the module-name token (everywhere it
# appears -- definition and instantiations) and the filename. Braces are matched
# literally in sed BRE, and these tokens never collide with Verilog {..}
# concatenations, so this is safe. (The older committed gen/ used this same
# brace-free convention, e.g. mmu2_01_4.v.)
shopt -s nullglob
for f in "$OUT_DIR"/*'{'*.v; do
    base="$(basename "$f" .v)"                 # e.g. demux3{456}16
    safe="$(printf '%s' "$base" | tr '{}' '__')"   # e.g. demux3_456_16
    for vf in "$OUT_DIR"/*.v; do
        sed -i.bak "s/$base/$safe/g" "$vf"
        rm -f "$vf.bak"
    done
    mv "$f" "$OUT_DIR/$safe.v"
done
shopt -u nullglob
