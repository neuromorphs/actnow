# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Architecture

- **soc.act** — top-level. Contains inlined instruction fetch, decode, and
  execution logic in a single CHP process. Instantiates `interrupt` and
  `regfile` as separate processes, and calls into the shared `alu` function
  from `alu.act` for every ALU-computational instruction.
- **alu.act** — a single shared `alu(a, b, funct3, funct7)` function used by
  every R-type and I-type computational instruction (including the shift-
  immediates). Decode is responsible for resolving what `a`/`b`/`funct7`
  mean for a given instruction before calling it; the ALU itself doesn't know
  or care whether it's an R-type op, a normal immediate, or a shift amount.
  This keeps exactly one adder/comparator/shifter/etc in the design instead
  of one per instruction family.
- **interrupt.act** — arbitrates 16 external event lines (`event_id_0` ..
  `event_id_15`) into a single vectored PC, sent to soc over the `event_pc`
  channel. Also sends an initial boot vector (`pc = 0`) once at startup.
- **regfile.act** — 32×32-bit register file, `x0` hardwired to zero (reads
  always return 0, writes are silently dropped). Talks to soc over a single
  bundled request/response packet (`reg_req` / `reg_resp`) rather than
  separate channels per field, so unused operands (e.g. a register an
  instruction doesn't read) don't cost a wasted transaction. `regfile` only
  sends a response when the request actually asked for a read
  (`rs1_valid | rs2_valid`) — a write-only request gets no response at all,
  saving a handshake.
- **mem.act** — not yet implemented (stub). Instruction fetch is currently
  serviced directly by whatever testbench drives `imem_req`/`imem_resp`.

## Instructions implemented

### WFI — Wait For Interrupt (custom-0 opcode, `0b0001011` / `0x0B`)

Not a standard RV32I instruction — this project's own extension, placed in
the `custom-0` opcode slot the base ISA reserves for exactly this purpose.
Puts the core back into its low-power wait state; the core resumes only when
a new external event arrives (via `interrupt`), at which point it loads the
vectored PC for that event and starts executing again.

### LUI — Load Upper Immediate (opcode `0110111`)

`rd := imm[31:12] << 12`. Builds a 32-bit constant by placing a 20-bit
immediate into the upper bits of `rd` and zeroing the lower 12.

### R-type ALU instructions (opcode `0110011`, `OP`)

Two operands are read from the register file, computed on via `alu`, and the
result is written back to `rd`. `funct3` selects the operation; `ADD`/`SUB`
and `SRL`/`SRA` additionally need `funct7` bit 30 to disambiguate.

| Instr | funct3 | funct7 bit 30 | Operation |
|---|---|---|---|
| ADD  | 000 | 0 | `rd := rs1 + rs2` |
| SUB  | 000 | 1 | `rd := rs1 - rs2` |
| SLL  | 001 | — | `rd := rs1 << rs2[4:0]` |
| SLT  | 010 | — | `rd := (rs1 <s rs2) ? 1 : 0` (signed) |
| SLTU | 011 | — | `rd := (rs1 <u rs2) ? 1 : 0` (unsigned) |
| XOR  | 100 | — | `rd := rs1 ^ rs2` |
| SRL  | 101 | 0 | `rd := rs1 >> rs2[4:0]` (logical) |
| SRA  | 101 | 1 | `rd := rs1 >>> rs2[4:0]` (arithmetic, sign-extending) |
| OR   | 110 | — | `rd := rs1 \| rs2` |
| AND  | 111 | — | `rd := rs1 & rs2` |

### I-type ALU instructions (opcode `0010011`, `OP-IMM`)

Same operations as above, but the second operand is a sign-extended 12-bit
immediate from the instruction rather than `rs2`.

| Instr  | funct3 | Operation |
|---|---|---|
| ADDI  | 000 | `rd := rs1 + imm` |
| SLTI  | 010 | `rd := (rs1 <s imm) ? 1 : 0` (signed) |
| SLTIU | 011 | `rd := (rs1 <u imm) ? 1 : 0` (unsigned) |
| XORI  | 100 | `rd := rs1 ^ imm` |
| ORI   | 110 | `rd := rs1 \| imm` |
| ANDI  | 111 | `rd := rs1 & imm` |

**SLLI / SRLI / SRAI** (funct3 `001`/`101`) are also implemented, but don't
use the normal sign-extended immediate path. For these three, the field
encodes `shamt` (bits `[24:20]`, i.e. `rs2`'s position) plus a funct7-shaped
variant selector (bits `[31:25]`), exactly like the R-type shift
instructions. Decode routes them through `alu` with `b = int(rs2, 32)` and
`funct7` passed straight through, reusing the exact same `SLL`/`SRL`/`SRA`
logic rather than duplicating it.

## Testbenches

All four live under `tests/` and use `assert(condition, "message", ...)` for
pass/fail — a failing check prints `ASSERTION failed: ...`; passing checks
are silent.

- `wfi_test.act` — drives the wait → wake → `WFI` → wait loop via the
  external event lines.
- `alu_test.act` — probes the shared `alu` function directly (no `soc`
  instantiation), covering all 10 R-type `OP` instructions plus all 9 I-type
  `OP-IMM` instructions (including `SLLI`/`SRLI`/`SRAI` via the shamt-reuse
  trick), with signed/negative operand edge cases. 34 assertions.
- `regfile_test.act` — exercises `regfile`'s packet interface directly:
  reads, writes, `x0` hardwiring, blocked writes to `x0`, and combined
  read+write packets.
- `lui_test.act` — drives `soc` end-to-end through a `LUI` instruction. Note:
  the testbench can only assert on the instruction word it encodes itself —
  `regfile` lives privately inside `soc` with no readback path, so the
  actual write result is only visible via `regfile`'s own debug `log`, not
  something the test can assert on directly.

## Instructions to implement:
### J-type instructions
- JAL 
- JALR
### B-type instructions
- BEQ
- BNE
- BLT
- BGE
- BLTU
- BGEU
### Loads
- LB
- LH
- LW
- LBU
- LHU
### Stores
- SB
- SH
- SW
### Misc ( I don't think we need these , do we ? )
- FENCE
- FENCE.TSO
- PAUSE
- ECALL
- EBREAK

### another thing to keep in mind is 

## Toolchain

Built and simulated against the `act`/`actsim` toolchain (`asyncvlsi/act`).

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
