# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Implemented ISA

Full RV32I base integer set, in `core/core.act`: loads (LB/LH/LW/LBU/LHU) and
stores (SB/SH/SW), routed through the MMU's (`core/mmu.act`) data port to
either internal RAM (`core/mem.act`) or external memory.
Instruction fetch goes through the MMU's separate instr port, which can read
the same RAM/external memory but can never write either. The M-extension
(multiply/divide) is not implemented.

Two distinct address routers exist, not one: `core/mmu.act` decides what's
on-chip, `core/peripherals/demux.act` splits whatever isn't.

## Address routers (`core/mmu.act` and `core/peripherals/demux.act`)

Both route on the same shape: `N_EXACT` downstream ports selected by an exact
match against `EXACT_BASES[k]`, plus one optional catch-all port for any
address with `base >= CATCHALL_MIN_BASE`. An address matching neither is
silently dropped (reads never answered, writes absorbed) — what a real
reserved/unmapped address should do.

- **`core/mmu.act`'s `mmu`** is core-facing: a real dual-port PMP (physical
  memory protection) design. An **instr** port (`addr_instr`/`mode_instr`/
  `rdata_instr` — no `wdata` channel, since fetch never writes) and a
  **data** port (`addr_data`/`mode_data`/`wdata_data`/`rdata_data` — full
  R/W/RMW), both routed by the same table onto one shared downstream array,
  so instruction and data genuinely share one physical RAM. A write is
  structurally impossible through the instr port. Instantiated inside
  `core/core.act` with 2 exact routes (internal RAM, interrupt controller)
  plus a catch-all that passes anything off-chip straight through to core's
  own `addr_ext`/etc boundary — needed since core always boots by fetching
  the reset vector from external ROM ("XIP").
- **`core/peripherals/demux.act`'s `demux`** is periphery-facing: it never
  talks to the core directly, only to whatever's already off-chip.
  `chips/bench/periphery.act` wires it to core's `addr_ext` boundary with no
  catch-all, splitting into ROM, input FIFO, output FIFO, and GPIO.

A non-on-chip access takes the path: core's fetch/load-store logic → `mmu`
(falls through its catch-all) → core's `addr_ext` boundary → `demux` → the
actual peripheral.

## Interrupt controller (`core/interrupt.act`)

`core<N_EVENTS>` and `interrupt<N_EVENTS>` are templated on the number of
physically wired event lines (`event_id[0]`..`event_id[N_EVENTS-1]`), each
with its own software-configured vector register: a memory-mapped table at
`base=ADDR_INT_CTRL`, word-addressed (offset `4*N` → the vector for
`event_id[N]`). Software writes its own ISR address into `vectors[N]` once,
and when `event_id[N]` fires, `pc` jumps to whatever was last written there —
works the same whether the program is running XIP from ROM or copied into
internal SRAM by the bootloader.

The vector table/enable register are always a fixed 16-slot map regardless of
`N_EVENTS` — the software-visible register layout never changes across chip
variants, only the number of physically wired lines does. Each chip variant
picks its own width: `chips/bench/soc.act` instantiates `core<16>` (the full
width), `chips/dvs/soc.act` uses `core<3>` (AER input + 2 GPIO lines), and
`chips/fpga/soc.act` uses `core<1>` (a single FIFO interrupt) — unused
vector-table slots just sit there unreferenced.

Each event line also has an **enable bit** (32-bit mask register at
`ADDR_INT_CTRL_ENABLE`, offset 64). Until software sets it, `interrupt.act`
doesn't even offer to receive on that channel — so whoever's driving it just
blocks at the rendezvous rather than being serviced with a not-yet-configured
vector. That's what makes "wait for the program to finish booting"
self-managed instead of needing a guessed delay.

The reset vector is the one exception: fixed at `ADDR_RESET`, since it fires
before software has had any chance to configure anything.

## External reset (`reset_ext`)

`core/core.act` exposes a `chan?(bool) reset_ext` port that puts the core back
into its boot state on demand, without restarting the simulation. The main
`chp` loop races `#reset_ext` as a flat sibling of "keep running" and "idle,
waiting for a wake-up" — not nested inside a catch-all — which is what lets
it recover a core that's genuinely hung (stuck in an infinite loop, never
reaching WFI) as well as one that's legitimately idle. Reset can't interrupt
an instruction already in flight, only between instructions.

