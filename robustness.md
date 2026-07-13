# ActNow Robustness Roadmap

Each stage below is scoped so it can be done in order, gated on `make test`
(and `make software-tests` where noted) staying green throughout. Work
top-to-bottom within a stage — later tasks assume earlier ones are done.
Checkboxes are meant to be checked off as you go; "Gate" lines are the
concrete pass/fail criteria before moving on.

## Repo conventions (apply from Stage 0 onward)

- **One `defproc` per file, filename matches the proc name.** Already true
  today (`mmu.act` → `defproc mmu`, `regfile.act` → `defproc regfile`, ...);
  keep it true for every new file added in Stages 0-2
  (`demux.act`/`defproc demux`, `gpio.act`/`defproc gpio`,
  `spi_boot.act`/`defproc spi_boot`, etc.).
- **`chips/` (simulation chip variants) vs `harness/` (FPGA/Vivado flow)
  are unrelated, despite the similar vocabulary.** `harness/` already
  exists at repo root for the physical FPGA build (`harness/fpga`,
  `harness/static`, `convert_verilog.sh`, `run_vivado_flow.sh`) — don't
  merge simulation harness code into it, and don't rename `chips/` to
  `harness/`.
- Every `import "x.act"` resolves relative to the working directory the
  compiler is invoked from (see the top-level `Makefile`'s own comment on
  this), not relative to the importing file. Any file move below means
  updating every `import` string that references the moved file, repo-wide
  — not just the mover's own imports.

## Stage 0 — Source tree reorg

Pure file-move/rename work, done before Stage 1's functional changes so
those changes land directly in the new layout instead of moving twice.
Zero behavior change expected anywhere in this stage.

### 0.1 Split the flat root `.act` files into `core/`

Mirrors the split `tests/` already uses (`tests/core` vs
`tests/peripherals`) on the source side, so the two trees read the same
way and stay legible as more peripherals land in Stage 1/2.

- [ ] `core/` — core datapath, tightly coupled to `soc`'s own execution:
  - `core/soc.act`
  - `core/mmu.act` (becomes the real dual-port PMP mmu in Stage 1.2 — see
    below; lives here rather than under `peripherals/` because it's
    soc-internal, not a bus-attached device)
  - `core/regfile.act`
  - `core/interrupt.act`
  - `core/utils.act`
  - `core/globals.act`
- [ ] `core/peripherals/` — bus-attached devices, reusable across chip
  variants:
  - `core/peripherals/mem.act`
  - `core/peripherals/fifo_in.act`
  - `core/peripherals/fifo_out.act`
  - `core/peripherals/demux.act` (new in Stage 1.1 — a periphery-facing
    address router, not core-facing like `mmu.act`; splits whatever `mmu`
    decided wasn't on-chip)
- [ ] Update every `import "..."` string repo-wide to the new paths:
  `core/soc.act`, `core/globals.act`, etc. (`gen/file_ids.act` is
  build-generated and untouched.)
- [ ] Update the top-level `Makefile`'s `TEST_SRC` resolution and any other
  path references to match.
- [ ] **Gate:** `make test` and `make software-tests` both pass with
  nothing but paths changed.

### 0.2 Tidy `tests/`

- [ ] New `tests/regression/` directory for one-off bug-repro tests, so
  `tests/peripherals/` stays "one test file per peripheral." Move
  `tests/peripherals/mode_mem_t_enum_bug_test.act` →
  `tests/regression/mode_mem_t_enum_bug_test.act` as the first occupant.
- [ ] `tests/core/`, `tests/peripherals/`, `tests/sw/` stay exactly as they
  are otherwise — these are chip-agnostic (ISA datapath, standalone
  peripherals, real-program-through-`soc` runs) and don't belong to any one
  chip variant.
- [ ] `tests/e2e/` is retired from the shared tree in Stage 1.5/2.5 below —
  e2e tests wire through one specific chip variant's harness, so each
  variant owns its own `tests/e2e/` under `chips/<variant>/` instead of
  sharing a root-level one.
- [ ] **Gate:** `make test` passes with the renamed/relocated regression
  test picked up correctly.

## Stage 1 — Real PMP MMU, external reset, harness reorg, GPIO, e2e coverage

### 1.1 Split the periphery-facing router out into its own `demux.act`

`core/mmu.act`'s router is core-facing: it sits directly between soc's own
fetch/load-store logic and the chip's on-chip resources (internal RAM,
interrupt controller), servicing those addresses itself and passing
anything else (base >= `ADDR_EXT_MIN`) straight through its catch-all to
soc's own `addr_ext` boundary. It stays named `mmu` and stays soc's own
internal instance — it isn't going anywhere.

