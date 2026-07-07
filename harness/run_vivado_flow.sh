#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

vivado_bin="${VIVADO:-vivado}"
vivado_flags=(-mode batch -nojournal -nolog)

tcl_steps=(
  "fpga/tcl/create_project.tcl"
  "fpga/tcl/run_synth.tcl"
  "fpga/tcl/run_impl.tcl"
)

for tcl_step in "${tcl_steps[@]}"; do
  echo "==> Running ${tcl_step}"
  "$vivado_bin" "${vivado_flags[@]}" -source "$tcl_step"
done

echo "==> Vivado flow complete"
