# ACTNow FPGA Harness

This directory contains the Verilog/Vivado harness for building the ACTNow
`soc` process on an FPGA.

Everything below is driven by the `Makefile` in this directory (`make help` for
the full list); run it from here, since the Tcl scripts address the project
relative to Vivado's working directory.

## Layout

- `Makefile` drives everything: RTL generation, simulation, and the Vivado flow.
- `convert_verilog.sh` generates Verilog from ACT/CHP with `chp2fpga`.
- `gen/` contains generated RTL such as `soc.v`, `func.v`, and `arbiter.v`.
- `static/` contains hand-written wrapper RTL. `fpga_top.v` instantiates `soc`.
- `sim/` contains the RTL testbench (`tb_core.v`) and its xsim build directory.
- `fpga/tcl/` contains Vivado batch scripts.
- `fpga/vivado/` is Vivado output and is ignored by git.

## Generate RTL

```sh
make rtl
```

This runs `convert_verilog.sh` (process `core<4>`, source `chips/fpga/core.act`,
output `gen/`) with the **patched** `chp2fpga` — the stock build miscompiles this
core in four separate ways, all documented in `sim/BUG2_mem_word_truncation.md`.
Override the binary with `make rtl CHP2FPGA=...`, or call the script directly for
a different process/source:

```sh
./convert_verilog.sh -p soc -f core/soc.act -o gen
```

## Simulate

```sh
make sim          # all four scenarios
make boot         # or one at a time: boot / fifo / reset / reset_reload
```

Each scenario is the RTL analogue of the same-named end-to-end test under
`chips/fpga/tests/e2e/` — same chip, same compiled program, same stimulus, with
the outside world (ROM, `rom_selector`, the base-6 output FIFO, the FIFO pushes)
modelled in `sim/tb_core.v` instead of CHP:

| scenario | ACT test | program |
|---|---|---|
| `boot` | `e2e_fpga_boot_test` | `software/boot_only`, XIP from the raw ROM route, run to WFI |
| `fifo` | `e2e_fpga_fifo_test` | `software/application`, two interrupt/FIFO batches |
| `reset` | `e2e_fpga_reset_test` | `software/application`, batch → external reset → batch |
| `reset_reload` | `e2e_fpga_reset_reload_test` | `software/hang` → flip ROM bank → reset → `software/application` |

`TRACE_ROM=1` logs every ROM fetch; `TIMEOUT_NS=` bounds a run.

## Vivado Flow

```sh
make project      # create fpga/vivado/actnow_proj.xpr (static/ + gen/ + block design)
make synth        # -> fpga/vivado/reports/post_synth_*.rpt
make impl         # -> fpga/vivado/reports/post_route_*.rpt
make xsa          # -> fpga/vivado/actnow.xsa (hardware platform, bitstream included)
make fpga         # all four, end to end
```

`make xsa` runs `write_hw_platform -fixed -include_bit`, i.e. the handoff to the
software side (Vitis / PetaLinux / a PYNQ overlay), with the bitstream embedded.
Target part is `xck26-sfvc784-2LV-c` (KR260).

Note that the flow currently implements an **empty PL**: `static/fpga_top.v` has
no ports and ties every one of the core's channels idle, so synthesis optimizes
the core away (0 LUTs, 0 registers in the utilization report). It builds and
exports cleanly, but there is nothing in the fabric until the block design below
actually drives the core's `rom_*` / `fifo_push` / `io_*` groups.

## Todos

- [x] Test setup (`make sim` — four RTL scenarios matching chips/fpga's e2e tests)
- [ ] Block design: input/output FIFO, program ROM, interrupt generator
- [ ] Pynq integration of DVS camera