Reset also clears `core/interrupt.act`'s vector table and enable mask,
relayed via a second internal channel (a channel send can only be received
once, so `core.act` can't fan `reset_ext` out to both itself and
`interrupt.act` directly). Register file contents are *not* cleared, matching
real RISC-V semantics.

See `tests/core/reset_test.act` for the hand-assembled scenario, and
`chips/bench/tests/e2e/e2e_reset_test.act` for the same thing through a real
compiled program.

## FIFO peripherals (`core/peripherals/fifo_in.act` / `core/peripherals/fifo_out.act`)

Fixed-depth circular-buffer FIFOs, each memory-mapped as a single data
register.

**`fifo_in<DEPTH, WIDTH>`**: an external `push` port feeds it (`WIDTH` bits
per word — `WIDTH_DATA` for a generic 32-bit producer, or narrower for a
domain-specific one); the CPU pops the oldest entry on every read, always
zero-extended to the full 32-bit CPU-facing word. CPU writes configure a
**trigger level** instead of being rejected: once `count` reaches it,
`fifo_in` fires its own `event_out` port, wired to one of core's `event_id[N]`
inputs — so filling the FIFO to the configured level *is* what raises the
interrupt. Pushes are gated on the trigger level having been explicitly
configured at least once.

**Pitfall:** `event_out!true` is a plain blocking send. If nothing is wired
to it when `count` reaches `trigger_level`, this deadlocks `fifo_in`
entirely. Anything that fires events manually instead (like
`chips/bench/tests/e2e/e2e_multi_event_test.act`) must configure
`trigger_level` to something unreachable.

**`fifo_out<DEPTH>`**: the CPU pushes on every write; an external `pop` port
drains it. CPU reads are rejected via `assert`. A write to a full FIFO
**blocks** (real backpressure) rather than crashing.

## GPIO (`core/peripherals/gpio.act`)

A single MMIO output register (address offset ignored) whose low 4 bits
drive 4 physical output pins. A CPU write updates the register and re-drives
all 4 pins in the same transaction; a CPU read returns the last-written
value. Pins are 4 individually-named `chan!(bool)` ports rather than an
array — each one is a genuinely distinct physical wire, unlike core's own
`event_id[N]` array port.

GPIO *input* has no dedicated peripheral — `chips/bench/soc.act` wires two of
`core<16>`'s event lines (`event_id[14]`/`event_id[15]`) straight out as
`gpio_in_0`/`gpio_in_1`. An input pin going high is exactly an
interrupt-controller event: it uses the existing vector table, no separate
hardware needed.

## `chips/bench/` — the simulation test chip

`chips/bench/periphery.act` assembles the off-chip periphery (ROM + `fifo_in`
+ `fifo_out` + `gpio`, behind a `demux`) that a chip variant needs.
`chips/bench/soc.act` wires that together with `core/core.act` into one
chip, exposing:

- `event_id_0`..`event_id_13`: generic pass-through event lines.
- `reset_ext`, `fifo_event` (fifo_in's auto-fire, left for the caller to
  route or ignore), `push`/`pop` (the FIFOs).
- 8 GPIO pins: `gpio_in_0`/`gpio_in_1` (`event_id[14]`/`event_id[15]`),
  `swd_0`/`swd_1` (reserved for future debug support, unimplemented),
  `gpio_out_0`..`gpio_out_3`.

## End-to-end tests (`chips/bench/tests/e2e/`)

Each boots a real compiled program through the real bootloader and exercises
one path end to end. Run any of them with `make <name>` (or all via
`make test`):

- **`e2e_boot_test`** (`software/boot_only`) — boot, zero peripheral
  interaction, reach WFI. The baseline every other scenario builds on.
- **`e2e_fifo_test`** (`software/application`) — two batches through the
  input FIFO, processed by a real ISR, observed on the output FIFO.
- **`e2e_multi_event_test`** (`software/multi_event`) — fires all 16 event
  lines in turn, each with its own ISR, proving the interrupt controller
  works across its full width.
- **`e2e_multi_event_reset_test`** (`software/multi_event`) — same, but
  back-to-back with no artificial delay, split across a `reset_ext`
  boundary.
- **`e2e_reset_test`** (`software/application`) — one batch, external reset,
  a second batch, proving the same bootloader+program combination reboots
  cleanly.
- **`e2e_reset_reload_test`** (`software/hang` + `software/application`) —
  boot a genuinely broken program, flip a `rom_selector` bank, reset into a
  corrected one.
- **`e2e_gpio_test`** (`software/gpio_demo`) — fires each GPIO input pin,
  checks the resulting pattern on the GPIO output pins.

