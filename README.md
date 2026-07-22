# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Implemented ISA

Full RV32I base integer set, in `assets/core/cpu.act`: loads (LB/LH/LW/LBU/LHU) and
stores (SB/SH/SW), routed through the MMU's (`assets/core/mmu.act`) data port to
either internal RAM (`assets/core/mem.act`) or external memory.
Instruction fetch goes through the MMU's separate instr port, which can read
the same RAM/external memory but can never write either. The M-extension
(multiply/divide) is not implemented.

Two distinct address routers exist, not one: `assets/core/mmu.act` decides what's
on-chip, `assets/peripherals/demux.act` splits whatever isn't.

## Address routers (`assets/core/mmu.act` and `assets/peripherals/demux.act`)

Both route on the same shape: `N_EXACT` downstream ports selected by an exact
match against `EXACT_BASES[k]`, plus one optional catch-all port for any
address with `base >= CATCHALL_MIN_BASE`. An address matching neither is
silently dropped (reads never answered, writes absorbed) — what a real
reserved/unmapped address should do.

- **`assets/core/mmu.act`'s `mmu`** is core-facing: a real dual-port PMP (physical
  memory protection) design. An **instr** port (`addr_instr`/`mode_instr`/
  `rdata_instr` — no `wdata` channel, since fetch never writes) and a
  **data** port (`addr_data`/`mode_data`/`wdata_data`/`rdata_data` — full
  R/W/RMW), both routed by the same table onto one shared downstream array,
  so instruction and data genuinely share one physical RAM. A write is
  structurally impossible through the instr port. Instantiated inside
  `assets/core/cpu.act` with 2 exact routes (internal RAM, interrupt controller)
  plus a catch-all that passes anything off-chip straight through to core's
  own `addr_ext`/etc boundary — needed since core always boots by fetching
  the reset vector from external ROM ("XIP").
- **`assets/peripherals/demux.act`'s `demux`** is periphery-facing: it never
  talks to the core directly, only to whatever's already off-chip.
  `chips/bench/periphery.act` wires it to core's `addr_ext` boundary with no
  catch-all, splitting into ROM, input FIFO, output FIFO, and GPIO.

A non-on-chip access takes the path: core's fetch/load-store logic → `mmu`
(falls through its catch-all) → core's `addr_ext` boundary → `demux` → the
actual peripheral.

## Interrupt controller (`assets/core/interrupt.act`)

`cpu<N_EVENTS>` and `interrupt<N_EVENTS>` are templated on the number of
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
picks its own width: `chips/bench/soc.act` instantiates `cpu<16>` (the full
width), `chips/dvs/soc.act` uses `cpu<3>` (AER input + 2 GPIO lines), and
`chips/fpga/soc.act` uses `cpu<1>` (a single FIFO interrupt) — unused
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

`assets/core/cpu.act` exposes a `chan?(bool) reset_ext` port that puts the core back
into its boot state on demand, without restarting the simulation. The main
`chp` loop races `#reset_ext` as a flat sibling of "keep running" and "idle,
waiting for a wake-up" — not nested inside a catch-all — which is what lets
it recover a core that's genuinely hung (stuck in an infinite loop, never
reaching WFI) as well as one that's legitimately idle. Reset can't interrupt
an instruction already in flight, only between instructions.

Reset also clears `assets/core/interrupt.act`'s vector table and enable mask,
relayed via a second internal channel (a channel send can only be received
once, so `cpu.act` can't fan `reset_ext` out to both itself and
`interrupt.act` directly). Register file contents are *not* cleared, matching
real RISC-V semantics.

See `tests/core/reset_test.act` for the hand-assembled scenario, and
`chips/bench/tests/e2e/e2e_reset_test.act` for the same thing through a real
compiled program.

## FIFO peripherals (`assets/peripherals/fifo_in.act` / `assets/peripherals/fifo_out.act`)

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

## GPIO (`assets/peripherals/gpio.act`)

A single MMIO output register (address offset ignored) whose low 4 bits
drive 4 physical output pins. A CPU write updates the register and re-drives
all 4 pins in the same transaction; a CPU read returns the last-written
value. Pins are 4 individually-named `chan!(bool)` ports rather than an
array — each one is a genuinely distinct physical wire, unlike core's own
`event_id[N]` array port.