What actually needed extracting was the *separate* instantiation of the same
underlying "N exact + 1 catch-all" template that the e2e tests hand-roll
inline to split soc's external bus further into distinct peripherals (ROM /
fifo_in / fifo_out, no catch-all) — that one is periphery-facing (it never
talks to the core, only to whatever `mmu`'s catch-all already decided was
off-chip) and needed to become its own named process: `demux`.

- [x] Copy the router template into `core/peripherals/demux.act` as
      `defproc demux<N_EXACT, EXACT_BASES, CATCHALL_MIN_BASE>`, with its
      single upstream port group named `addr_in`/`mode_in`/`wdata_in`/
      `rdata_in` (not `_core` — this process never faces the CPU core
      directly, unlike `mmu`) and the downstream array kept as
      `addr_out[]`/etc.
  - Hoisted the shared `mask_data` helper into `core/utils.act` (already
    the shared-helper home for `alu`/`branch_taken`/`compute_addr`) so
    `core/mmu.act` and `core/peripherals/demux.act` both reuse one
    definition instead of each defining their own — needed anyway, since
    both end up in scope together wherever a file imports both (ACT's
    import namespace is global/transitive), and two same-named functions
    would conflict.
  - `core/mmu.act` is untouched otherwise — still `defproc mmu`, still
    core-facing `_core` naming, still soc's real, working, on-chip router.
    (**Correction:** an earlier pass at this task emptied `core/mmu.act`
    out and renamed soc's own internal instance to `demux` — backwards.
    `mmu` stays the on-chip router; only the harness's separate splitter
    becomes `demux`. Fixed.)
- [x] Consumers:
  - `core/soc.act` — unchanged (still imports `core/mmu.act`, still
    instantiates `mmu<SOC_MMU_N_EXACT, SOC_MMU_EXACT_BASES, ADDR_EXT_MIN>
    mmu`).
  - `tests/e2e/e2e_fifo_test.act` / `e2e_multi_event_test.act` — now
    `import "core/peripherals/demux.act"` and instantiate `demux<...>`
    for their ROM/fifo_in/fifo_out split (previously hand-rolled against
    the shared `mmu` template).
  - `tests/peripherals/mmu_test.act` — unchanged in spirit, still tests
    `core/mmu.act`'s real 2-exact+catch-all deployment (RAM / interrupt
    controller / external).
  - `tests/peripherals/demux_test.act` — new file, testing
    `core/peripherals/demux.act`'s actual real-world shape (3 exact
    routes mirroring ROM=4/fifo_in=5/fifo_out=6, no catch-all) rather than
    mirroring `mmu_test`'s configuration.
- [x] **Gate:** `make test` passes (including both `mmu_test` and
      `demux_test`); `make software-tests` all 38 programs still pass.
      Zero functional change to soc's own on-chip routing.

### 1.2 Design and implement the real dual-port PMP mmu

- [x] Resolved the open design question: **yes**, instruction fetch needs
      the external bus. soc always boots by fetching the reset vector from
      `ADDR_RESET` (base=4, external ROM, via the catch-all), and the
      top-level Makefile's default (non-`BOOT`) mode executes straight from
      there ("XIP") — confirmed by actually running `make BOOT=1
      ROM_TEST=addi rom_program_test` (fetch from internal RAM via the
      exact-match route) and the default `ROM_TEST=addi rom_program_test`
      (fetch from external ROM via the catch-all route), both against the
      new mmu. So the instr port needs the *same* exact-bases-plus-catch-all
      table as the data port, not just a private path to RAM.
