# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Architecture

- **soc.act** — top-level. Contains inlined instruction fetch, decode, and
  ALU execution logic in a single CHP process. Instantiates `interrupt` and
  `regfile` as separate processes.
- **interrupt.act** — arbitrates 16 external event lines (`event_id_0` ..
  `event_id_15`) into a single vectored PC, sent to soc over the `event_pc`
  channel. Also sends an initial boot vector (`pc = 0`) once at startup.
- **regfile.act** — 32×32-bit register file, `x0` hardwired to zero (reads
  always return 0, writes are silently dropped). Talks to soc over a single
  bundled request/response packet (`reg_req` / `reg_resp`) rather than
  separate channels per field, so unused operands (e.g. a register an
  instruction doesn't read) don't cost a wasted transaction.
- **mem.act** — not yet implemented (stub). Instruction fetch is currently
  serviced directly by whatever testbench drives `imem_req`/`imem_resp`.

## Instructions implemented

### WFI — Wait For Interrupt (custom-0 opcode, `0b0001011` / `0x0B`)

Not a standard RV32I instruction — this project's own extension, placed in
the `custom-0` opcode slot the base ISA reserves for exactly this purpose.
Puts the core back into its low-power wait state; the core resumes only when
a new external event arrives (via `interrupt`), at which point it loads the
vectored PC for that event and starts executing again.

### R-type ALU instructions (opcode `0110011`, `OP`)

Two operands are read from the register file, computed on, and the result
is written back to `rd`. `funct3` selects the operation; `ADD`/`SUB` and
`SRL`/`SRA` additionally need `funct7` bit 30 to disambiguate.

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

**Not yet implemented:** `SLLI`/`SRLI`/`SRAI` (funct3 `001`/`101`) — these
encode a shift amount in the immediate field rather than a normal
sign-extended value, and aren't handled yet. Feeding one of these opcodes to
the core will currently fall through with no matching guard.

## Testbenches

- `init_wfi_test.act` — drives the wait → wake → `WFI` → wait loop via the
  external event lines.
- `alu_test.act` — exercises the R-type ALU function directly (no `soc`
  instantiation) against all 10 `OP` instructions, including signed/negative
  operand edge cases.
- `regfile_test.act` — exercises `regfile`'s packet interface directly:
  reads, writes, `x0` hardwiring, and combined read+write packets.
- `alu_op_test.act` — drives `soc` end-to-end through `ADDI` followed by
  `ADD`, to check a computed (non-zero) value threading through the
  register file and ALU together.

## Toolchain

Built and simulated against the `act`/`actsim` toolchain
(`asyncvlsi/act`).
