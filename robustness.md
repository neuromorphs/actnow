# ActNow Robustness Roadmap

Each stage below is scoped so it can be done in order, gated on `make test`
(and `make software-tests` where noted) staying green throughout. Work
top-to-bottom within a stage — later tasks assume earlier ones are done.
Checkboxes are meant to be checked off as you go; "Gate" lines are the
concrete pass/fail criteria before moving on.

## Stage 1 — Real PMP MMU, external reset, harness reorg, GPIO, e2e coverage

### 1.1 Split the generic address router out of `mmu.act` into `demux.act`

The templated router currently living in `mmu.act` (`defproc mmu<N_EXACT,
EXACT_BASES, CATCHALL_MIN_BASE>`) is just an address demux — it's reused
today both inside `soc.act` (RAM / interrupt-controller / external routing)
and inline in the e2e tests (ROM / fifo_in / fifo_out routing). It needs to
become its own process so the *real* PMP mmu (1.2) can take the `mmu` name.

- [ ] Copy the existing template verbatim into a new `demux.act`, renamed
      `defproc demux<N_EXACT, EXACT_BASES, CATCHALL_MIN_BASE>` (same ports,
      same body — pure rename, no behavior change).
- [ ] Update every current consumer to `import "demux.act"` and instantiate
      `demux<...>` instead of `mmu<...>`:
  - `soc.act`'s own peripheral router (`SOC_MMU_N_EXACT` / `SOC_MMU_EXACT_BASES`)
  - `tests/e2e/e2e_fifo_test.act`
  - `tests/e2e/e2e_multi_event_test.act`
  - `tests/peripherals/mmu_test.act` — rename to `demux_test.act`
    (`defproc demux_test`), since it's exercising the generic router, not
    the new PMP design.
- [ ] Remove the old generic router's body from `mmu.act`, leaving the file
      empty/ready for 1.2.
- [ ] **Gate:** `make test` passes with everything renamed — zero functional
      change expected from this task alone.

### 1.2 Design and implement the real PMP mmu

- [ ] Resolve the open design question before writing code: does
      instruction fetch ever need to reach the external bus (XIP from
      external ROM, per the Makefile's non-`BOOT` path), or does this
      architecture only ever execute out of internal RAM? This determines
      whether the instruction port needs a read-only path through `demux`
      or can go straight to `mem.act`'s RAM route.
- [ ] Define the new `defproc mmu(...)` in `mmu.act` with two independent
      core-facing port groups sharing one physical-memory-facing port group:
  - **instr port group:** `addr_instr` (in), `mode_instr` (in, always
    `op_mem_t.R`), `rdata_instr` (out) — no `wdata` channel at all, since
    fetch never writes.
  - **data port group:** `addr_data`, `mode_data`, `wdata_data`,
    `rdata_data` — full R/W/RMW, same shape as today's `addr_core` group.
  - **physical side:** one addr/mode/wdata/rdata group wired to a single
    unchanged `mem.act` bank; instr and data traffic arbitrate onto it.
- [ ] Decide and implement the arbitration policy for a same-cycle
      fetch + load/store race (soc's core is single-issue, so simple
      priority/round-robin is likely sufficient — document the choice).
- [ ] Protection is structural (no `wdata` on the instr side means no write
      is representable), but add a defensive `assert`/log if `mode_instr`
      ever carries `op_mem_t.W`, in case a future caller misuses the port.
- [ ] New unit test: `tests/peripherals/mmu_test.act` (the real one this
      time) — drive the instr port with reads only, the data port with
      reads/writes/RMW against the same backing addresses, assert writes
      through the data port are visible to instr-port reads, and that a
      manufactured illegal instr-port write is rejected/asserted.
- [ ] **Gate:** new `mmu_test` passes; `demux_test` (1.1) still passes
      unmodified.

### 1.3 Rewire `soc.act` onto the new dual-port mmu

- [ ] Replace the single `mmu.addr_core/mode_core/wdata_core/rdata_core`
      handshake with two call sites in the existing `chp`: the instruction
      fetch sequence targets `addr_instr/mode_instr/rdata_instr`; the
      `OPCODE_LOAD`/`OPCODE_STORE` branches target
      `addr_data/mode_data/wdata_data/rdata_data`.
- [ ] soc's existing peripheral routing (RAM / interrupt controller /
      external, via `demux` from 1.1) sits downstream of the **data** port
      only. Per the 1.2 design decision: if fetch never needs the external
      bus, wire the instr port straight to `mem.act`'s RAM and skip `demux`
      for instruction traffic entirely.
