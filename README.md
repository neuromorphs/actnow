# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Implemented ISA

The full RV32I base integer set is implemented in `core/soc.act`, including
loads (LB/LH/LW/LBU/LHU) and stores (SB/SH/SW), both routed through the MMU
(`core/mmu.act`) to either internal RAM (`core/peripherals/mem.act`) or
external memory (`addr_ext`/`mode_ext`/`wdata_ext`/`rdata_ext`). Loads
sign/zero-extend in `core/soc.act` after the MMU's masking; stores rely on
the MMU masking the write value down to the requested size before it
reaches the peripheral. The M-extension (multiply/divide) is not
implemented.

## Address-routed bus (`core/mmu.act`)

`core/mmu.act`'s `mmu` is a generic template, `mmu<N_EXACT; EXACT_BASES[N_EXACT];
CATCHALL_MIN_BASE>`: `N_EXACT` downstream ports are selected by an exact
match against `EXACT_BASES[k]`, and one further downstream port (index
`N_EXACT`, the last one) catches any address with `base >=
CATCHALL_MIN_BASE` that didn't already match. An address matching neither is
silently dropped — reads are never answered, writes are absorbed — which is
what a real reserved/unmapped address should do (undefined, but doesn't hang
waiting on a response). Two instantiations of the same template are used:

- **soc's own core-to-peripheral MMU** (inside `core/soc.act`): 2 exact routes
  (`ADDR_MEM`=0 → internal RAM, `ADDR_INT_CTRL`=1 → interrupt controller)
  plus a catch-all at `base >= ADDR_EXT_MIN` (4) → soc's own
  `addr_ext`/`mode_ext`/`wdata_ext`/`rdata_ext` ports. Bases 2 and 3 fall in
  the gap and are unreachable by construction.
- **a plain address demux** with no catch-all at all (pass `ADDR_NO_CATCHALL`
  for `CATCHALL_MIN_BASE`, which — since `addr_t`'s base field is only
  `WIDTH_ADDR_BASE` bits wide — can never actually match, so the router's
  last downstream port just goes unused). `tests/e2e/e2e_fifo_test.act` reuses
  the same `mmu` this way to split soc's external bus further into distinct
  peripherals: ROM at base=4, input FIFO at base=5, output FIFO at base=6.

## Interrupt controller (`core/interrupt.act`)

16 maskable event lines (`event_id_0`..`15`), each with its own
software-configured vector register: a real, memory-mapped table at
`base=ADDR_INT_CTRL`, word-addressed (offset `4*N` → the vector for
`event_id_N`). Software writes the address of its own ISR into `vectors[N]`
once — typically during startup, before ever going to sleep — and when
`event_id_N` later fires, `pc` jumps to whatever was last written there. This
is what makes interrupt vectoring work regardless of where the running
program actually lives (XIP from ROM, or copied into internal SRAM by the
bootloader under `BOOT=1`) — the address is a runtime value the program
supplies itself, not a constant baked into the hardware. The reset vector is
the one exception: fixed at `ADDR_RESET`, since it fires before software has
had any chance to configure anything (matching real hardware, where the
reset vector is fixed in silicon).

Each event line also has an **enable bit** (a 32-bit mask register at
`ADDR_INT_CTRL_ENABLE`, offset 64, bit N gates `event_id_N`). Until software
sets it, `core/interrupt.act` doesn't even offer to receive on that channel — so
whatever's driving it (a real device, or a testbench) just blocks at the
rendezvous rather than being serviced with a not-yet-configured vector. This
is what makes "wait for the program to finish booting" self-managed instead
of needing a guessed delay: fire the interrupt any time, even at simulated
time 0, and it'll naturally wait for the program's own vector-then-enable
sequence — see `tests/e2e/e2e_fifo_test.act`, which does exactly that.

## FIFO peripherals (`core/peripherals/fifo_in.act` / `core/peripherals/fifo_out.act`)

Fixed-depth circular-buffer FIFOs, each memory-mapped as a single data
register (the address offset is ignored — there's only one meaningful
register). Both guard their external port (`push`/`pop`) on the queue's fill
count as a top-level, re-evaluated-every-iteration alternative in a probed
selection — safe from deadlock, unlike gating the CPU-facing port the same
way would be (see the comments in both files). `tests/peripherals/fifo_test.act` is a
standalone unit test for both, independent of `soc`.

**`fifo_in<DEPTH>`**: an external `push` port feeds it (e.g. a testbench
simulating an external device); the CPU pops the oldest entry on every read
(`assert`s if empty — see the file for why blocking isn't safe here).
CPU writes configure a **trigger level** instead of being rejected: once
`count` reaches it, `fifo_in` fires its own `event_out` port — wired
directly to one of `soc`'s `event_id_N` inputs, so filling the FIFO to the
configured level *is* what raises the interrupt, no separate triggering
needed anywhere. Pushes are gated on the trigger level having been
explicitly configured at least once, so a producer pushing before software
configures it genuinely blocks (rather than silently counting against a
default it doesn't know about) — the same self-managed-synchronization
pattern as the interrupt controller's enable bit.

**Pitfall:** `event_out!true` is a plain blocking send. If `count` ever
reaches `trigger_level` and nothing is wired to `event_out`, that send blocks
forever — silently deadlocking `fifo_in` entirely (stuck mid-push, unable to
service any further transaction, CPU or testbench). Anything that doesn't
wire `event_out` up (e.g. because it's firing events manually instead, like
`tests/e2e/e2e_multi_event_test.act`) must configure `trigger_level` to something
unreachable (larger than `DEPTH`) — see `software/multi_event/main.c`'s
comment for a real example of getting this wrong and the fix.

**`fifo_out<DEPTH>`**: the CPU pushes on every write; an external `pop` port
drains it (e.g. for a testbench to observe what the CPU produced); CPU reads
are rejected via `assert`. A write to a full FIFO **blocks** (real
backpressure, like a hardware FIFO stalling the bus) rather than crashing —
it simply doesn't accept the transaction until `pop` makes room.

## End-to-end testbench (`tests/e2e/e2e_fifo_test.act`)

Models how the chip actually operates: bootloads a real compiled program
(`software/application/main.c`, built `BOOT=1`-style), lets it configure its
own interrupt vector and go to sleep, then fires two separate interrupts
(with a delay between them, modeling real-world latency), each carrying one
word of data through a real input FIFO, processed by the program's own ISR
(reads the input FIFO, adds 1, writes the output FIFO), and observed on a
real output FIFO. Run it with `make e2e_fifo_test` (or as part of `make
test`) — it has its own dedicated Makefile rule, since it needs the
`application` ROM image specifically and the shared
`$(FILE_REGISTRY_GEN)`/`$(ROM_IMAGE)` prerequisite (which only rebuilds once
per `make` invocation) can't guarantee that against an arbitrary `ROM_TEST`
default.

## Generality testbench (`tests/e2e/e2e_multi_event_test.act`)

Same shape as `e2e_fifo_test.act` (boot a real program, fire interrupts,
check FIFO output) but maxed out across the interrupt controller's full
width: `software/multi_event/main.c` registers a **distinct** ISR for every
one of the 16 maskable event lines (ISR N: `out = in + (N+1)`), then enables
all 16 via the enable mask in one write. The testbench fires all 16 events
in turn — one at a time, matching this architecture's lack of
preemption/concurrency — pushing a different value each time and checking
each ISR's distinct, correct response, to build confidence that vector
configuration, the enable mask, and dispatch genuinely work across the whole
controller, not just the one or two lines the other demos exercise.

Since `soc`'s `event_id_N` ports are individually named rather than an array,
the testbench wires each one into a local `chan(bool) event_ch[16]` so its
driving loop can index into it with a runtime variable — indexing a channel
array directly with a runtime value (`event_ch[int(i,4)]!(true)`) doesn't
compile ("dynamic channel arrays are unsupported"), so it goes through a
replicated selection instead: `[ ([]k:16: i = k -> event_ch[k]!(true)) ]`
(the outer `[...]` is required — a bare `(...)` around the replicated
selection fails to parse).

Run it with `make e2e_multi_event_test` (or as part of `make test`) — it has
its own dedicated Makefile rule for the same reason `e2e_fifo_test` does
(needs the `multi_event` ROM image specifically, and the shared
`$(ROM_IMAGE)` prerequisite only rebuilds once per `make` invocation).

## Running tests

Everything is driven by `make` from the project root (`actnow/`). There are two
layers of tests.

### Hardware testbenches (ACT/CHP)

The CHP testbenches are split by kind: `tests/core/` (CPU/ISA datapath —
hand-crafted instruction words, e.g. ALU, register file), `tests/peripherals/`
(standalone peripheral/infra unit tests — MMU, memory, FIFOs),
`tests/regression/` (one-off bug-repro tests), and `tests/e2e/` (full boot +
real compiled program + real peripheral interaction). Each reports
`<name>: PASS` or `FAIL`; `make` finds a test by name regardless of which
subdirectory it lives in.

```
make                 # build + run every test under tests/core, tests/peripherals, tests/sw
                      # (alias: make test -- also runs tests/e2e/* via their own rules)