- [x] Defined the new `defproc mmu(...)` in `core/mmu.act` with two
      independent core-facing port groups sharing one physical-resource-facing
      port group:
  - **instr port group:** `addr_instr` (in), `mode_instr` (in), `rdata_instr`
    (out) — no `wdata` channel at all, since fetch never writes. A
    defensive `assert(mode.op = op_mem_t.R, ...)` catches a caller mistake,
    but the actual protection is structural (no channel exists to carry a
    write payload, regardless of what `mode` says).
  - **data port group:** `addr_data`, `mode_data`, `wdata_data`,
    `rdata_data` — full R/W/RMW, same shape as the old single `addr_core`
    group.
  - **shared side:** *both* ports route through the same
    `EXACT_BASES`/`CATCHALL_MIN_BASE` table and the same
    `addr_out[]`/`mode_out[]`/`wdata_out[]`/`rdata_out[]` array (not just a
    RAM bank) — this is what "share the same physical memory" means
    concretely, and it's also what makes the resolved XIP question work:
    the instr port's catch-all reaches the same external boundary the data
    port's catch-all does. The instr branch technically has routing access
    to every `EXACT_BASES` entry (including the interrupt controller), not
    just RAM — restricting *which* addresses fetch may target isn't a
    protection property this project asked for (only "never allow a write
    through the instruction port" was), so it isn't modeled.
- [x] Arbitration: a top-level probed selection (`[| #addr_instr -> ... []
      #addr_data -> ... |]`) picks whichever port has a pending request.
      soc's core is single-issue and strictly sequential (fetch runs to
      completion, then decode/execute — at most one load/store — runs to
      completion, then back to fetch), so the two ports are never both
      pending at once in practice; the probed selection is what makes that
      safe structurally rather than by convention.
- [x] Protection is structural (no `wdata_instr` channel), plus the
      defensive assert above for a misbehaving caller.
- [x] Rewrote `tests/peripherals/mmu_test.act` for the dual-port shape:
      drives the data port through RAM/interrupt-controller/external-catch-all
      (reads/writes/RMW, same masking coverage as before) using a new small
      *stateful* `ram_backend` (unlike the stateless echo-testers used for
      interrupt-controller/external), then drives the instr port to (a) read
      back the exact RAM address the data port just wrote — proving the two
      ports genuinely share one physical resource — and (b) read through the
      catch-all, proving XIP. Didn't add a test that deliberately triggers
      the new defensive assert and expects it to pass: this codebase's
      existing convention (e.g. `mem.act`'s own "write attempted to
      read-only memory" assert) is that should-never-happen asserts are
      implemented but not exercised by the automated suite, since the
      Makefile's pass/fail classification treats any triggered assertion as
      an overall test failure.
- [x] **Gate:** `mmu_test` passes (including the cross-port and XIP
      scenarios above) and `demux_test` (1.1) still passes unmodified.

### 1.3 Rewire `soc.act` onto the new dual-port mmu

Folded into the same change as 1.2 rather than done as a separate step:
changing `core/mmu.act`'s port shape without also updating its only real
consumer left `core/soc.act` (and everything that imports it — most of the
test suite) uncompilable, which breaks this project's "all tests still
pass" gate at every stage. Confirmed by trying `aflat core/soc.act` right
after the 1.2 port-shape change: `ERROR: Port name 'addr_core' does not
exist for the identifier: mmu`.

- [x] Replaced the single `mmu.addr_core/mode_core/wdata_core/rdata_core`
      handshake with two call sites in the existing `chp`: the instruction
      fetch sequence targets `addr_instr/mode_instr/rdata_instr`; the
      `OPCODE_STORE` branch targets `addr_data/mode_data/wdata_data`
      (write-only, no `rdata_data`); the `OPCODE_LOAD` branch targets
      `addr_data/mode_data/rdata_data`. soc's own local scratch variables
      (`addr_core`/`mode_core`/`wdata_core`/`rdata_core`) keep their names
      unchanged — only which mmu port they're sent to/received from changed.
- [x] soc's existing on-chip routing (RAM / interrupt controller / external
      catch-all, `mmu`'s own exact+catch-all logic, wired via
      `mmu.addr_out[]`/etc.) is completely unchanged — same instantiation,
      same `SOC_MMU_N_EXACT`/`SOC_MMU_EXACT_BASES`/`ADDR_EXT_MIN`. Per the
      resolved design question above, the instr port needs this same
      routing (not a private RAM-only path), so nothing here needed to
      change to accommodate it.
