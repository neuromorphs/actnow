#!/usr/bin/env bash
# Run the tb_core scenarios through iverilog/vvp instead of Vivado
# (xvlog/xelab/xsim aren't available everywhere Vivado is missing).
#
# Usage: harness/sim/run_iverilog.sh [-n] [boot|fifo|reset|reset_reload|all]
#   -n   skip regenerating gen/*.v (chp2fpga) -- just recompile/rerun
#        whatever's already there.
#
# By default this runs `make rtl` first, i.e. re-invokes chp2fpga and
# overwrites harness/gen/*.v from the current ACT source. If you've hand
# -patched a generated file (see harness/sim/BUGS_verilog_sim.md), that
# patch is gone after this -- pass -n to skip regeneration and keep it.
#
# Requires: iverilog/vvp (brew install icarus-verilog), riscv64-unknown-elf-gcc,
# and a chp2fpga binary (set CHP2FPGA=/path/to/chp2fpga if it's not
# /share/fpga_proto/chp/chp2fpga.x86_64_linux6_17_0 -- convert_verilog.sh's
# own default).
#
# excludes gen/actnow_core_core1.v -- an unused/orphaned duplicate of
# gen/actnow_core_cpu1.v that chp2fpga sometimes leaves stale between runs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GEN="$ROOT/harness/gen"
TB="$ROOT/harness/sim/tb_core.v"
BUILD="$ROOT/harness/sim/iverilog_build"
CROSS=riscv64-unknown-elf-

REGEN=1
if [[ "${1:-}" == "-n" ]]; then
  REGEN=0
  shift
fi

if [[ "$REGEN" == "1" ]]; then
  echo "--- regenerating harness/gen/*.v (make rtl) ---"
  make -C "$ROOT/harness" rtl
fi

mkdir -p "$BUILD"
GEN_SRCS=$(ls "$GEN"/*.v | grep -v func.v | grep -v actnow_core_core1.v)

run_scenario() {
  name=$1; test_id=$2; prog=$3; prog_b=${4:-}

  rm -f "$ROOT/software/build/rom.mem"
  make -s -C "$ROOT/software" PROG="$prog" CROSS="$CROSS"
  cp "$ROOT/software/build/rom.mem" "$BUILD/rom.mem"

  if [[ -n "$prog_b" ]]; then
    rm -f "$ROOT/software/build/rom.mem"
    make -s -C "$ROOT/software" PROG="$prog_b" CROSS="$CROSS"
    cp "$ROOT/software/build/rom.mem" "$BUILD/rom_b.mem"
  fi

  echo "--- $name: compiling ---"
  iverilog -g2012 -I "$GEN" -Ptb_core.TEST=$test_id -Ptb_core.TIMEOUT_NS=2000000 \
    -o "$BUILD/${name}.vvp" $GEN_SRCS "$TB"

  echo "--- $name: running ---"
  ( cd "$BUILD" && vvp "${name}.vvp" | tee "${name}.log" )

  if grep -q "FAIL" "$BUILD/${name}.log"; then
    echo "$name: FAIL"; return 1
  elif ! grep -q "PASS:" "$BUILD/${name}.log"; then
    echo "$name: FAIL (no completion)"; return 1
  else
    echo "$name: PASS"
  fi
}

case "${1:-all}" in
  boot)          run_scenario boot 0 boot_only ;;
  fifo)          run_scenario fifo 1 application ;;
  reset)         run_scenario reset 2 application ;;
  reset_reload)  run_scenario reset_reload 3 hang application ;;
  all)
    run_scenario boot 0 boot_only
    run_scenario fifo 1 application
    run_scenario reset 2 application
    run_scenario reset_reload 3 hang application
    ;;
  *) echo "usage: $0 [boot|fifo|reset|reset_reload|all]"; exit 1 ;;
esac