`e2e_reset_reload_test` is the one exception that doesn't use
`chips/bench/soc.act`: it needs two independent backing ROMs behind
`core/peripherals/rom_selector.act`, which `periphery.act`'s single-ROM
parameterization doesn't accommodate.

## Other chip variants' e2e suites (`chips/dvs/`, `chips/fpga/`)

`chips/bench/` above is the generic simulation chip. `chips/dvs/` (AER event
sensor + SPI boot/programming interface) and `chips/fpga/` (the real
FPGA-bound variant) are separate chip variants shaped the same way --
`soc.act` + `periphery.act` + their own `tests/e2e/` + their own Makefile --
but **neither is wired into the root `make test`**. Run them explicitly, from
the project root same as everything else:

```
make -C chips/dvs test                     # every chips/dvs e2e test
make -C chips/fpga test                    # every chips/fpga e2e test
make -C chips/fpga e2e_fpga_rotate_test    # a single test by name
```

- **`chips/dvs/tests/e2e/`** — `e2e_boot_test`/`e2e_reset_test`/
  `e2e_gpio_test` mirror chips/bench's tests of the same name, against
  `software/dvs_application`'s bootloader+program combination.
  `e2e_aer_test`/`e2e_aer_stress_test` are the AER-input equivalent of
  `e2e_fifo_test`/`e2e_fifo_stress_test` above. `e2e_spi_read_test` and
  `e2e_dvs_probe_all` exercise `spi_prog`'s read direction and raw
  transaction decoding. `e2e_full_test` chains the whole pipeline: boot XIP
  out of `spi_boot`, load data via `spi_prog`, then run it.
- **`chips/fpga/tests/e2e/`** — boot/fifo/reset tests mirroring chips/bench's,
  plus rotate/track/motion/timesurface/denoise tests that replay real
  recorded DVS captures (`chips/fpga/data/`, see below) through each
  program's ISR and assert against reference values. Not every `.act` file
  here is wired into `make -C chips/fpga test` — the `*_capture` variants in
  particular are meant to be driven interactively by `harness/host/
  dvs_*_live.py` (which regenerate the `*_capture_results.mem` scratch files
  those same scripts visualize), not run as part of the automated suite; see
  `chips/fpga/Makefile`'s own `test:` target for the current list.

## ROM bank selector (`core/peripherals/rom_selector.act`)

A 2-way mux between two backing `core/peripherals/rom.act` (`rom<file_id>`)
instances, routing whichever is "active" to a single CPU-facing ROM port.
Which bank is active is chosen by `flip_bank`, entirely independent of
`reset_ext` — reset never
carries a target address or program identity; it always just reboots from
whatever's currently mapped at `ADDR_RESET`. Models a real dual-bank-boot
flash: something else (an operator, an OTA update) flips the bank, and reset
is oblivious to which one it lands on.

## Running tests

Everything is driven by `make` from the project root (`actnow/`).

### Hardware testbenches (ACT/CHP)

Split by kind: `tests/core/` (CPU/ISA datapath), `tests/peripherals/`
(standalone peripheral/infra unit tests), `tests/regression/` (one-off
bug-repro tests), `tests/sw/` (real-program-through-core runner), and
`chips/bench/tests/e2e/` (full boot + real compiled program + real
peripheral interaction, delegated to via `chips/bench/Makefile`). Each
reports `<name>: PASS` or `FAIL`; `make` finds a test by name regardless of
subdirectory.

```
make                 # build + run every test
make alu_test        # run a single testbench by name
make list             # list the discovered testbench names
```

### RV32I software tests (real programs through core)

`tests/sw/rom_program_test.act` runs a *compiled* RV32I program through
`core`'s real fetch/decode/execute pipeline, serving the program image as
external memory. Programs live in two places:

- `software/tests/unit/` — the official RISC-V suite (picorv32 riscv-tests),
  one `.S` per instruction.
- `software/tests/` — our own tests, `.S` or `.c`, e.g. `fib.c`.

**Run one program** (prints the full simulator log):

```
make ROM_TEST=simple rom_program_test   # the default smoke test
make ROM_TEST=addi   rom_program_test   # an official unit test
make ROM_TEST=fib    rom_program_test   # our custom fib.c
```

**Run the whole suite**:

```
make software-tests
make software-tests SW_TESTS="addi"
make software-tests SW_TESTS="addi sub"
```

The M-extension tests (`mul*`/`div*`/`rem*`) are skipped — this core decodes
only base RV32I.

