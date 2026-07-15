# ACTNow FPGA Harness

The Verilog/Vivado harness that puts the ActNow core on a KR260, fed by a SciDVS
event camera over asynchronous AER, with two streams going back to a host:

```
 SciDVS ─GAER─▶ ECP3 ─async AER─▶ KR260 PL                                       PS            host
                                  aer_rx ─▶ evt_pack (+timestamp)
                                               ├──▶ evt_stream ──▶ DMA ──▶ DDR ──▶ UDP :3333 ──▶ viewer (raw)
                                               │
                                               └──▶ evt_stream ──▶ ActNow core<4>
                                                    (fifo_in fills → interrupt →
                                                     ISR → writes base 6)
                                                            └─────▶ DMA ──▶ DDR ──▶ UDP :3334 ──▶ viewer (processed)
```

Stream A is `kr260_aer_interface`'s path, unchanged in behaviour. Stream B is the
same events routed through the async core and back out.

Everything is driven by the `Makefile` here (`make help`); run it from this
directory, since the Tcl scripts address the project relative to Vivado's cwd.

**No part of the DVS path has run on hardware yet.**

## Layout

- `Makefile` — RTL generation, simulation, and the Vivado flow.
- `convert_verilog.sh` — generates Verilog from ACT/CHP with `chp2fpga`.
- `gen/` — generated RTL (`core4.v`, `soc.v`, `arbiter.v`, …).
- `static/` — hand-written PL RTL (see below).
- `sim/` — testbenches (`tb_core.v`, `tb_pl.v`) and the xsim build dir.
- `fpga/tcl/`, `fpga/xdc/` — Vivado batch scripts and pin constraints.
- `fpga/vivado/` — Vivado output (gitignored).
- `pynq/` — what runs on the KR260's PS.
- `BD_AER_BRAINSTORM.md` — the design options and the decisions taken.

### The PL (`static/`)

| module | what it does |
|---|---|
| `aer_rx_simple.sv` / `aer_rx_wrap.v` | the 4-phase AER receiver + word-serial decode, from `kr260_aer_interface`, plus a live event tap |
| `evt_pack.v` | packs `{ts[16:0], pol, y[6:0], x[6:0]}` — the low 15 bits are exactly the old `last_event`, so the host parser is unchanged |
| `evt_stream.v` | per-consumer: decimation, elastic FIFO, **drop** when full |
| `axis_pack_fifo.v` | the FIFO + packetizer behind both streams (`tlast` so a DMA transfer can complete) |
| `rom_bram_adapter.v` | serves the core's ROM route (base 4) from the PS-written firmware BRAM |
| `io_axis_adapter.v` | the core's base-6 writes → the result stream |
| `reset_ext_send.v` | an AXI-GPIO bit → one send on the core's `reset_ext` channel |
| `actnow_core_wrap.v` | the core plus those adapters |
| `actnow_pl.v` | the whole PL minus the block design (so it is simulable) |
| `fpga_top.v` | `actnow_pl` + the block design wrapper |

**The one rule the design is built around:** the AER bus has a single ACK line, so
no consumer may ever backpressure the receiver — stalling it would stall the
*camera*, and with it the raw stream. `evt_stream` therefore **drops** (and counts
drops) rather than stalling. The core's result path is the exception: it
backpressures, because a computed result must not be lost, and the core stalling
is harmless (the `evt_stream` in front of it absorbs it by dropping).

## Generate RTL

```sh
make rtl
```

Runs `convert_verilog.sh` (process `core<4>`, source `chips/fpga/core.act`) with
the **patched** `chp2fpga` — the stock build miscompiles this core in four ways,
all documented in `sim/BUG2_mem_word_truncation.md`.

## Simulate

```sh
make sim          # everything below
make pl           # the DVS datapath: AER in -> raw stream + core -> result stream
make boot         # or one core scenario: boot / fifo / reset / reset_reload
```

`make pl` is the one that says the DVS harness works: a behavioral ECP3 drives real
4-phase AER, the core boots `software/application` out of a behavioral firmware
BRAM, the interrupt fires when the input FIFO fills, and both output streams are
checked (raw = every event; results = each event the core was handed, +1).

The four core scenarios are the RTL analogues of the ACT e2e tests under
`chips/fpga/tests/e2e/` — same chip, same programs, same stimulus, with the
outside world modelled in `sim/tb_core.v` instead of CHP:

| scenario | ACT test | program |
|---|---|---|
| `boot` | `e2e_fpga_boot_test` | `software/boot_only`, XIP from the raw ROM route, run to WFI |
| `fifo` | `e2e_fpga_fifo_test` | `software/application`, two interrupt/FIFO batches |
| `reset` | `e2e_fpga_reset_test` | `software/application`, batch → external reset → batch |
| `reset_reload` | `e2e_fpga_reset_reload_test` | `software/hang` → flip ROM bank → reset → `software/application` |

## Build

```sh
make fpga         # project -> synth -> impl -> xsa
make project synth impl xsa      # or one step at a time
```

Produces `fpga/vivado/actnow.xsa` (`write_hw_platform -fixed -include_bit`) — the
PYNQ overlay. Target part `xck26-sfvc784-2LV-c` (KR260).

Current build: **17,373 LUTs (14.8%), 14,174 FF (6.1%), 4 BRAM, WNS +4.293 ns** at
the PS's PL clock (96.97 MHz — the preset does not land on exactly 100).

## Run (on the KR260)

```sh
python3 pynq/actnow_dvs_send.py --host <HOST_IP> --firmware rom.mem --decim 8
```

Loads the overlay from the XSA, writes the firmware image into the BRAM, pulses the
core's `reset_ext` (**the core does not self-boot: this pulse is what starts it, and
it is also how a firmware change takes effect — no bitstream rebuild**),
and pumps both DMAs out as UDP in `kr260_aer_interface`'s existing datagram format,
so its viewer works unchanged on either port:

```sh
python3 aer_udp_viewer.py --port 3333    # raw
python3 aer_udp_viewer.py --port 3334    # processed
```

## Todos

- [x] Test setup (`make sim`: four core scenarios + the full PL datapath)
- [x] Block design: AER in, event streams, program BRAM, the core's interrupt path
- [ ] The BATCH/decimation sweep, and a real ISR
- [ ] Bring-up on real hardware
- [ ] Pynq integration of DVS camera (the PS side exists; unverified on hardware)