GPIO *input* has no dedicated peripheral — `chips/bench/soc.act` wires two of
`cpu<16>`'s event lines (`event_id[14]`/`event_id[15]`) straight out as
`gpio_in_0`/`gpio_in_1`. An input pin going high is exactly an
interrupt-controller event: it uses the existing vector table, no separate
hardware needed.

## Assembling a chip (core + periphery)

Every chip variant under `chips/<variant>/` is the same two-file pattern: a
`periphery.act` (the off-chip peripheral set, behind a `demux`) and a
`soc.act` (`cpu<N_EVENTS>` + that `periphery`, wired together). Building a
new variant means writing both.

1. **Decide what you're modeling.** A chip variant isn't just "a core with
   some peripherals" — it stands in for a real (or simulated) piece of
   hardware, and that choice drives everything else: what peripherals exist,
   how boot works (embedded ROM vs. SPI flash vs. raw pass-through to an
   external block), and how many event lines are physically wired. See
   "Chip variants" below for what `chips/bench`, `chips/dvs`, and
   `chips/fpga` each model.

2. **Pick `N_EVENTS`.** `cpu<N_EVENTS>` (`assets/core/cpu.act`) only wires
   up `event_id[0]`..`event_id[N_EVENTS-1]` as real ports — the vector
   table/enable register inside `assets/core/interrupt.act` are always a
   fixed 16-slot map regardless, so unused slots just sit unreferenced. Pick
   `N_EVENTS` to match how many lines are actually wired for this variant
   (`chips/bench` uses `cpu<16>` to exercise the controller's full width,
   `chips/dvs` uses `cpu<3>`, `chips/fpga` uses `cpu<1>`), not a default of
   16.