**Run from internal memory (`BOOT=1`).** By default a program executes in
place from external ROM (slow). With `BOOT=1` it's prepended with a small
bootloader that copies it into internal SRAM and jumps there:

```
make BOOT=1 ROM_TEST=addi rom_program_test
make BOOT=1 software-tests
```

**`rom_program_test` only works for programs with no real MMIO** — it wires
core's entire external bus to one read-only ROM instance, no demux. Programs
that do real FIFO/GPIO writes (`application`, `multi_event`, `gpio_demo`,
`boot_only`, `hang`) run through `chips/bench/`'s e2e tests instead, always
bootloader-loaded regardless of `BOOT`.

**Adding a new app-style program.** Any `software/<name>/` directory with its
own two-line Makefile (`PROG := <name>; include ../common/program.mk`) is
automatically treated as an app-style, bootloader-driven build with
`ROM_TEST=<name>` — no top-level `Makefile` changes needed.

Building a program needs an rv32i cross-compiler; the `Makefile`
auto-detects a `riscv32-`/`riscv64-unknown-elf-` prefix (override with
`make CROSS=...`).

## How the software tests work

**Program image + file registry.** A program is assembled/compiled into
`software/tests/build/rom_image.mem` — one `0b`-prefixed 32-bit word per
line. Its path is managed by the file registry
(`tests/files/file_registry.txt` + `tools/gen_file_registry.py`), which
generates `gen/file_ids.act` and `gen/file_registry.conf` (passed to
`actsim` via `-cnf`).

**Pass/fail signalling.** Reaching WFI means the program ran to completion.
The riscv-tests convention is `WFI` = pass, `EBREAK` = fail (with the
failing `TESTNUM`/`x28` value logged just above it). A software test passes
only if it reaches WFI with no EBREAK and no assertion failure.

## Naming & layout conventions

- **`chips/<variant>/periphery.act`** (not `harness.act`) is each chip
  variant's off-chip periphery assembly — `defproc periphery`, instantiated
  as `p` in that variant's own `soc.act` (e.g. `chips/bench/periphery.act`,
  `chips/dvs/periphery.act`, `chips/fpga/periphery.act`). Renamed from
  `harness.act` to stop colliding with the unrelated top-level `harness/`
  (the physical FPGA/Vivado bring-up flow — `harness/fpga`, `harness/static`,
  `harness/host`, `harness/dashboard`) — same vocabulary, different concepts;
  `chips/` and `harness/` (top-level) stay separate and unrenamed.
- **`chips/fpga/data/`** holds the tracked DVS capture recordings
  (`dvs_capture_20260714_151049.csv`, `phone.csv`, `stabilize.csv`) that the
  FPGA e2e rotate/track/motion/timesurface/denoise tests and
  `harness/host/dvs_*_live.py` replay from — split out from `chips/fpga/`'s
  source/build files (`soc.act`, `periphery.act`, `Makefile`) so recorded
  fixtures don't sit flat alongside them. The generated `*_capture_*.mem`
  scratch files those scripts also write stay directly under `chips/fpga/`
  (gitignored, not fixtures).
- **`core/mem.act`** (untemplated `mem`, internal read/write RAM) vs.
  **`core/peripherals/rom.act`** (`rom<file_id>`, file-preloaded and
  read-only) used to be one templated `defproc mem<READ_ONLY; file_id>` in
  `core/peripherals/mem.act`. Split so core's own internal SRAM (always
  read/write, no file) isn't carrying ROM-only template parameters it never
  uses; `core/mem.act` lives alongside `core/core.act` rather than under
  `peripherals/` since it's core's own internal memory, not an off-chip
  peripheral.

## Toolchain

Built and simulated against the `act`/`actsim` toolchain (`asyncvlsi/act`).
Requires `actsim` built from commit `fa1a636` or later — earlier versions
crash the instant a `deftype` struct with an enum-typed field (e.g.
`mode_mem_t`) is sent over a channel. `tests/regression/mode_mem_t_enum_bug_test.act`
is a standalone regression test for it.

**Always compile and run from the project root (`actnow/`), never from
inside `tests/`** — ACT resolves every `import` path relative to the
compiler's working directory, not the importing file.

```
cd actnow
make file-registry                                              # once: generate gen/
aflat tests/peripherals/<name>.act
actsim -cnf=gen/file_registry.conf tests/peripherals/<name>.act <name>
```

At the `actsim` prompt, `cycle` runs to completion, `quit` exits.

## Balloon area 15%
## Sky130 tech file config