make alu_test        # run a single testbench by name
make list            # list the discovered testbench names
```

### RV32I software tests (real programs through soc)

`tests/sw/rom_program_test.act` runs a *compiled* RV32I program through `soc`'s
real fetch/decode/execute pipeline (instead of hand-crafted instruction words),
serving the program image as external memory. Programs live in two places:

- `software/tests/unit/` — the official RISC-V suite (picorv32 riscv-tests),
  one `.S` per instruction.
- `software/tests/` — our own tests, `.S` or `.c` (compiled with rv32i gcc),
  e.g. `fib.c`.

**Run one program** through `soc` (prints the full simulator log). `ROM_TEST`
selects it by name; its image is (re)built automatically:

```
make ROM_TEST=simple rom_program_test   # the default smoke test
make ROM_TEST=addi   rom_program_test   # an official unit test
make ROM_TEST=fib    rom_program_test   # our custom fib.c
```

**Run the whole suite** — every RV32I program, each rebuilt and run in turn,
with a per-test PASS/FAIL line and a summary (exits non-zero if any fail):

```
make software-tests                       # all programs
make software-tests SW_TESTS="addi"       # just one, reported PASS/FAIL
make software-tests SW_TESTS="addi sub"   # a subset
```

The M-extension tests (`mul*`/`div*`/`rem*`) are skipped — this core decodes
only base RV32I.

**Run from internal memory (`BOOT=1`).** By default a program executes in place
from external ROM (a read-only `mem<true, ROM_IMAGE>` instance, slow). With
`BOOT=1` it is instead prepended with a small bootloader that copies it into
internal SRAM and jumps there, so it runs from fast internal memory. The flag
works with either runner:

```
make BOOT=1 ROM_TEST=addi rom_program_test   # one test, from internal memory
make BOOT=1 software-tests                    # whole suite, from internal memory
```

**`rom_program_test` only works for programs with no real MMIO.** It wires
`soc`'s entire external bus straight to one read-only `mem<true, ROM_IMAGE>`
instance (see `tests/sw/rom_program_test.act`) -- correct for plain
riscv-tests-style code (internal RAM + code fetch only), but any program that
writes to a real peripheral address (base >= 4) will hit
`ASSERTION failed: mem: write attempted to read-only memory`, since there's no
demux there to route base=5/6 to an actual FIFO. `application` and
`multi_event` (`software/application/`, `software/multi_event/`) both do real
`FIFO_IN`/`FIFO_OUT` writes, so **don't** run them via `ROM_TEST=<name>
rom_program_test` -- use `make e2e_fifo_test` / `make e2e_multi_event_test`
instead, which wire up the real ROM@4 / FIFO_IN@5 / FIFO_OUT@6 demux these
programs actually need (see `tests/e2e/e2e_fifo_test.act`). Both are always
bootloader-loaded regardless of `BOOT` -- see below.

`ROM_TEST=application` builds `software/application/main.c` (a generic C program,
not a self-checking test); it is always bootloader-loaded. See
`software/bootloader/` and `software/common/{bootloader,application}.lds`.

**Adding a new app-style program.** Any `software/<name>/` directory with its
own two-line Makefile (`PROG := <name>; include ../common/program.mk`) is
automatically treated as an app-style, bootloader-driven build with
`ROM_TEST=<name>` — no top-level `Makefile` changes needed. `application` and
`multi_event` are both just instances of this convention.

Building a program needs an rv32i cross-compiler; the `Makefile` auto-detects a
`riscv32-`/`riscv64-unknown-elf-` prefix (override with `make CROSS=...`).
Generating the file registry (below) builds the default program image, so the
cross-compiler is required the first time any test runs.

## How the software tests work

**Program image + file registry.** A program is assembled/compiled and turned
into a memory image, `software/tests/build/rom_image.mem` — one `0b`-prefixed
32-bit word per line (the prefix is what actsim's file reader needs to parse
binary). Its path is managed by the **file registry**
(`tests/files/file_registry.txt` + `tools/gen_file_registry.py`): the image is
registered as `ROM_IMAGE`, which the generator turns into a file-id constant
(`gen/file_ids.act`) and a `name_table` entry (`gen/file_registry.conf`, passed
to `actsim` via `-cnf`). `rom_program_test` opens it with
`sim::file::openr(ROM_IMAGE)` — no hand-edited config. `rom_image.mem` is a
single shared slot; the `ROM_TEST` and `software-tests` targets rebuild it as
you switch programs.

**Pass/fail signalling.** Reaching WFI means the program ran to completion. The
riscv-tests convention (`common/test_start.S`) is `WFI` = pass, `EBREAK` = fail
(emitted by a `TEST_CASE` comparison that didn't match). `core/soc.act` halts on
EBREAK via the same path as WFI, but logs a distinct `EBREAK -- test FAILED`
line (with the failing `TESTNUM`/`x28` value just above it), which the
`Makefile` greps for alongside `ASSERTION failed`. So a software test passes
only if it reaches WFI with no EBREAK and no assertion failure.

## Toolchain

Built and simulated against the `act`/`actsim` toolchain (`asyncvlsi/act`).
Requires `actsim` built from commit `fa1a636` ("tests and fixes for
user-defined enums") or later — earlier versions crash (`Assertion: pos ==
nvals` in `state.cc`) the instant a `deftype` struct with an enum-typed field
(e.g. this project's `mode_mem_t`) is sent over a channel, which
`core/mmu.act`/`core/peripherals/mem.act` do on every memory transaction.
`tests/regression/mode_mem_t_enum_bug_test.act` is a standalone regression test for it. If
your `actsim` predates the fix, update the `actsim` submodule in your
`act`/`actflow` checkout to `origin/master` and rebuild just `act` + `actsim`.

**Always compile and run from the project root (`actnow/`), never from inside
`tests/`.** ACT resolves every `import` path relative to the compiler's working
directory, not the importing file — so `core/soc.act`'s own `import
"core/interrupt.act"` only resolves when the whole compilation runs with
`actnow/` as the working directory. `make` handles this; to drive a testbench
by hand (what `make <name>` does under the hood) -- substitute the right
subdirectory (`tests/core/`, `tests/peripherals/`, `tests/regression/`,
`tests/sw/`, or `tests/e2e/`) for `<name>`'s actual location:

```
cd actnow
make file-registry                                              # once: generate gen/
aflat tests/peripherals/<name>.act
actsim -cnf=gen/file_registry.conf tests/peripherals/<name>.act <name>
```

At the `actsim` prompt, `cycle` runs to completion, `quit` exits.

## Balloon area 15%
## Sky130 tech file config
