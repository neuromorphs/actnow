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

**Async clock generation:** the textbook mechanism is request-attached-to-
acknowledge through a delay line (`std::delay_lines::chain_delay_buffer`,
`~/.local/act/act/std/delay_lines.act`) — a real `prs`-level circuit,
confirmed mixable with `chp` at the language level. It works in isolation
(verified standalone: `req := ~req; [ dl.out = req ]` produces correctly-
paced self-timed edges). But actsim crashes — `Assertion failed, file
core.cc, line 2814: offset >= 0`, in its multi-driver-conflict detection
pass — on *any* `chp`-only process that both exposes a plain `bool` port to
its parent and has a local variable connected (at any depth, even through
a clean single-purpose wrapper process) to real `prs` circuitry. Minimally
reproduced outside this project's files; looks like a genuine gap in this
actsim build's support for hybrid `chp`/`prs` *port-level* boundaries,
not a language issue. Decision: keep the real delay line as the intended
mechanism (documented in both files' header comments) but simulate with a
`SPI_CLOCK_DELAY_N`-bounded busy-wait loop (`core/globals.act`) as a
CHP-behavioral stand-in for now — swap it back once the actsim issue is
understood/fixed; nothing else about either peripheral needs to change
when that happens.

**spi_boot** (confirmed unchanged in role): answers `soc.act`'s hardcoded
`pc := ADDR_RESET` fetch (base=4). Now actively pulls `IMAGE_WORDS`
sequential words from an external SPI flash into its `mem<false, 0>`
backing store at boot (sending each word's address out over `mosi`,
reading the word back over `miso`) — a bounded loop, not a probed select
on `cs`, since `cs` is a wire now, not a channel. After the boot load,
falls into the same infinite CPU-read-servicing loop as before. No R/W bit
needed (spi_boot only ever reads).

**spi_prog** (role narrowed after explicit confirmation): stays a
**RAM-facing bus master** exactly as before (`addr`/`mode`/`wdata`/`rdata`
outward-facing, arbitrated into RAM alongside `spi_boot` in 2.4) — *not*
flipped into a CPU-facing slave, even though "replacing `fifo_out`'s role"
no longer quite applies, since a real SPI master can't also passively
receive commands over `mosi` telling it what a CPU wants. Instead it
decides its own address/data autonomously: a placeholder policy (walk a
`WORD_COUNT`-word window of RAM starting at `BASE_OFFSET` once, read each
word, push it out over SPI) stands in until the real triggering/addressing
policy is defined. Bounded to one pass, like `spi_boot`'s boot load — an
unbounded loop would keep generating events forever and never let
`actsim`'s `cycle` command (run to quiescence) return, hanging `make`.
Direction is fixed (RAM → external), so no `miso`/R-W-bit needed either.

- [x] `core/peripherals/spi_boot.act` — as above.
- [x] `core/peripherals/spi_prog.act` — as above.
- [x] `core/globals.act`: added `SPI_CLOCK_DELAY_N` (busy-wait bound,
      documented as a stand-in for the real delay line).
- [x] Rewrote unit tests to match: `tests/peripherals/spi_boot_test.act`
      now wires `spi_boot<2>` to a new `external_flash` process playing the
      slave role (gated purely by observing `cs`/`sclk`/`mosi` transitions,
      never rendezvousing with `spi_boot`), then checks the two words come
      back correctly via the CPU-facing port. `tests/peripherals/
      spi_prog_test.act` wires `spi_prog<8, 2>` to a `backend` (answers its
      RAM reads) and a new `external_sink` process (checks what gets pushed
      out over SPI matches expectations — since `spi_prog` never receives
      anything back, verification now lives entirely in `external_sink`'s
      asserts, not the top-level test driver).
- [x] **Gate:** `make test` (still 26 testbenches, both SPI tests rewritten
      in place) and `make software-tests` (38/38) both pass.

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

- [ ] `chips/dvs/harness.act` and `chips/dvs/core.act` already exist
      (started in 2.1, with just the AER input + 3-event topology, no
      `fifo_out`); this stage is "add the remaining pieces" (GPIO from 2.2,
      both SPI peripherals + the dvs demux from 2.3/2.4), not a from-scratch
      assembly.
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

## Backlog

- [ ] Configuring the SPI protocol with registers

- [ ] Implement UART equivalent implementation in place of SI

Once these are answered, expand this section into the same
checkbox/Gate structure as Stages 1 and 2 before starting implementation.