- [ ] **Gate:** `make test`, `make software-tests`, and both existing e2e
      tests (`e2e_fifo_test`, `e2e_multi_event_test`) all still pass
      unmodified.

### 1.4 External reset

- [ ] Add a `chan?(bool) reset_ext` port to `soc.act`, sampled as a
      top-level alternative (same shape as `interrupt.act`'s `is_reset`
      handling) that re-triggers reset behavior on demand — `pc :=
      ADDR_RESET`, `running := false` — without restarting the simulation.
- [ ] Decide whether reset also needs to clear `interrupt.act`'s vector
      table / `enable_mask` (real hardware would) — if yes, plumb an
      equivalent reset line into `interrupt.act` too.
- [ ] New unit test: `tests/core/reset_test.act` — boot, execute a few
      instructions/events, assert external reset returns `pc` to
      `ADDR_RESET` and `running=false`, then confirm normal execution
      resumes correctly afterward.
- [ ] **Gate:** `make test` includes and passes `reset_test`; all
      pre-existing tests still pass.

### 1.5 Reorganize the harness into its own directory

- [ ] New directory (e.g. `test_grizzly/`) containing:
  - `test_harness.act` — the 16 `event_id` channels + `fifo_in` + `fifo_out`
    + `demux` instantiation currently duplicated inline in
    `e2e_fifo_test.act` / `e2e_multi_event_test.act`, extracted into one
    reusable `defproc`.
  - `test_core.act` — instantiates `soc` + `test_harness` together,
    exposing the 16 event lines, `reset_ext` (1.4), and the GPIO pins added
    in 1.6.
- [ ] Move the e2e-specific Makefile logic (the `ROM_IMAGE` rebuild dance,
      `e2e_fifo_test` / `e2e_multi_event_test` rules) out of the top-level
      `Makefile` into a `Makefile` inside this new directory — mirror the
      existing `software/*/Makefile` sub-make pattern; have the top-level
      `Makefile` delegate to it.
- [ ] Update `tests/e2e/e2e_fifo_test.act` and `e2e_multi_event_test.act` to
      instantiate `test_core` instead of hand-rolling `soc` + `demux` +
      fifo wiring.
- [ ] **Gate:** identical test behavior — `make test` from the top level
      still runs both e2e tests successfully (directly or via delegation).

### 1.6 Add 8 external GPIO pins to `test_core.act`

- [ ] 4 input pins:
  - 2 wired straight to two of the interrupt controller's event lines
    (`event_id_N`), each configurable via the existing vector-table
    mechanism to jump to a program-associated pc — pure wiring, no new
    hardware needed.
  - 2 reserved for SWD debug — stub ports only, no-op for now.
- [ ] 4 output pins, driven from a new GPIO regfile peripheral:
  - New file `gpio.act` — an MMIO register (own address-space base, added
    to `globals.act`'s `ADDR_*` constants and to `test_harness`'s demux
    base list) whose bits drive the 4 output pins directly.
- [ ] New unit test: `tests/peripherals/gpio_test.act` — write the GPIO
      register, observe the corresponding output pin, independent of the
      full core.
- [ ] **Gate:** `make test` passes with `gpio_test` added.

### 1.7 Full e2e robustness suite