- [x] **Gate:** `make test` (all unit + regression + e2e tests) and `make
      software-tests` (38/38) both pass. Explicitly re-verified both
      instruction-fetch paths against the real core: `make BOOT=1
      ROM_TEST=addi rom_program_test` (fetch from internal RAM) and the
      default XIP path (fetch from external ROM) both PASS.

### 1.4 External reset

Requirement resolved up front: reset must recover a **genuinely hung**
program (an infinite loop of otherwise-normal instructions that never
reaches WFI), not just wake a deliberately idle one. That ruled out routing
reset through `interrupt.act`'s existing `event_pc` pipeline (tried first;
`event_pc` is only ever read while idle, so reset would've been stuck
waiting for the program to voluntarily sleep first — useless for a hang).

- [x] Added `chan?(bool) reset_ext` to `core/soc.act`. The main `chp` loop's
      top-level dispatch is a **flat three-way non-deterministic selection**
      (`[| #reset_ext -> ... [] running -> skip [] (~running) & #event_pc ->
      event_pc?pc |]`), not reset nested inside a catch-all branch. This
      went through two iterations before landing here, both driven by
      empirical testing (see below) rather than assumption:
  1. First attempt nested the idle-wait inside a `true` branch racing
     `#reset_ext`. Compiles and mostly works, but has a real gap: if the
     selection ever picked `true` while idle (legal, since `true` is always
     ready) instead of `#reset_ext`, execution commits to blocking on
     `event_pc?pc` and won't reconsider `reset_ext` until a real event
     happens to arrive.
  2. Flattening `running` and `(~running) & #event_pc` out as direct
     siblings of `#reset_ext` — instead of nesting the wait inside a
     `true` catch-all — closes that gap: reset is now a genuine sibling of
     every wait point, not of a branch that merely contains one. Verified
     both that this compiles (probes and plain-boolean guards *can* mix in
     one `[|...|]`, confirmed empirically since there's no precedent for it
     elsewhere in this codebase) and that it actually recovers a core
     that's genuinely idle (blocked on `event_pc?pc` with no event coming)
     purely via `reset_ext`, with no `event_pc` send involved at all.
  - Reset can't interrupt an instruction already in flight (no abort
    mid-transaction) — only between instructions. That's a deliberate
    safety property, not a limitation: no instruction is ever left
    half-executed when reset takes hold.
  - A real, separate bug class discovered and ruled out along the way: a
    testbench that answers a hung program's repeated fetch a few times and
    then *sequentially switches* to a plain blocking `reset_ext!true` can
    deadlock — soc's own arbiter is free to pick "run the next instruction"
    one more time even while reset is pending, and if the testbench has
    already stopped offering to answer fetches by then, that fetch send has
    no receiver, forever. The fix (used in `reset_test.act` below) is an
    always-live fetch-answering process that never stops, with reset sent
    from a separate, independent process — not a sequential hand-off.
- [x] Decided **yes**: reset also clears `interrupt.act`'s vector table and
      enable mask. Rationale: if a new program loads after reset and
      something fires an event before that program reconfigures its own
      vectors, it would otherwise vector through a stale ISR address left
      over from whatever ran before. Implementation: `soc.act` can't fan the
      single external `reset_ext` signal out to both itself and
      `interrupt.act` directly (a channel send has exactly one receiver), so
      it consumes `reset_ext` itself and relays it onward via a second,
      internal `reset_int_ctrl` channel; `interrupt.act` gained a matching
      `reset_int_ctrl` port and an unconditional (not enable-bit-gated)
      branch that re-zeros `vectors[]`/`enable_mask`, reusing the same
      clearing loop already used at cold boot. Register file contents are
      *not* cleared on reset, matching real RISC-V semantics (x1-x31 are
      undefined after reset, not zeroed).
- [x] New unit test: `tests/core/reset_test.act`. Boots a program that
      configures an interrupt vector and enable bit, then hits an
      intentional infinite self-loop (`JAL x0, 0`) modeling a genuine hang
      (never reaches WFI on its own). An always-live `fetch_answerer`
      sub-process keeps answering the repeated self-loop fetch (135
      iterations in a real run) while the top-level test independently
      fires `reset_ext`. Post-reset, the program reads back
      `vectors[0]`/`enable_mask` via real LOAD instructions and stores them
      to external memory, where the testbench asserts both are 0, then
      reaches WFI cleanly, proving normal execution resumes correctly.
