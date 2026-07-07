# ACTNow FPGA Harness

This directory contains the Verilog/Vivado harness for building the ACTNow
`soc` process on an FPGA.

## Layout

- `convert_verilog.sh` generates Verilog from ACT/CHP with `chp2fpga`.
- `gen/` contains generated RTL such as `soc.v`, `func.v`, and `arbiter.v`.
- `static/` contains hand-written wrapper RTL. `fpga_top.v` instantiates `soc`.
- `fpga/tcl/` contains Vivado batch scripts.
- `fpga/vivado/` is Vivado output and is ignored by git.

## Generate RTL

Run from this directory:

```sh
./convert_verilog.sh
```

Defaults:

- process: `soc`
- source ACT file: `../soc.act`
- output directory: `gen/`

Override them when needed:

```sh
./convert_verilog.sh -p soc -f soc.act -o gen
```

## Vivado Flow

Run Vivado from this directory so the Tcl relative paths resolve correctly:

```sh
vivado -mode batch -nojournal -nolog -source fpga/tcl/create_project.tcl
vivado -mode batch -nojournal -nolog -source fpga/tcl/run_synth.tcl
vivado -mode batch -nojournal -nolog -source fpga/tcl/run_impl.tcl
```

Or run the full project-to-implementation flow:

```sh
./run_vivado_flow.sh
```

The project is created as `fpga/vivado/actnow_proj.xpr`, with reports under
`fpga/vivado/reports/`.

## Todos

- [ ] Test setup
- [ ] Block design: input/output FIFO, program ROM, interrupt generator
- [ ] Pynq integration of DVS camera
