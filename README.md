# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Implemented ISA

The full RV32I base integer set is implemented in `soc.act`, including loads
(LB/LH/LW/LBU/LHU) and stores (SB/SH/SW), both routed through the MMU
(`mmu.act`) to either internal RAM (`mem.act`) or external memory
(`addr_ext`/`mode_ext`/`wdata_ext`/`rdata_ext`). Loads sign/zero-extend in
`soc.act` after the MMU's masking; stores rely on the MMU masking the write
value down to the requested size before it reaches the peripheral. The
M-extension (multiply/divide) is not implemented.

## Running tests

Everything is driven by `make` from the project root (`actnow/`). There are two
layers of tests.

### Hardware testbenches (ACT/CHP)

The CHP testbenches under `tests/*.act` exercise the individual blocks (ALU,
MMU, register file, memory) and the assembled `soc`. Each reports
`<name>: PASS` or `FAIL`.

```
make                 # build + run every tests/*.act  (alias: make test)
make alu_test        # run a single testbench by name
make list            # list the discovered testbench names
```

### RV32I software tests (real programs through soc)

`tests/rom_program_test.act` runs a *compiled* RV32I program through `soc`'s
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
from external ROM (`rom_backend`, slow). With `BOOT=1` it is instead prepended
with a small bootloader that copies it into internal SRAM and jumps there, so it
runs from fast internal memory. The flag works with either runner:

```
make BOOT=1 ROM_TEST=addi rom_program_test   # one test, from internal memory
make BOOT=1 software-tests                    # whole suite, from internal memory
make BOOT=1 ROM_TEST=application rom_program_test  # the software/application demo
```

`ROM_TEST=application` builds `software/application/main.c` (a generic C program,
not a self-checking test); it is always bootloader-loaded. See
`software/bootloader/` and `software/common/{bootloader,application}.lds`.

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
(emitted by a `TEST_CASE` comparison that didn't match). `soc.act` halts on
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
`mmu.act`/`mem.act` do on every memory transaction.
`tests/mode_mem_t_enum_bug_test.act` is a standalone regression test for it. If
your `actsim` predates the fix, update the `actsim` submodule in your
`act`/`actflow` checkout to `origin/master` and rebuild just `act` + `actsim`.

**Always compile and run from the project root (`actnow/`), never from inside
`tests/`.** ACT resolves every `import` path relative to the compiler's working
directory, not the importing file — so `soc.act`'s own `import "interrupt.act"`
only resolves when the whole compilation runs with `actnow/` as the working
directory. `make` handles this; to drive a testbench by hand (what
`make <name>` does under the hood):

```
cd actnow
make file-registry                                    # once: generate gen/
aflat tests/<name>.act
actsim -cnf=gen/file_registry.conf tests/<name>.act <name>
```

At the `actsim` prompt, `cycle` runs to completion, `quit` exits.