- [x] **Gate:** `make test` (including `reset_test`) and `make
      software-tests` (38/38) all pass.

### 1.5 Reorganize the harness into `chips/bench/`

- [ ] New directory `chips/bench/` containing:
  - `chips/bench/harness.act` — the 16 `event_id` channels + `fifo_in` +
    `fifo_out` + `demux` instantiation currently duplicated inline in
    `e2e_fifo_test.act` / `e2e_multi_event_test.act`, extracted into one
    reusable `defproc harness`.
  - `chips/bench/core.act` — instantiates `soc` + `harness` together,
    exposing the 16 event lines, `reset_ext` (1.4), and the GPIO pins added
    in 1.6.
- [ ] Move the e2e-specific Makefile logic (the `ROM_IMAGE` rebuild dance,
      `e2e_fifo_test` / `e2e_multi_event_test` rules) out of the top-level
      `Makefile` into `chips/bench/Makefile` — mirror the existing
      `software/*/Makefile` sub-make pattern; have the top-level `Makefile`
      delegate to it.
- [ ] Move `tests/e2e/e2e_fifo_test.act` and `e2e_multi_event_test.act` to
      `chips/bench/tests/e2e/`, updating them to instantiate
      `chips/bench/core.act` instead of hand-rolling `soc` + `demux` + fifo
      wiring. (Completes the `tests/e2e/` retirement noted in 0.2.)
- [ ] **Gate:** identical test behavior — `make test` from the top level
      still runs both e2e tests successfully (directly or via delegation
      into `chips/bench/`).

### 1.6 Add 8 external GPIO pins to `chips/bench/core.act`

- [ ] 4 input pins:
  - 2 wired straight to two of the interrupt controller's event lines
    (`event_id_N`), each configurable via the existing vector-table
    mechanism to jump to a program-associated pc — pure wiring, no new
    hardware needed.
  - 2 reserved for SWD debug — stub ports only, no-op for now.
