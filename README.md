# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Instructions

Google suite of tests????

The full RV32I base integer set is implemented in `soc.act`, including
loads (LB/LH/LW/LBU/LHU) and stores (SB/SH/SW), both routed through the MMU
(`mmu.act`) to either internal RAM (`mem.act`) or external memory
(`addr_ext`/`mode_ext`/`wdata_ext`/`rdata_ext`). Loads sign/zero-extend in
`soc.act` after the MMU's masking; stores rely on the MMU masking the write
value down to the requested size before it reaches the peripheral. See
`tests/load_test.act` and `tests/store_test.act`.

## Running real compiled programs

`tests/rom_program_test.act` runs an actual compiled RV32I program through
`soc`'s real fetch/decode/execute pipeline, instead of hand-crafted
instruction words. It streams the program image from disk at simulation
time via actsim's `sim::file_private` file API (see `actsim.conf`'s
`sim.file.name_table`, which maps file ID 0 to
`software/tests/build/rom.actsim.mem`), and serves it as `soc`'s external
memory.

To point it at a different program, build one under `software/tests/`
(riscv-tests style — see `software/tests/README`) and copy/regenerate
`rom.actsim.mem`:

```
cd software/tests
make TEST=<name>          # -> build/rom.mem, build/rom.actsim.mem, build/<name>.lst
cd ../..
make rom_program_test
```

`software/tests/Makefile`'s `rom.actsim.mem` target derives from `rom.mem`
(itself already used for a Verilog-`$readmemb`-style flow, one bitstring per
line) by adding the `0b` prefix actsim's file reader needs to parse binary;
it doesn't touch `rom.mem` itself.

Note: `rom.mem`/`rom.actsim.mem` don't track which `TEST=` last produced
them, and `make TEST=<name>` won't regenerate them if `<name>`'s own `.elf`
happens to already be up to date from a previous build — if you switch
`TEST=` and the image doesn't look like you expect, `rm -f build/rom.mem
build/rom.actsim.mem` first.

### Pass/fail signalling

Reaching WFI alone doesn't mean a test *passed* — only that it ran to
completion. The riscv-tests convention (`common/test_start.S`) is: `WFI` =
pass, `EBREAK` = fail (emitted by a `TEST_CASE` comparison that didn't
match). `soc.act` treats EBREAK as a halt via the same path as WFI, but logs
a distinct `EBREAK -- test FAILED` line (with the failing `TESTNUM`/`x28`
value logged just above it), which the top-level `Makefile` also greps for
as a second FAIL condition alongside `ASSERTION failed`.

`addi.S` (20 real `TEST_CASE` comparisons — sign extension, overflow
wraparound, aliasing) passes cleanly through `rom_program_test.act` with
zero EBREAKs. The detection path itself is verified against a deliberately
wrong expected value, which correctly produces `EBREAK -- test FAILED`.

## Toolchain

Built and simulated against the `act`/`actsim` toolchain (`asyncvlsi/act`).
Requires `actsim` built from commit `fa1a636` ("tests and fixes for
user-defined enums") or later — earlier versions crash
(`Assertion: pos == nvals` in `state.cc`) the instant a `deftype` struct with
an enum-typed field (e.g. this project's `mode_mem_t`) is sent over a
channel, which `mmu.act`/`mem.act` do on every single memory transaction.
`tests/mode_mem_t_enum_bug_test.act` is a standalone regression test for
this. If your `actsim` predates the fix, update the `actsim` submodule
inside your `act`/`actflow` checkout to `origin/master` and rebuild just
`act` + `actsim` (no need to build the rest of an `actflow` monorepo
checkout, if you have one — the unrelated EDA tools in it aren't needed
here).

**Always compile and run from the project root (`actnow/`), never from
inside `tests/`.** ACT resolves every `import` path relative to the
compiler's working directory, not relative to the importing file — so
`soc.act`'s own `import "interrupt.act"` only resolves correctly if the
whole compilation runs with `actnow/` as the working directory:

```
cd actnow
aflat tests/<name>.act
actsim tests/<name>.act <defproc-name>
```

At the `actsim` prompt, type `cycle` to run to completion, `quit` to exit.
