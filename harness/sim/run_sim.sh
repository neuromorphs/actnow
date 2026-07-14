#!/usr/bin/env bash
#
# Standalone functional RTL simulation of the converted fpga core (no block
# design). Builds the software/boot_only image, compiles the generated RTL +
# tb_core.v with the Xilinx simulator, and runs the core to WFI.
#
# Usage:  ./run_sim.sh            (from anywhere)
#   CROSS=<prefix>   override the RISC-V cross-compiler prefix
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # harness/sim
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                    # harness
REPO_ROOT="$(cd "$HARNESS_DIR/.." && pwd)"                     # actnow
GEN_DIR="$HARNESS_DIR/gen"
BUILD_DIR="$SCRIPT_DIR/xsim_build"

# RISC-V cross-compiler prefix (same auto-detection as the top-level Makefile).
if [[ -z "${CROSS:-}" ]]; then
    for p in riscv32-unknown-elf- riscv64-unknown-elf- riscv-none-elf-; do
        if command -v "${p}gcc" >/dev/null 2>&1; then CROSS="$p"; break; fi
    done
    CROSS="${CROSS:-riscv64-unknown-elf-}"
fi

echo "==> Building software/boot_only image (CROSS=$CROSS)"
make -C "$REPO_ROOT/software" PROG=boot_only CROSS="$CROSS"

mkdir -p "$BUILD_DIR"
# tb_core.v $readmemb's "rom.mem" from the sim working directory.
cp "$REPO_ROOT/software/build/rom.mem" "$BUILD_DIR/rom.mem"

# Generated RTL, minus func.v -- func.v is a bare set of functions that every
# module textually `include`s, so it must not be compiled as its own unit.
mapfile -t GEN_SRCS < <(find "$GEN_DIR" -maxdepth 1 -name '*.v' ! -name 'func.v' | sort)

cd "$BUILD_DIR"

echo "==> xvlog (compile ${#GEN_SRCS[@]} generated modules + tb_core.v)"
# -i GEN_DIR so each module's `include "func.v"` resolves.
xvlog --relax -i "$GEN_DIR" "${GEN_SRCS[@]}" "$SCRIPT_DIR/tb_core.v"

echo "==> xelab tb_core"
xelab tb_core -relax -s tb_core_snap --timescale 1ns/1ps

echo "==> xsim (run to completion)"
xsim tb_core_snap -R