- [ ] 4 output pins, driven from a new GPIO regfile peripheral:
  - New file `core/peripherals/gpio.act` — an MMIO register (own
    address-space base, added to `core/globals.act`'s `ADDR_*` constants
    and to `chips/bench/harness.act`'s demux base list) whose bits drive
    the 4 output pins directly.
- [ ] New unit test: `tests/peripherals/gpio_test.act` — write the GPIO
      register, observe the corresponding output pin, independent of the
      full core.
- [ ] **Gate:** `make test` passes with `gpio_test` added.

### 1.7 Full e2e robustness suite

- [ ] Enumerate the execution-path matrix before writing tests:
  1. boot → load program A → run to WFI
  2. boot → load program A → fire event N → ISR → return → WFI
  3. [x] boot → load program A → external reset mid-execution → reboots to
     the reset vector — done, see `tests/e2e/e2e_reset_test.act` below.
  4. [x] boot → load program A → external reset → load program B → run B to
     WFI — done, see `tests/e2e/e2e_reset_reload_test.act` below.
  5. GPIO input pin → configured event → ISR jumps to the pc associated
     with that pin
  6. back-to-back event pressure across a reset boundary (extends
     `e2e_multi_event_test`'s existing coverage)
  7. GPIO output pin driven by software, observed by the testbench
- [ ] One new file per scenario (or one consolidated
      `e2e_robustness_test.act` if setup is shared enough) under
      `chips/bench/tests/e2e/`.
- [ ] **Gate:** every scenario passes; top-level `make test` stays green
      end to end.

#### Scenario 3 (done): `tests/e2e/e2e_reset_test.act`

Real compiled program (`software/application/main.c`) through the real
bootloader, not hand-assembled instructions (that's what
`tests/core/reset_test.act` already covers, along with hang recovery and
interrupt-controller-clearing verification in tight, deterministic detail —
this test's job is the complementary, more practical one). Sequence: boot,
run one batch through the program's real ISR, fire `reset_ext`, then run a
second batch — which only works if the *same* bootloader+application image
genuinely reboots from scratch and re-registers its ISR vector/FIFO trigger
level/enable bit against a freshly-cleared interrupt controller. Verified
`make test`/`make software-tests` both green.

#### Scenario 4 (done): `tests/e2e/e2e_reset_reload_test.act`

The "oh shit, wrong firmware" scenario: a genuinely broken program running,
noticed, and recovered by resetting into a genuinely different, corrected
program — not just rebooting the same image (that's scenario 3 above).

First attempt (abandoned, not fixed forward at the time): two stitched ROM
images plus a test-local `rom_selector` 2-way mux, with the bank flip driven
by the *same* signal that fires `reset_ext`. Hit two problems: `mem<true,...>`'s
one-time preload cost seemed to dominate the timeline unpredictably with two
ROM instances, and a spurious WFI appeared to decode partway through running
`software/hang/main.c` through `soc`+`demux`+`fifo_in`+`fifo_out`, even with
`rom_selector` removed from the picture — while the identical image ran
correctly forever through `tests/sw/rom_program_test.act`'s plain single-ROM
wiring. Root cause wasn't found at the time, so the scenario was descoped.

Revisited later and resolved:

- **The spurious WFI was not a real bug.** Re-isolated with a clean repro
  (`hang` alone, idle, through the exact `demux`+`fifo_in`+`fifo_out`
  topology, nothing ever pushed to it): ran cleanly for 2.55M log lines /
  simulated t≈3B, all `is_wfi = 0`, pure `JAL` self-loop, no spurious
  anything — well past the t≈500340 point where the original run saw it.
  The most likely explanation is the *other* issue documented at the time in
  this same debugging session: orphaned `actsim` processes (from a shell
  timeout that didn't kill the whole process group) racing on shared files
  including the ROM image build path, corrupting an in-flight preload. Not a
  defect in `demux.act`/`fifo_in.act`/the fetch path.
- **The design was corrected per explicit direction:** `reset_ext` must
  never carry a target address or program identity — it stays the same
  plain `chan?(bool)` "reboot from whatever's currently mapped at
  `ADDR_RESET`" signal as scenario 3. Which program is mapped there is
  decided by a completely independent control,
  `core/peripherals/rom_selector.act`'s `flip_bank` — modeling a real
  dual-bank-boot flash, where a human (or an OTA process) picks the bank and
  reset is oblivious to which one it is.
- `rom_selector` mirrors `demux.act`'s routing-loop shape, with `flip_bank`
  folded in as a flat sibling alternative (not nested inside the routing
  branch) — same "reset must be a genuine sibling, not buried behind another
  branch" lesson from scenario 3's own design iteration.
- Test sequence: boot into `software/hang/main.c` (bank A, a real infinite
  loop, verified via `objdump` to be a genuine `j <self>`) and let it idle —
  deliberately *not* proving unresponsiveness by pushing into `fifo_in` and
  expecting it to hang, since `fifo_in.act`'s own `configured` gate means an
  unconfigured push just blocks forever, which would deadlock the testbench
  itself (the same deadlock class `tests/core/reset_test.act`'s
  `fetch_answerer` comment warns about). Then `rs.flip_bank!true` (bank B =
  `software/application/main.c`), then `s.reset_ext!true`, then run the same
  push/expect batch `e2e_fifo_test.act`/`e2e_reset_test.act` already use —
  only possible if the corrected program genuinely booted, registered its
  ISR vector, configured `fifo_in`'s trigger level, and enabled its event
  from scratch. Verified `make test`/`make software-tests` both green.
- `ROM_IMAGE_HANG`/`ROM_IMAGE_APPLICATION` are permanent registry fixtures
  (built once via `file-registry`'s own prerequisite chain), unlike the
  shared `ROM_IMAGE` slot the other e2e tests rebuild-then-restore in place.

## Stage 2 — DVS-specific chip

Depends on Stage 1 being complete (`core/mmu.act`/`core/peripherals/demux.act`
split, `gpio.act`, `chips/bench/` pattern all exist and are stable).

### 2.1 dvs event topology

- [ ] 3 events total: `event_id_0` driven directly by the new 20-bit AER
      input (replacing `fifo_in`'s role); `event_id_1`/`event_id_2`
      externally driven, same as `chips/bench/core.act`'s spare interrupt
      lines.
- [ ] Remove `fifo_out` entirely — any chip-to-outside data path goes
      through the bidirectional program/data SPI instead (2.3).
- [ ] Open design question to resolve first: does the AER input need its
      own edge-triggered wrapper (a `fifo_in`-style "new 20-bit word
      arrived → fire `event_id_0`" peripheral, e.g.
      `core/peripherals/aer_input.act`), or does it wire straight through
      as a plain rendezvous? Decide before implementing — it determines
      whether a new peripheral file is needed.

### 2.2 GPIO reuse

- [ ] Reuse `core/peripherals/gpio.act` and the same 4-in/4-out pin
      allocation from 1.6 unchanged; SWD's 2 pins stay stubbed/no-op.

### 2.3 SPI peripherals

Transaction framing (both interfaces): 1 bit read(0)/write(1), 20-bit
address, 32-bit data — one "transmission" per chip-select-low pulse.

- [ ] `core/peripherals/spi_boot.act` — unidirectional, read-only from the
      chip's perspective: loads the bootloader image into RAM once at
      boot, does nothing thereafter.
- [ ] `core/peripherals/spi_prog.act` — bidirectional: implements the
      transaction framing above, used both to push programs into memory
      and to read/write data through the demux (replacing `fifo_out`'s old
      role).
- [ ] New unit tests: `tests/peripherals/spi_boot_test.act` and
      `tests/peripherals/spi_prog_test.act`, each driving raw SPI-shaped
      transactions and asserting the correct addr/mode/wdata/rdata sequence
      comes out on the memory-facing side.

### 2.4 New demux wiring for dvs

- [ ] A dvs-specific `demux` instantiation (reuse
      `core/peripherals/demux.act` from 1.1 with a new base table) that
      routes converted addr/mode/wdata streams from both SPI peripherals
      into RAM (mirroring how `chips/bench/harness.act`'s demux routes ROM
      traffic today) — with no `fifo_out` route at all.
- [ ] Document the new address map additions in `core/globals.act` (new
      `ADDR_*` constants for the two SPI bases), alongside a comment
      analogous to the existing `ADDR_EXT_MIN` block.

### 2.5 Assemble `chips/dvs/core.act`

- [ ] `chips/dvs/harness.act` (3 events + AER input + GPIO + both SPIs +
      the dvs demux, mirroring `chips/bench/harness.act`'s role) and `soc`
      instantiated together into `chips/dvs/core.act` (mirroring
      `chips/bench/core.act`).
- [ ] New `chips/dvs/Makefile` (+ supporting scripts as needed), modeled on
      `chips/bench/Makefile` from 1.5, reusing existing test-running logic
      where practical.
- [ ] `chips/dvs/tests/e2e/` holds this variant's e2e tests, same pattern
      as `chips/bench/tests/e2e/`.

### 2.6 dvs-specific e2e tests + SPI serialization script

- [ ] New conversion script (e.g. `tools/spi_serialize.py`) that reframes
      compiled program images / assembly test vectors as SPI transaction
      streams (R/W bit + 20-bit addr + 32-bit data per transmission) for
      `spi_prog_test` and the dvs e2e tests.
- [ ] Port the Stage 1 execution-path matrix (1.7) to `chips/dvs/core.act`,
      substituting AER input for fifo_in-driven events and SPI in/out for
      `fifo_out`, plus at least one test specific to the 20-bit AER data
      width end to end.
- [ ] **Gate:** `chips/dvs/` has its own green test run, mirroring the
      top-level `make test` gate structure.

## Stage 3 — Debugging support (not yet scoped)

The only concrete constraint so far: 2 GPIO pins are reserved for SWD in
both `chips/bench/core.act` (1.6) and `chips/dvs/core.act` (2.2), currently
wired to nothing. This stage can't be broken into Stage-1/2-style
actionable tasks until these are answered:

- [ ] What protocol — real ARM SWD, or a simplified custom 2-wire scheme?
- [ ] What's actually debuggable: halt/resume, register read/write, memory
      read/write, breakpoints — some combination, or all of them?
- [ ] Does this live in a new peripheral hanging off the existing
      demux/mmu pattern, or does it need direct core access that bypasses
      both (e.g. forcing `pc`, halting the `chp` loop from outside)?
- [ ] Is this needed on `chips/bench`, `chips/dvs`, or both?

Once these are answered, expand this section into the same
checkbox/Gate structure as Stages 1 and 2 before starting implementation.