3. **Write `periphery.act`.** Instantiate `demux<N, BASES[], CATCHALL_MIN_BASE>`
   (`assets/peripherals/demux.act`) and give it one route per peripheral,
   wiring each route's `addr`/`mode`/`wdata`/`rdata` to the peripheral's own
   ports:
   - Route bases must be `>= ADDR_EXT_MIN` (4) — bases 0 (`ADDR_MEM`) and 1
     (`ADDR_INT_CTRL`) are reserved for `mmu`'s on-chip routing and never
     reach a periphery-level demux. Beyond that, each variant is free to
     assign its own bases (`chips/bench` uses 4/5/6/`ADDR_GPIO`(7),
     `chips/dvs` uses `ADDR_SPI_BOOT`(4)/`ADDR_AER`(5)/`ADDR_SPI_PROG`(6)/
     `ADDR_GPIO`(7)) — there's no single canonical map across variants.
   - Pass `ADDR_NO_CATCHALL` unless something genuinely needs to fall
     through to a further-off-chip destination the demux can't see (none of
     the three existing variants' peripheries do — `mmu`'s catch-all, one
     level up, is what already routes anything non-on-chip out to
     `addr_ext`).
   - A peripheral instance can be a real file-preloaded `rom<file_id>`
     (`assets/peripherals/rom.act`), or — if the actual device lives further
     off-chip (an FPGA's own BRAM, an AXI bus) — just the bare demux route
     channels exposed straight through to the periphery's own boundary with
     no peripheral instantiated at all (see `chips/fpga/periphery.act`'s
     `rom_addr`/`rom_mode`/`rom_rdata`, base 4).
   - Available peripheral building blocks: `rom<file_id>`,
     `fifo_in<DEPTH, WIDTH>`, `fifo_out<DEPTH>`, `gpio`, `rom_selector`,
     `spi_boot`, `spi_prog` (all under `assets/peripherals/`).

4. **Write `soc.act`.** Instantiate `cpu<N_EVENTS>` and your `periphery`,
   then:
   - Wire core's `addr_ext`/`mode_ext`/`wdata_ext`/`rdata_ext` straight to
     periphery's `addr_in`/`mode_in`/`wdata_in`/`rdata_in` — this is the
     entire on-chip/off-chip boundary, always a 1:1 wire-up.
   - Wire each `core.event_id[k]` to whatever should raise that interrupt:
     a peripheral's own auto-fire port (`fifo_in`'s `event_out`, `dvs`'s
     `aer_event`), a raw external pin (a GPIO input line), or left as a
     pass-through channel for a test/testbench to drive manually
     (`chips/bench`'s `event_id_0`..`event_id_13`). Each is a single
     rendezvous channel — decide up front whether it's peripheral-driven or
     externally-driven, since only one sender can ever be wired to it.
   - Wire `reset_ext`. Nothing in `cpu` runs until this is asserted once —
     there's no implicit power-on-and-go — and it's also the only way to
     warm-reboot without restarting the simulation (see "External reset"
     above).
   - Expose whatever ports the outside world actually needs: real chip-package
     pins for a hardware-bound variant (`chips/dvs`'s `spi_boot_*`/
     `spi_prog_*`), or simulation-only test hooks for a bench-style variant
     (`chips/bench`'s `push`/`pop`).

5. **Namespace it.** `namespace actnow { export namespace chips { export
   namespace <variant> { ... } } }`, matching the directory path
   (`chips/<variant>/*.act` → `actnow::chips::<variant>`) — see "ACT
   namespaces" below. `export` is required at every level or nothing outside
   the file can see it.

6. **Give it its own test harness.** Only `chips/bench` is wired into the
   root `make test`; every other variant needs its own `Makefile` +
   `tests/e2e/` (mirroring `chips/bench/Makefile`'s structure) and is run
   explicitly with `make -C chips/<variant> test`.

Naming note: the off-chip assembly file is always `periphery.act`
(`defproc periphery`, instantiated as `p`), never `harness.act` — that name
collides with the unrelated top-level `harness/` (the physical FPGA/Vivado
bring-up flow). See "Naming & layout conventions" below.

## Chip variants

Three chip variants exist, each modeling a different target and each its own
`cpu<N_EVENTS>` + `periphery` pair assembled per the recipe above:

- **`chips/bench/`** — a generic simulation testbench, not a model of any
  specific real chip. `cpu<16>` (the full event-line width, so tests can
  drive the interrupt controller across its entire vector table) plus ROM +
  input FIFO + output FIFO + GPIO behind a 4-way demux. Boots XIP from an
  embedded, file-preloaded `rom<ROM_IMAGE>`. This is the variant almost
  every test in `tests/` and `chips/bench/tests/e2e/` runs against, and the
  only one wired into the root `make test`. See below for what it exposes.
- **`chips/dvs/`** — models a real AER (address-event) sensor chip: an event
  camera's pixel-event stream comes in through `aer_in` straight to
  `event_id[0]` (hardwired, since nothing ever needs to fire it manually),
  plus two GPIO input lines (`event_id[1]`/`event_id[2]`), so `cpu<3>` is
  enough. Boots via `spi_boot` (SPI flash XIP) rather than an embedded ROM,
  and `spi_prog` is the chip's read/write data path to the outside world —
  there's no `fifo_out` in this variant at all. Its own e2e suite
  (`chips/dvs/tests/e2e/`) is run with `make -C chips/dvs test`.
- **`chips/fpga/`** — the real FPGA-bound variant, meant to be run through
  `chp2fpga` (see `harness/`) and synthesized onto actual hardware. `cpu<1>`
  — just the input FIFO's auto-fire — and periphery is deliberately minimal:
  a *raw* pass-through ROM route (base 4, since the actual ROM is FPGA BRAM
  living outside the chip, not a file-preloaded `rom<...>`), the input event
  FIFO (base 5), and a *raw* read/write IO route (base 6, bound to
  PS/AXI/PL pins on the Vivado side, not decided here). Its e2e suite
  (`chips/fpga/tests/e2e/`) includes the rotate/track/motion/timesurface/
  denoise tests that replay real recorded DVS captures
  (`chips/fpga/data/`) — run with `make -C chips/fpga test`.

## `chips/bench/` — the simulation test chip

`chips/bench/periphery.act` assembles the off-chip periphery (ROM + `fifo_in`
+ `fifo_out` + `gpio`, behind a `demux`) that a chip variant needs.
`chips/bench/soc.act` wires that together with `assets/core/cpu.act` into one
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
`assets/peripherals/rom_selector.act`, which `periphery.act`'s single-ROM
parameterization doesn't accommodate.

## Other chip variants' e2e suites (`chips/dvs/`, `chips/fpga/`)

`chips/dvs/` and `chips/fpga/` (see "Chip variants" above for what each
models) are shaped the same way as `chips/bench/` -- `soc.act` +
`periphery.act` + their own `tests/e2e/` + their own Makefile -- but
**neither is wired into the root `make test`**. Run them explicitly, from
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

## ROM bank selector (`assets/peripherals/rom_selector.act`)

A 2-way mux between two backing `assets/peripherals/rom.act` (`rom<file_id>`)
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
`cpu`'s real fetch/decode/execute pipeline, serving the program image as
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
- **`assets/core/mem.act`** (untemplated `mem`, internal read/write RAM) vs.
  **`assets/peripherals/rom.act`** (`rom<file_id>`, file-preloaded and
  read-only) used to be one templated `defproc mem<READ_ONLY; file_id>` in
  `assets/peripherals/mem.act`. Split so core's own internal SRAM (always
  read/write, no file) isn't carrying ROM-only template parameters it never
  uses; `assets/core/mem.act` lives alongside `assets/core/cpu.act` rather than under
  `assets/peripherals/` since it's core's own internal memory, not an off-chip
  peripheral.

## ACT namespaces

Every `defproc`/`deftype`/`defenum`/`function` in this repo lives in one of
four namespaces (mirroring the directory layout the rest of this doc refers
to by file path):

```
actnow::core                -- cpu.act, mmu.act, regfile.act, interrupt.act,
                                utils.act, mem.act
actnow::core::peripherals    -- demux, fifo_in, fifo_out, gpio, rom,
                                rom_selector, spi_boot, spi_prog
actnow::chips::bench         -- chips/bench/{soc,periphery}.act
actnow::chips::dvs           -- chips/dvs/{soc,periphery}.act
actnow::chips::fpga          -- chips/fpga/{soc,periphery}.act
```

So the CPU process (`defproc cpu` in `assets/core/cpu.act`) is really
`actnow::core::cpu`, the `soc` in `chips/fpga/soc.act` is
`actnow::chips::fpga::soc`, and so on — this doc keeps using the short names
throughout since the file path already disambiguates which one is meant.
(The process is named `cpu`, not `core`, specifically to avoid colliding
with its own enclosing `actnow::core` namespace — `actnow::core::core` was
confusing to read and to say out loud.)

**Why:** before namespaces, `chips/bench/soc.act`, `chips/dvs/soc.act`, and
`chips/fpga/soc.act` each declared a global `defproc soc(...)` with a
different signature — same for `periphery`. That only worked because no
single build ever imported more than one chip variant at once; the first
time something needed two side by side, it would have been a duplicate-name
compile error. Each chip variant now gets its own namespace instead.

**Mechanics, if you're adding a new file:**
- `export` is required at *every* level of the namespace path for something
  to be usable outside the file that defines it — including a plain
  top-level `deftype`/`defenum` with no enclosing `namespace` block at all
  (see `assets/core/globals.act`'s `addr_t`/`mode_mem_t`/etc, all `export`ed even
  though they're not wrapped in any namespace). Forgetting `export` at any
  level fails with `Type is not exported up the namespace hierarchy: ...`.
  `pint` constants (`WIDTH_DATA`, `ADDR_*`, ...) are the one exception —
  they're visible everywhere unconditionally, no `export` needed.
- A namespace automatically sees its *parent's* exported names with no
  `open` needed (e.g. `assets/peripherals/demux.act`, in
  `actnow::core::peripherals`, calls `utils.act`'s `mask_data` — declared in
  the enclosing `actnow::core` — directly). Anything else needs
  `open actnow::core;` / `open actnow::core::peripherals;` etc. at the top
  of the file, after the `import`s — see any file under `chips/*/` or
  `tests/` for the pattern.
- `chp2fpga` (the RTL generator — see `harness/`) handles namespaced process
  names fine: it flattens `::` to `_` in both the generated module name and
  filename (e.g. `actnow::chips::fpga::soc<4>` → module/file
  `actnow_chips_fpga_soc4`), so no special-casing is needed there.
- `tests/` and each chip's own `tests/e2e/` stay un-namespaced — nothing
  else ever imports them, so there's no collision to solve and namespacing
  them would only add ceremony.

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
