# ACTNow FPGA Harness

Requirements-only Verilog/Vivado harness for putting the generated ActNow
`core4` on a KR260, fed by a SciDVS camera over asynchronous AER.

```
SciDVS -> ECP3 -> KR260 PL -> aer_rx -> evt_pack -> evt_stream -> core4.fifo_push
                                                              core4.io_* -> DMA -> PS -> UDP -> host
```

The event word ABI is:

```
[31] pad, [30:24] x, [23:17] y, [16:1] timestep, [0] polarity
```

Everything is driven by the `Makefile` here; run it from `harness/`.

## Layout

- `static/` - hand-written PL RTL around the generated core.
- `sim/` - `tb_core.v` and the requirements-only `tb_pl.v`.
- `fpga/tcl/` - Vivado project, BD, synth, impl, and XSA export scripts.
- `fpga/xdc/` - KR260 AER pin constraints.
- `pynq/actnow_fpga_server.py` - KR260-side overlay/firmware/DMA/UDP server.
- `host/actnow_client.py` - host-side SSH/SCP launcher and UDP viewer.

## PL Modules

| module | role |
|---|---|
| `aer_rx_simple.sv` / `aer_rx_wrap.v` | 4-phase AER receiver and decoded event tap |
| `evt_pack.v` | packs decoded AER events into the required 32-bit ABI |
| `evt_stream.v` | elastic FIFO into the core; drops when full so AER is never stalled |
| `axis_pack_fifo.v` | packetizes streams with `tlast` for DMA completion |
| `rom_bram_adapter.v` | serves `core4.rom_*` from PS-written firmware BRAM |
| `io_axis_adapter.v` | converts `core4.io_*` writes to the result AXI stream |
| `reset_ext_send.v` | turns GPIO bit 0 into one `reset_ext` send |
| `actnow_core_wrap.v` | generated core plus ROM/reset/output adapters |
| `actnow_pl.v` | full simulable PL excluding the Vivado block design |
| `fpga_top.v` | `actnow_pl` plus the BD wrapper |

The important hardware rule is that the camera-facing AER receiver is never
backpressured. If the core cannot accept events quickly enough, `evt_stream`
drops and counts them instead of stalling the AER handshake.

## Simulate

```sh
make sim
make pl
make boot fifo reset reset_reload
```

`make pl` boots `software/application` from the behavioral firmware BRAM, sends
real 4-phase AER events, checks the required event-word layout at the core input,
and verifies the result stream returns the firmware's rotated event words.

## Build

```sh
make fpga
make project synth impl xsa
```

The Vivado BD contains the KR260 PS, one S2MM AXI-DMA for core results, a
PS-writable firmware BRAM, and AXI GPIO for reset/status.

## Run

Build a firmware image, then launch from the host:

```sh
python3 host/actnow_client.py \
  --listen-host <HOST_IP> \
  --xsa fpga/vivado/actnow.xsa \
  --firmware ../software/build/rom.mem
```

The host client copies `pynq/actnow_fpga_server.py`, the XSA, and the firmware to
`ubuntu@kria.local`, starts the server with `sudo` after sourcing
`/etc/profile.d/pynq_venv.sh`, then renders UDP result words locally. Use
`--headless` to print rates without opening a viewer.
