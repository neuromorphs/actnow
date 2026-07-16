# ACTNow FPGA Harness

Verilog/Vivado harness for putting the generated ActNow
`core4` on a KR260, fed by a SciDVS camera over asynchronous AER.

```
SciDVS -> ECP3 -> KR260 PL -> aer_rx -> evt_pack +-> evt_stream -> core4.fifo_push
                                                   |              core4.io_* -> result DMA -> UDP :3334
                                                   +-> raw FIFO -> raw DMA --------> UDP :3336
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
- `host/actnow_raw_viewer.py` - listener-only standalone raw DVS viewer.

## PL Modules

| module | role |
|---|---|
| `aer_rx_simple.sv` / `aer_rx_wrap.v` | 4-phase AER receiver and decoded event tap |
| `evt_pack.v` | packs decoded AER events into the required 32-bit ABI |
| `evt_stream.v` | independent elastic FIFOs for core and raw DMA; each drops locally when full |
| `axis_pack_fifo.v` | packetizes streams with `tlast` for DMA completion |
| `rom_bram_adapter.v` | serves `core4.rom_*` from PS-written firmware BRAM |
| `io_axis_adapter.v` | converts `core4.io_*` writes to the result AXI stream |
| `reset_ext_send.v` | turns GPIO bit 0 into one `reset_ext` send |
| `actnow_core_wrap.v` | generated core plus ROM/reset/output adapters |
| `actnow_pl.v` | full simulable PL excluding the Vivado block design |
| `fpga_top.v` | `actnow_pl` plus the BD wrapper |

The important hardware rule is that the camera-facing AER receiver is never
backpressured. The core and raw branches each have their own `evt_stream`; a
slow consumer increments only its branch's drop counter. Raw capture remains
active while core ingress is paused for firmware reload.

## Simulate

```sh
make sim
make pl
make boot fifo reset reset_reload
```

`make pl` boots `software/application` from the behavioral firmware BRAM, sends
real 4-phase AER events, checks the required event-word layout on both branches,
and verifies raw events continue while the core is paused. `make pl_stress`
checks both streams under a continuous event burst.

## Build

```sh
make fpga
make project synth impl xsa
```

The Vivado BD contains the KR260 PS, independent S2MM AXI-DMAs for processed and
raw events, a PS-writable firmware BRAM, and AXI GPIO for reset/status. The two
DMAs use separate PS high-performance ports and interrupts.

## Run

Build a firmware image, then launch from the host:

```sh
python3 host/actnow_client.py \
  --listen-host <HOST_IP> \
  --xsa fpga/vivado/actnow.xsa \
  --firmware ../software/build/rom.mem
```

From the repository root, run `make dashboard`. It builds the application and
dashboard dependencies, copies the server, XSA, and firmware to
`ubuntu@kria.local`, starts PYNQ under `sudo` after sourcing
`/etc/profile.d/pynq_venv.sh`, and opens the dashboard at
`http://127.0.0.1:8088`. The dashboard provides the processed DVS view, hardware
counters, C editing, Easy Mode transformations, and firmware hot reload without
reloading the XSA. It intentionally receives only processed UDP on port 3334.

While the dashboard/server is running, open the independent raw stream in a
second terminal:

```sh
make raw-viewer
```

This listens on UDP port 3336 and does not deploy, restart, or control the
KR260. Override the destination/listen port consistently with
`RAW_UDP_PORT=<port> make dashboard` and `RAW_UDP_PORT=<port> make raw-viewer`.
Both streams use the same `ACT1` UDP packet header and 32-bit event ABI, but
maintain independent packet sequence counters.

Use `make kria-headless` for a terminal rate monitor. The direct
`host/actnow_client.py` command remains available for diagnostics.