- [ ] Enumerate the execution-path matrix before writing tests:
  1. boot → load program A → run to WFI
  2. boot → load program A → fire event N → ISR → return → WFI
  3. boot → load program A → external reset mid-execution → reboots to the
     reset vector
  4. boot → load program A → external reset → load program B → run B to
     WFI
  5. GPIO input pin → configured event → ISR jumps to the pc associated
     with that pin
  6. back-to-back event pressure across a reset boundary (extends
     `e2e_multi_event_test`'s existing coverage)
  7. GPIO output pin driven by software, observed by the testbench
- [ ] One new file per scenario (or one consolidated
      `tests/e2e/e2e_robustness_test.act` if setup is shared enough) under
      `tests/e2e/`, each wired through `test_core.act` (1.5).
- [ ] **Gate:** every scenario passes; top-level `make test` stays green
      end to end.

## Stage 2 — DVS-specific chip

Depends on Stage 1 being complete (`mmu.act`/`demux.act` split, `gpio.act`,
`test_core.act` pattern all exist and are stable).

### 2.1 dvs event topology

- [ ] 3 events total: `event_id_0` driven directly by the new 20-bit AER
      input (replacing `fifo_in`'s role); `event_id_1`/`event_id_2`
      externally driven, same as `test_core.act`'s spare interrupt lines.
- [ ] Remove `fifo_out` entirely — any chip-to-outside data path goes
      through the bidirectional program/data SPI instead (2.3).
- [ ] Open design question to resolve first: does the AER input need its
      own edge-triggered wrapper (a `fifo_in`-style "new 20-bit word
      arrived → fire `event_id_0`" peripheral, e.g. `aer_input.act`), or
      does it wire straight through as a plain rendezvous? Decide before
      implementing — it determines whether a new peripheral file is needed.

### 2.2 GPIO reuse

- [ ] Reuse `gpio.act` and the same 4-in/4-out pin allocation from 1.6
      unchanged; SWD's 2 pins stay stubbed/no-op.

### 2.3 SPI peripherals

Transaction framing (both interfaces): 1 bit read(0)/write(1), 20-bit
address, 32-bit data — one "transmission" per chip-select-low pulse.

- [ ] `spi_boot.act` — unidirectional, read-only from the chip's
      perspective: loads the bootloader image into RAM once at boot, does
      nothing thereafter.
- [ ] `spi_prog.act` — bidirectional: implements the transaction framing
      above, used both to push programs into memory and to read/write data
      through the demux (replacing `fifo_out`'s old role).
- [ ] New unit tests: `tests/peripherals/spi_boot_test.act` and
      `tests/peripherals/spi_prog_test.act`, each driving raw SPI-shaped
      transactions and asserting the correct addr/mode/wdata/rdata sequence
      comes out on the memory-facing side.

### 2.4 New demux wiring for dvs

- [ ] A dvs-specific `demux` instantiation (reuse `demux.act` from 1.1 with
      a new base table) that routes converted addr/mode/wdata streams from
      both SPI peripherals into RAM (mirroring how `test_harness`'s demux
      routes ROM traffic today) — with no `fifo_out` route at all.
- [ ] Document the new address map additions in `globals.act` (new
      `ADDR_*` constants for the two SPI bases), alongside a comment
      analogous to the existing `ADDR_EXT_MIN` block.

### 2.5 Assemble `dvs_core.act`

- [ ] `dvs_harness.act` (3 events + AER input + GPIO + both SPIs + the
      dvs demux, mirroring `test_harness.act`'s role) and `soc` instantiated
      together into `dvs_core.act` (mirroring `test_core.act`), in a new
      top-level directory (e.g. `dvs/`).
- [ ] New `Makefile` (+ supporting scripts as needed) in `dvs/`, modeled on
      the harness Makefile extracted in 1.5, reusing existing test-running
      logic where practical.

### 2.6 dvs-specific e2e tests + SPI serialization script

- [ ] New conversion script (e.g. `tools/spi_serialize.py`) that reframes
      compiled program images / assembly test vectors as SPI transaction
      streams (R/W bit + 20-bit addr + 32-bit data per transmission) for
      `spi_prog_test` and the dvs e2e tests.
- [ ] Port the Stage 1 execution-path matrix (1.7) to `dvs_core`,
      substituting AER input for fifo_in-driven events and SPI in/out for
      `fifo_out`, plus at least one test specific to the 20-bit AER data
      width end to end.
- [ ] **Gate:** `dvs/` has its own green test run, mirroring the
      top-level `make test` gate structure.

## Stage 3 — Debugging support (not yet scoped)

The only concrete constraint so far: 2 GPIO pins are reserved for SWD in
both `test_core.act` (1.6) and `dvs_core.act` (2.2), currently wired to
nothing. This stage can't be broken into Stage-1/2-style actionable tasks
until these are answered:

- [ ] What protocol — real ARM SWD, or a simplified custom 2-wire scheme?
- [ ] What's actually debuggable: halt/resume, register read/write, memory
      read/write, breakpoints — some combination, or all of them?
- [ ] Does this live in a new peripheral hanging off the existing
      demux/mmu pattern, or does it need direct core access that bypasses
      both (e.g. forcing `pc`, halting the `chp` loop from outside)?
- [ ] Is this needed on `test_core`, `dvs_core`, or both?

Once these are answered, expand this section into the same
checkbox/Gate structure as Stages 1 and 2 before starting implementation.
