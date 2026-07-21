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

- [x] New directory `chips/bench/` containing:
  - `chips/bench/harness.act` — the `fifo_in` + `fifo_out` + `demux`
    instantiation currently duplicated inline in `e2e_fifo_test.act` /
    `e2e_multi_event_test.act` / `e2e_reset_test.act`, extracted into one
    reusable `template<pint ROM_IMAGE> defproc harness`. Exposes
    `addr_in`/`mode_in`/`wdata_in`/`rdata_in` (soc's external bus),
    `push`/`pop` (the two FIFOs), and `fifo_event` — fifo_in's auto-fire
    (`event_out`), left for the caller to route rather than hardwired to a
    specific `event_id_N` here (see below for why).
  - `chips/bench/core.act` — instantiates `soc` + `harness` together,
    exposing all 16 `event_id_N` lines (uniform pass-through, undifferentiated),
    `reset_ext` (1.4), `fifo_event`, and `push`/`pop`. GPIO (1.6) will extend
    this port list once it lands.
  - **Resolved design question:** `event_id_0` is not hardwired to
    `fifo_event` inside `harness`/`core`. `e2e_fifo_test.act` /
    `e2e_reset_test.act` want fifo_in's auto-fire wired to `event_id_0`
    (real hardware behavior), but `e2e_multi_event_test.act` deliberately
    fires *all 16* lines manually, including line 0, to get full-width
    interrupt-controller coverage in one uniform loop — a simulation-only
    technique that only works if line 0 is externally drivable, i.e. *not*
    claimed internally by fifo_in. Since a channel can only have one sender,
    these two needs are mutually exclusive at the hardware-wiring level, so
    the choice is left to each testbench: `core`'s `fifo_event` and
    `event_id_0` are two independent boundary ports of the same instance,
    and a caller that wants the real-hardware wiring connects them itself
    (`c.event_id_0 = c.fifo_event;`, one line, in `e2e_fifo_test.act` /
    `e2e_reset_test.act`); `e2e_multi_event_test.act` instead connects
    `event_id_0` to its own manually-driven channel and leaves `fifo_event`
    unconnected (safe, since its trigger_level stays configured
    unreachable — see `software/multi_event/main.c`'s own comment).
- [x] Moved the e2e-specific Makefile logic (the `ROM_IMAGE` rebuild dance,
      all four `e2e_*_test` rules) out of the top-level `Makefile` into
      `chips/bench/Makefile`; the top-level `Makefile` delegates to it
      (`$(MAKE) -C chips/bench $@ ROM_TEST=... BOOT=... CROSS=...`).
      Shared infrastructure (`file-registry`/`$(ROM_IMAGE)`/
      `$(ROM_IMAGE_HANG)`/`$(ROM_IMAGE_APPLICATION)`) stays owned by the
      top-level `Makefile` — it's needed by every test, not just e2e ones,
      since `file_registry.txt` requires all registered inputs to exist
      before `gen/` can be regenerated at all — so `chips/bench/Makefile`'s
      recipes call back into it via an explicit sub-make
      (`$(MAKE) -C $(ROOT) ...`, `$(ROOT) := ../..`). Every aflat/actsim
      invocation there explicitly `cd`s to `$(ROOT)` first, since ACT
      resolves every `import` relative to the compiler's invocation
      directory (must stay `actnow/`), not relative to the importing file
      or to wherever the sub-make happened to be invoked from.
- [x] Moved all four e2e tests (`e2e_fifo_test.act`, `e2e_multi_event_test.act`,
      `e2e_reset_test.act`, `e2e_reset_reload_test.act`) from `tests/e2e/` to
      `chips/bench/tests/e2e/` — completes the `tests/e2e/` retirement noted
      in 0.2. The first three now instantiate `chips/bench/core.act` instead
      of hand-rolling `soc` + `demux` + fifo wiring. `e2e_reset_reload_test.act`
      keeps its own hand-rolled topology (`soc` + `demux` + `rom_selector` +
      two `mem` instances + fifo_in/fifo_out): it needs two independent
      backing ROMs behind a bank-select mux, which `harness`'s single
      `ROM_IMAGE` parameter doesn't (and shouldn't) accommodate.
- [x] **Gate:** `make test` (all unit + regression + sw tests, plus all four
      e2e tests via delegation into `chips/bench/`) and `make software-tests`
      (38/38) both pass, verified end to end with the real toolchain.

### 1.6 Add 8 external GPIO pins to `chips/bench/core.act`

- [x] 4 input pins:
  - `gpio_in_0`/`gpio_in_1` are pure wiring onto two of the interrupt
    controller's event lines — specifically `event_id_14`/`event_id_15`
    (the top two of soc's 16), chosen so `event_id_0..13` stay free and
    undifferentiated for `e2e_multi_event_test.act`'s existing full-width,
    manually-driven interrupt-controller coverage test. `event_id_14`/
    `event_id_15` were removed from `core.act`'s own generic `event_id_N`
    port list and replaced by these two GPIO-named ports (same underlying
    soc event line, just given its real-world identity at the boundary);
    `e2e_multi_event_test.act` updated accordingly (drives them via
    `c.gpio_in_0`/`c.gpio_in_1` instead of `c.event_id_14`/`c.event_id_15`
    — no functional change, still fires all 16 lines). Vectoring (jump to
    a program-associated pc) is the existing `core/interrupt.act` table —
    no new hardware needed.
  - `swd_0`/`swd_1` reserved for SWD debug — stub ports only (declared on
    `core.act` for interface completeness, left completely unconnected to
    any soc functionality), genuine no-ops until Stage 3 is scoped.
- [x] 4 output pins, driven from a new GPIO regfile peripheral:
  - New file `core/peripherals/gpio.act` — a single MMIO register (address
    offset ignored, same convention as fifo_in/fifo_out) whose low 4 bits
    drive 4 individually-named `chan!(bool)` pins (`pin_0..pin_3`, not an
    array — matches `core/soc.act`'s own `event_id_N` convention). A CPU
    write updates the register and re-drives all 4 pins with the new bit
    values in the same transaction; a CPU read returns the last-written
    value. New `pint ADDR_GPIO = 7` in `core/globals.act`, added as a 4th
    exact route (alongside ROM=4/fifo_in=5/fifo_out=6) to
    `chips/bench/harness.act`'s demux; `harness.act` exposes the 4 pins as
    `gpio_out_0..gpio_out_3`, passed straight through by `core.act`.
- [x] New unit test: `tests/peripherals/gpio_test.act` — drives `gpio`
      directly (no soc/harness involved), writes two different 4-bit
      patterns and asserts all 4 pins fire with the correct level in the
      same transaction as the write, plus asserts CPU readback returns the
      last-written value.
- [x] **Gate:** `make test` (21 testbenches, including `gpio_test`) and
      `make software-tests` (38/38) both pass, verified end to end with the
      real toolchain.

### 1.7 Full e2e robustness suite

- [x] Enumerate the execution-path matrix before writing tests:
  1. [x] boot → load program A → run to WFI — done, see
     `chips/bench/tests/e2e/e2e_boot_test.act` below.
  2. [x] boot → load program A → fire event N → ISR → return → WFI —
     already covered by `e2e_fifo_test.act`'s own batch 1 and
     `e2e_gpio_test.act` (both are exactly this shape), so no separate
     file was added for it — would've been pure duplication with nothing
     new to prove.
  3. [x] boot → load program A → external reset mid-execution → reboots to
     the reset vector — done, see `chips/bench/tests/e2e/e2e_reset_test.act`
     below.
  4. [x] boot → load program A → external reset → load program B → run B to
     WFI — done, see
     `chips/bench/tests/e2e/e2e_reset_reload_test.act` below.
  5. [x] GPIO input pin → configured event → ISR jumps to the pc associated
     with that pin — done, see Scenario 5/7 below.
  6. [x] back-to-back event pressure across a reset boundary (extends
     `e2e_multi_event_test`'s existing coverage) — done, see Scenario 6
     below.
  7. [x] GPIO output pin driven by software, observed by the testbench —
     done, see Scenario 5/7 below.
- [x] One new file per scenario (scenario 2 excepted — already covered, see
      above) under `chips/bench/tests/e2e/`.
- [x] **Gate:** every scenario passes; top-level `make test` stays green
      end to end — verified (24 testbenches total) alongside
      `make software-tests` (38/38).

#### Scenario 1 (done): `chips/bench/tests/e2e/e2e_boot_test.act`

The baseline every other e2e scenario already builds on implicitly, given
its own dedicated, minimal test: a new program, `software/boot_only/main.c`,
that does nothing but return from `main()` — no interrupts configured, no
FIFO/GPIO touched at all. `e2e_boot_test.act` has no `chp` body of its own:
`boot_only` never exercises a channel the testbench could block on, so
there's nothing to synchronize against or observe. That's fine —
`actsim`'s `cycle` command doesn't return until the *entire* simulation
quiesces, not just the outermost test process, so `soc`'s own `decoded wfi`
log line is guaranteed to appear by completion regardless. `chips/bench/
Makefile`'s `e2e_boot_test` rule judges pass/fail directly from that log
line (same convention `make software-tests` already uses for programs with
no testbench-side interaction), rather than requiring a testbench-emitted
`"test complete"` line the way every other e2e rule here does.

#### Scenario 6 (done): `chips/bench/tests/e2e/e2e_multi_event_reset_test.act`

Extends `e2e_multi_event_test.act`'s full-width interrupt-controller
coverage two ways at once, reusing the same `software/multi_event/main.c`
program:

- **Back-to-back pressure:** fires events with no artificial inter-event
  delay, unlike `e2e_multi_event_test.act`'s own 300-time-unit pause
  ("model a little real-world latency between events"). This architecture
  is single-issue and non-preemptive, so genuinely overlapping ISRs aren't
  a real scenario here — what *is* meaningful to prove is that firing the
  next event with zero testbench-side pacing is still safe purely from
  `core/interrupt.act`'s own structure: it's one sequential loop that sends
  `event_pc!vectors[N]` (itself blocked until `soc` goes idle again) before
  ever looping back to offer the next `event_id_N`, so a premature
  `event_ch[k]!(true)` just blocks at that rendezvous until the controller
  is genuinely ready — no deadlock, no dropped event, no delay required
  from the testbench.
- **Across a reset boundary:** batch 1 (events 0..7) runs back-to-back,
  external reset fires mid-stream, then batch 2 (events 8..15) runs
  back-to-back against the freshly-rebooted program. Only passes if the
  *entire* 16-entry vector table and enable mask — not just `event_id_0`,
  the only line `e2e_reset_test.act` re-proves — genuinely survive a
  reset+reboot cycle intact.

Verified `make test`/`make software-tests` both green.

#### Scenario 3 (done): `chips/bench/tests/e2e/e2e_reset_test.act`

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

#### Scenario 4 (done): `chips/bench/tests/e2e/e2e_reset_reload_test.act`

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

#### Scenarios 5 & 7 (done): `chips/bench/tests/e2e/e2e_gpio_test.act`

Both scenarios share one setup (GPIO input triggering an ISR; that ISR's
own MMIO store driving GPIO output), so one new program and one new
testbench cover both directions instead of two separate files.

New real compiled program, `software/gpio_demo/main.c`: registers a
distinct ISR for each of `chips/bench/core.act`'s two GPIO input pins
(`gpio_in_0`/`gpio_in_1` — Stage 1.6's pure wiring onto soc's
`event_id_14`/`event_id_15`), enables both via the existing interrupt-
controller enable mask, then goes to sleep. `isr_gpio_in_0` writes `0b0101`
to the GPIO output register (`core/peripherals/gpio.act`, base=7);
`isr_gpio_in_1` writes `0b1010` — two distinct, easy-to-verify patterns so
the testbench can tell which ISR actually ran, not just that *an* ISR ran.

`e2e_gpio_test.act` boots that program via `chips/bench/core.act`, then for
each input pin: fires it (`c.gpio_in_0!true` / `c.gpio_in_1!true`, self-
managed synchronization same as every other event line — interrupt.act
doesn't offer to receive until gpio_demo's own enable write, so no guessed
boot-completion delay is needed) concurrently with receiving on all four of
`gpio_out_0..gpio_out_3`, and asserts the received pattern matches that
line's ISR. Proves scenario 5 (GPIO input → vectored ISR) and scenario 7
(GPIO output driven by software, observed externally) in the same real
program, end to end through the real bootloader. Verified `make test`/`make
software-tests` both green (`make test` now runs 22 testbenches total,
including `e2e_gpio_test`).

## Stage 2 — DVS-specific chip

Depends on Stage 1 being complete (`core/mmu.act`/`core/peripherals/demux.act`
split, `gpio.act`, `chips/bench/` pattern all exist and are stable).

### 2.1 dvs event topology

- [x] **Resolved the open design question:** the AER input needs an
      edge-triggered wrapper (something has to hold a pixel-event's value
      between when it asynchronously arrives and when the CPU's ISR reads
      it, and buffer more than one in case the sensor bursts faster than
      software drains it) — but **not** a new peripheral file.
      `core/peripherals/fifo_in.act` already *is* that wrapper; the only
      thing tying it to 32-bit data was its buffer/push element type, so it
      gained a second template parameter, `WIDTH`
      (`template<pint DEPTH; pint WIDTH>`), governing `buf[]`/`push`'s
      element width while `addr`/`mode`/`wdata`/`rdata` (the CPU-facing MMIO
      bus) stay fixed at `WIDTH_DATA` — `rdata` zero-extends a narrower
      buffered word via `int(buf[head], WIDTH_DATA)`. The dvs chip variant
      will instantiate `fifo_in<DEPTH, WIDTH_ADDR>` for its 20-bit AER
      input in 2.5 (`WIDTH_ADDR` — already defined in `core/globals.act`,
      previously unused — is this project's own address width, which an
      AER pixel-event payload naturally matches). Existing callers
      (`chips/bench/harness.act`, `tests/peripherals/fifo_test.act`,
      `chips/bench/tests/e2e/e2e_reset_reload_test.act`) updated to
      `fifo_in<4, WIDTH_DATA>`, preserving today's behavior exactly.
- [x] 3 events total: new `chips/dvs/harness.act` + `chips/dvs/core.act`
      (built incrementally starting here, rather than in one shot at 2.5 --
      2.2/2.3/2.4 will extend these same two files rather than assembling
      fresh ones). `chips/dvs/harness.act` is a demux with exactly one
      route so far (`base=ADDR_AER=5`, `fifo_in<AER_DEPTH, WIDTH_ADDR>` --
      the AER input, taking fifo_in's old base=5 slot from `chips/bench`).
      `chips/dvs/core.act` wires `soc` + that harness together:
      `event_id_0` is hardwired directly to the AER input's `event_out`
      (unlike `chips/bench/core.act`'s `fifo_event`, left as an
      independent, optionally-connected port -- dvs has no equivalent of
      `e2e_multi_event_test.act`'s need to fire that line manually, so
      there's no reason to expose the choice); `event_id_1`/`event_id_2`
      are generic externally-driven pass-through lines, the dvs equivalent
      of `chips/bench/core.act`'s spare interrupt lines (and where its two
      GPIO input pins will come from -- 2.2). `event_id_3..15` are simply
      never connected. Compile-checked by fully elaborating `core<8>`
      (`aflat` on a throwaway instantiating file) -- clean, only the
      pre-existing unrelated `mem.act` write-conflict warning that appears
      on every `soc`-based compile. `make test`/`make software-tests` both
      still pass (nothing here is reachable from the existing suite).
- [x] Remove `fifo_out` entirely — satisfied by omission: `chips/dvs/
      harness.act` never imports or instantiates `fifo_out.act`. Any
      chip-to-outside data path will go through spi_prog instead (2.3, base
      reserved at `ADDR_AER+1`, i.e. 6 -- fifo_out's old slot).
- [x] **Gate:** `make test`/`make software-tests` both still pass with
      `fifo_in`'s new signature — verified end to end.

### 2.2 GPIO reuse

- [x] **Confirmed:** `core/peripherals/gpio.act` is fully chip-agnostic — a
      generic MMIO register + 4 output pins, no `soc`/`chips/bench`-specific
      coupling — so it's reused for `chips/dvs` with zero code changes.
      The 4-in/4-out pin allocation from 1.6 carries over unchanged in
      role, just re-targeted to dvs's own (smaller) event set: `gpio_in_0`/
      `gpio_in_1` will wire onto `event_id_1`/`event_id_2` (dvs's two spare
      lines, per 2.1 — the dvs equivalent of `chips/bench`'s
      `event_id_14`/`event_id_15`), `swd_0`/`swd_1` stay stubbed/no-op, and
      `gpio_out_0`..`gpio_out_3` pass through a `gpio` instance exactly like
      `chips/bench/harness.act`'s does. Not wired yet -- `chips/dvs/
      harness.act`/`core.act` now exist (started in 2.1) and will gain
      these ports as an incremental extension when this stage is actually
      implemented, the same way 2.3/2.4 will extend them further rather
      than a one-shot assembly in 2.5.

### 2.3 SPI peripherals

**Reworked mid-stage** — the first pass (both peripherals receiving `cs`/
`mosi` as `chan?(bool)`, using each bit rendezvous as an implicit clock
edge) was wrong: it modeled the dvs chip as the SPI *slave* on both
interfaces, but both `spi_boot` and `spi_prog` are SPI **masters** —
`spi_boot` actively reads its own boot image from an external flash;
`spi_prog` actively streams RAM contents to an external device. A real SPI
master always drives `cs`/`sclk`/`mosi` itself and only listens on `miso` —
there's no external device handshaking back to synchronize against, so
"one channel rendezvous per bit" doesn't correspond to anything a real SPI
slave device would do. Fixed by making `cs`/`sclk`/`mosi`/`miso` plain
`bool` wires (not `chan`) — real signal lines, not rendezvous channels —
with the master side generating its own clock.

**Async clock generation:** the mechanism is request-attached-to-
acknowledge through a delay line — real, working, wired into both
peripherals via a new `core/peripherals/spi_clock.act`. Getting there took
three attempts:
1. `std::delay_lines::chain_delay_buffer` instantiated *inside*
   `spi_boot`/`spi_prog` themselves. Works in total isolation, but crashes
   actsim (`Assertion failed, file core.cc, line 2814: offset >= 0`, in its
   multi-driver-conflict detection pass) as soon as the *same* process also
   exposes a plain `bool` port to its own parent — true regardless of the
   delay mechanism (also hit with a hand-written `[after=N]` prs rule in
   the same position), and true even through a clean single-purpose wrapper
   process. Minimally reproduced outside this project's files; a genuine
   actsim limitation on hybrid `chp`/`prs` *port-level* boundaries, not a
   language issue.
2. Fix: keep `req`/`ack` as plain **ports** of `spi_boot`/`spi_prog`
   (never an internal instance touching `prs`), and move the delay line out
   into `spi_clock.act` — its own tiny, 100% structural process (no `chp`
   body at all) that whoever instantiates `spi_boot`/`spi_prog` wires up as
   a sibling. Neither process individually has "a port + local var touching
   prs" anymore, so the crash goes away — confirmed even when the wiring
   process itself has further ports up to its own parent, as long as that
   wiring process is purely structural (matches `harness.act`'s own style).
3. `chain_delay_buffer` itself (a real inverter-chain circuit) turned out
   to be unstable under the actual back-to-back toggling a full SPI
   transaction needs — `unstable transition`/`weak-interference` warnings
   and `ack` reading `X`, corrupting later bits. Persisted even at very
   large `N` (more stages doesn't fix a circuit not given time to settle
   between toggles). Fixed by switching `spi_clock.act` to an explicit
   `[after=N]` timing annotation directly in a flat `prs` rule (`req => ack+`
   / `~req => ack-`) instead — an ideal, simulator-only delay with none of
   the physical circuit's switch-level hazard dynamics.
   One more wrinkle: `ack` starts unknown (`X`) and only resolves once
   `req` has toggled *and the delay line has actually driven it* — if
   anything else runs first (e.g. `spi_prog`'s RAM read, which happens
   before its first real `sclk` edge), that first real toggle wedges
   forever waiting on an `ack` that never arrives. Fixed with a one-time
   throwaway priming toggle (`req := ~req; [ack=req]`, twice) at the very
   top of both peripherals' `chp`, before anything else touches req/ack.

`SPI_CLOCK_DELAY_N` (`core/globals.act`) is now a real `[after=N]` time
value (currently 20), not a delay-line chain length.

**spi_boot** (reworked a second time, in Stage 2.5, per direct feedback:
"we should not need an entire memory"): has **no backing store at all**.
Every CPU read is relayed live, address and all, as its own SPI
transaction to the external flash — genuine XIP, not "pull everything in
once, then serve from a copy." No `IMAGE_WORDS` template parameter either
— spi_boot doesn't know or care how big the image is; `software/
bootloader/main.c` (unchanged) runs directly out of it, one fetch at a
time, reads its own length-prefixed payload the same way, and copies it
into SRAM itself before jumping there. Protocol: 1 R/W bit (always 0,
since spi_boot never writes to the flash) + 20 address bits (the CPU's
real requested offset) + 32 data bits back over `miso`, all per CPU
transaction.

**spi_prog** (role reworked a second time, this time to stick): the
"RAM-facing bus master" design turned out to be a dead end — `soc.act`'s
RAM is entirely private (owned inside `soc`, reached only through `mmu`'s
own CPU-facing route), so a peer-level process like `spi_prog` had no port
to actually reach it through at all. Fixing that would have meant adding a
new external RAM-facing port to `soc.act` itself plus an arbiter merging
it with `mmu`'s own route — a real change to a shared, chip-agnostic core
file, for a design that was also just more machinery than needed. Instead:
`spi_prog` is a small **CPU-facing MMIO peripheral**, routed by the demux
exactly like `fifo_in`/`gpio`/`spi_boot` — no RAM access of its own at
all. Two registers: offset 0 (address register) stages a 20-bit address;
offset 4 (data register) triggers the actual SPI transaction — a write
stages 32 bits and sends them out (R/W=1 + staged address + data), a read
sends out a read command (R/W=0 + staged address) and blocks until the
32-bit result shifts back in over `miso`. This is how real, simple SPI
controller IP actually works: the CPU orchestrates RAM↔peripheral data
movement itself via ordinary loads/stores; the peripheral only shifts bits
over the external bus. `spi_prog` is still the SPI *master* externally
(drives `cs`/`sclk`/`mosi`, self-times `sclk` via `req`/`ack` + a
`spi_clock` sibling, same as `spi_boot`) — that part didn't change, only
which side decides what to send.

- [x] `core/peripherals/spi_boot.act` — as above.
- [x] `core/peripherals/spi_prog.act` — as above (now register-based, no
      template parameters).
- [x] `core/peripherals/spi_clock.act` — new; the real delay-line clock
      source, wired as a sibling wherever `spi_boot`/`spi_prog` are used.
- [x] `core/globals.act`: added `SPI_CLOCK_DELAY_N` (an `[after=N]` time
      value spi_clock.act uses, currently 20), `ADDR_SPI_BOOT` (=4) and
      `ADDR_SPI_PROG` (=6).
- [x] Rewrote unit tests to match: `tests/peripherals/spi_boot_test.act`
      wires `spi_boot` to a new `external_flash` process playing the slave
      role (gated purely by observing `cs`/`sclk`/`mosi` transitions, never
      rendezvousing with `spi_boot`), drives two CPU reads at different
      offsets, and `external_flash` answers by decoding the real requested
      offset each time (not a fixed sequence) — updated again in 2.5 once
      spi_boot dropped its backing store. `tests/peripherals/
      spi_prog_test.act` drives `spi_prog` as the CPU would (address
      register write, data register write, address register write, data
      register read) against a new `external_device` process playing the
      SPI slave role for both directions — checks the write it receives and
      answers the read with a known value that comes back correctly through
      spi_prog's own `rdata`.
- [x] **Gate:** `make test` (still 26 testbenches, both SPI tests rewritten
      in place) and `make software-tests` (38/38) both pass.

### 2.4 New demux wiring for dvs

Much simpler than originally scoped, now that 2.3 settled on `spi_prog`
being CPU-facing (not a RAM bus master) — no RAM arbitration or `soc.act`
changes needed at all, just two more ordinary demux routes.

- [x] `chips/dvs/harness.act`'s demux extended to 3 exact bases
      (`ADDR_SPI_BOOT`, `ADDR_AER`, `ADDR_SPI_PROG`), no catch-all (nothing
      here needs to reach further off-chip) — same shape as `chips/bench/
      harness.act`'s own table, still no `fifo_out` route. `spi_boot` and
      `spi_prog` each get their own `spi_clock` sibling (their `req`/`ack`
      ports can't share one — each peripheral drives its own `req`
      independently). Both peripherals' external pins
      (`spi_boot_cs`/`spi_boot_sclk`/`spi_boot_mosi`/`spi_boot_miso`,
      `spi_prog_*` likewise) are threaded out through `harness.act` and
      `chips/dvs/core.act` as real ports, same reasoning as `aer_in`.
- [x] Address map additions already landed in 2.3: `ADDR_SPI_BOOT` (=4),
      `ADDR_SPI_PROG` (=6), in `core/globals.act`.
- [x] **Gate:** compile-checked by fully elaborating `core<8>` (`aflat`
      on a throwaway instantiating file) — clean, only the pre-existing
      `mem.act` write-conflict warning. `make test`/`make software-tests`
      both still pass (nothing here is reachable from the existing suite
      yet — chips/dvs has no e2e tests of its own until 2.6).

### 2.5 Assemble `chips/dvs/core.act`

- [x] GPIO wired in, mirroring `chips/bench/core.act`'s pattern exactly:
      `chips/dvs/harness.act` gained a `gpio` instance (4th demux route,
      base=`ADDR_GPIO`) with `gpio_out_0..3` threaded through to `core.act`.
      `core.act`'s `event_id_1`/`event_id_2` ports were renamed to
      `gpio_in_0`/`gpio_in_1` (pure pass-through wiring onto those same two
      event lines, not a new mechanism) and `swd_0`/`swd_1` stub ports were
      added, unconnected, for interface completeness -- same as
      `chips/bench/core.act`'s own `gpio_in_0`/`gpio_in_1` ->
      `event_id_14`/`event_id_15` and `swd_0`/`swd_1`.
- [x] **Gate:** compile-checked by fully elaborating `core<8>` (`aflat`
      on a throwaway instantiating file) -- clean, only the pre-existing
      `mem.act` write-conflict warning. `make test`/`make software-tests`
      both still pass.
- [x] `spi_boot` reworked to drop its backing store entirely (see above) --
      direct feedback mid-stage: "we should not need an entire memory."
      Genuine XIP passthrough, no `IMAGE_WORDS`.
- [x] **Found and fixed a real bug in `core/soc.act`** (shared,
      chip-agnostic core file, not dvs-specific) while bringing up the
      first dvs e2e test. `running`, a plain `bool` used as a guard in
      `soc`'s top-level probed selection (`[| ... [] running -> skip
      [] ... |]`), was set once per instruction and expected to keep
      reading correctly indefinitely. It doesn't, on this actsim build:
      after `soc`'s own thread blocks on a channel rendezvous for a long
      stretch (confirmed with spi_boot's real, comparatively slow SPI
      transactions -- chips/bench's near-instant ROM fetch never blocks
      long enough to expose it), `running` reads back `false` -- its
      *initial* value -- instead of the `true` it was last written.
      Confirmed with a canary: an `int` set on the very next line after
      `running := true` survives the same gap correctly, isolating the bug
      to boolean guard variables specifically, not general state
      corruption. Root-caused, not worked around: removed a redundant
      second `running := true` (there were two -- one inside the
      `#reset_ext` branch, one unconditional right after the selection --
      only the second is actually needed) as a first pass, which didn't
      fix it alone, then restructured to stop depending on a long-held
      boolean guard entirely -- "idle vs. running" is now encoded by which
      block of code is executing (a one-time wait before the main loop,
      plus a repeated wait after WFI) rather than by re-reading a variable
      set arbitrarily far in the past. `running` no longer exists as a
      variable. The in-loop `reset_ext` check became `[ #reset_ext -> ...
      [] ~#reset_ext -> skip ]` -- ACT rejects `else` when a guard
      contains a channel probe, so the negated probe is spelled out
      explicitly instead. Verified `make test` (26/26) and
      `make software-tests` (38/38) still pass unchanged after the
      rewrite -- chips/bench never blocks long enough to have been
      exercising the buggy path either way.
- [x] `chips/dvs/Makefile`, modeled on `chips/bench/Makefile`: `e2e_boot_test`
      target (boots `software/boot_only/main.c` genuinely XIP out of
      spi_boot, checks for `soc`'s own "decoded wfi" log line, same
      completion convention as chips/bench's own `e2e_boot_test`).
- [x] `chips/dvs/tests/e2e/spi_flash_stub.act` -- new, shared test-support
      process playing spi_boot's external flash for e2e tests. Preloads
      the same `.mem` file `mem.act`'s `READ_ONLY` mode already reads
      (`sim::file`, no new file format or conversion tool needed) into an
      array, then answers by decoded address, not file position -- a real
      program's instruction fetches aren't monotonic (a loop's own
      backedge isn't), confirmed by testing a lazy sequential-only version
      first, which asserted the moment the bootloader's copy loop looped
      back.
- [x] `chips/dvs/tests/e2e/e2e_boot_test.act` -- passes end to end,
      including the real `software/bootloader/main.c` XIP-executing out of
      spi_boot and copying `software/boot_only/main.c`'s payload into SRAM
      before jumping there.
- [x] **Gate:** `make -C chips/dvs e2e_boot_test` passes; `make test`
      (26/26) and `make software-tests` (38/38) both still pass.

### 2.6 dvs-specific e2e tests

No separate SPI serialization script needed after all (originally
scoped as one, `tools/spi_serialize.py`) -- `spi_flash_stub.act` (2.5)
already reads the exact same `.mem` file every other ROM consumer in this
project uses, just serves it over a live SPI protocol instead of a direct
memory preload. Remaining work:

Ported what maps onto dvs's actual (deliberately reduced) topology; three
of chips/bench's seven e2e scenarios don't have a meaningful dvs
equivalent and were left out rather than forced:
- `e2e_multi_event_test`/`e2e_multi_event_reset_test` stress all 16 event
  lines — dvs's `core.act` only ever exposes 3 (`event_id_0`=AER,
  `event_id_1`/`event_id_2`=GPIO in); `event_id_3..15` are permanently
  unconnected by design (Stage 2.1), so a 16-line test has nothing to
  drive.
- `e2e_reset_reload_test` exercises `core/peripherals/rom_selector.act`'s
  bank-select register — chips/bench-specific hardware for swapping which
  of two preloaded ROM images is live. dvs has no equivalent register;
  which image `spi_flash_stub.act` serves is a simulation-harness choice
  (which file it opens), not something dvs's own hardware can select.

New software (mirrors the chips/bench program each is based on, adjusted
for dvs's addresses/event lines):
- `software/dvs_application/main.c` — `e2e_fifo_test` equivalent's
  program. Same ISR shape as `software/application/main.c` (read `BATCH`
  values, write back `+1` each), but reads from the AER input (base=5,
  `fifo_in`'s own interface, just a 20-bit element width) instead of
  `fifo_in` proper, and writes out through `spi_prog`'s two-register
  interface (stage an address at offset 0, write offset 4 to trigger the
  SPI transaction) instead of a plain push to `fifo_out`.
- `software/dvs_gpio_demo/main.c` — identical to `software/gpio_demo/
  main.c` except the vector vectors/enable bits target `event_id_1`/
  `event_id_2` (dvs's spare lines) instead of `event_id_14`/`event_id_15`.

New tests, all under `chips/dvs/tests/e2e/`, all booting genuinely XIP out
of `spi_boot` via `spi_flash_stub.act` (2.5) — no ROM:
- [x] `e2e_aer_test.act` — `e2e_fifo_test` equivalent. Two AER batches;
      each result checked as an (address, data) pair observed over SPI by
      a new `spi_sink` process (the receiving end chips/bench's output
      FIFO played). Waits for an explicit `done` signal from `spi_sink`
      before printing "test complete" — without it, the driver's own
      thread finishes issuing pushes and could print completion before
      `spi_sink`'s (still in-flight) checks on the later words actually
      run, letting a real failure slip past the Makefile's
      `grep "test complete"` gate.
- [x] `e2e_reset_test.act` — same program, one batch, `reset_ext`, identical
      batch again -- only passes if the rebooted bootloader + dvs_application
      genuinely re-registered its ISR vector, AER trigger level, and enable
      bit from a clean interrupt controller. Its own `spi_sink` variant hands
      each (address, data) pair straight back to the driver (rather than
      asserting internally) so the driver knows precisely when the
      pre-reset batch has finished, before it's safe to assert `reset_ext`.
- [x] `e2e_gpio_test.act` — direct structural port of chips/bench's own,
      `gpio_in_0`/`gpio_in_1` wired onto `event_id_1`/`event_id_2` instead
      of `event_id_14`/`event_id_15`.
- [x] Found and fixed a real bug along the way (`tests/peripherals/
      spi_prog_test.act`'s `external_device` had the same shape, but this
      is where it first surfaced end to end): a hand-rolled `spi_sink` in
      `e2e_aer_test.act` initially forgot to consume spi_prog's leading
      R/W bit before reading the address field, shifting every bit read by
      one position -- caught immediately by a garbage address value in the
      very first assertion, fixed by sampling and discarding that bit
      first, matching the existing `external_device` pattern.
- [x] **Coverage gap caught after the fact:** every test above (and both
      of `chips/bench`'s own equivalents) only ever drives `spi_prog`'s
      *write* direction -- `tests/peripherals/spi_prog_test.act` covers the
      read direction in isolation (no `soc` involved), but nothing
      exercised a real program, running through the real bootloader,
      actually pulling data in over SPI and using it. Closed with
      `software/dvs_spi_read_demo/main.c` (stages an address via
      `spi_prog`'s address register, reads its data register -- the read
      itself is what triggers the SPI transaction -- and drives a
      self-checking pattern onto GPIO: `0b1111` if the value matches a
      known constant, `0b0000` if not) and `e2e_spi_read_test.act` (new
      `external_source` process answers the read; the test just checks
      GPIO landed on `0b1111`).
- [x] `chips/dvs/Makefile` gained `e2e_aer_test`, `e2e_reset_test`,
      `e2e_gpio_test`, `e2e_spi_read_test`, and a `test` target running all
      five dvs e2e tests together (mirroring the top-level `make test`'s
      own role).
- [x] **Gate:** `make -C chips/dvs test` (all 5 green); `make test` (26/26)
      and `make software-tests` (38/38) at the top level both still pass
      unaffected.

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

## Backlog

- [ ] Configuring the SPI protocol with registers

- [ ] Implement UART equivalent implementation in place of SPI (?)

- [ ] Make the number of events templated in core

- [x] Confirm bootloader is XIP — done in Stage 2.5: `spi_boot` has no
      backing store, every CPU fetch (including the bootloader's own
      instructions) is a live SPI transaction; R/W bit convention (0=read,
      1=write) landed in Stage 2.3/2.5. `software/bootloader/main.c` reads
      its own length-prefixed payload the same way and copies it into SRAM
      itself, verified end to end by `e2e_boot_test`.

- [ ] untemplate the ram

- [x] Ensure programs can actually be loaded thru spi prog, and that data
      can be read/written — the narrow version of this (a real program,
      through the real bootloader, genuinely reading a value through
      `spi_prog` and using it) is done: `e2e_spi_read_test` (Stage 2.6).
      Still open: a *multi-word* load through `spi_prog` that actually
      writes the result into RAM (closer to "load a program"-scale, rather
      than one value staged through a GPIO pass/fail check).

- [x] SPI boot and SPI prog share the same clk instance. We have to find a way to test this reliably and safely. (using explicit delays )

- [ ] Reset is a channel communication

- [x] SOC is everything, core is SOC

- [ ] Use namespaces (import actnow; actnow::soc)

- [ ] What are the I/O standards I need to accomodate?

- [ ] Some sort of template act of how to add an I/O peripheral

      - [ ] Use that to implement a SPI interface

      - [ ] Implement an AXI Lite stream (Moritz)

- [ ] AVR datasheet 

## Back back log

- [ ] debugging



Once these are answered, expand this section into the same
checkbox/Gate structure as Stages 1 and 2 before starting implementation.


