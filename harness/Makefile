# Verilog/Vivado harness for the converted fpga core: RTL simulation of
# chips/fpga/core<4>, and the Vivado project-to-XSA flow around it.
#
#   make            - run every simulation scenario
#   make sim        - same
#   make boot       - run one scenario, e.g. make fifo / make reset / make reset_reload
#   make rtl        - regenerate gen/*.v from chips/fpga/core.act (chp2fpga)
#   make project    - create the Vivado project (fpga/vivado/actnow_proj.xpr)
#   make synth      - synthesize
#   make impl       - place & route
#   make xsa        - export the hardware platform (fpga/vivado/actnow.xsa)
#   make fpga       - project -> synth -> impl -> xsa, end to end
#   make clean      - remove simulation artifacts (add clean-vivado for fpga/vivado/)
#   make help       - full usage
#
# Must be run from this directory (harness/) -- fpga/tcl/*.tcl address the
# project as ./fpga/vivado/..., relative to Vivado's working directory.
#
# --- simulation ---------------------------------------------------------------
#
# Each scenario is the RTL analogue of the same-named ACT e2e test under
# chips/fpga/tests/e2e/ -- same chip, same compiled program, same stimulus, with
# the outside world (ROM, rom_selector, base-6 output FIFO, FIFO pushes) modelled
# in sim/tb_core.v instead of CHP:
#
#   boot          <- e2e_fpga_boot_test          software/boot_only
#   fifo          <- e2e_fpga_fifo_test          software/application
#   reset         <- e2e_fpga_reset_test         software/application
#   reset_reload  <- e2e_fpga_reset_reload_test  software/hang -> software/application
#
# The ACT tests get their ROM images from the file registry (gen/file_ids.act);
# there is no such indirection here -- tb_core.v $readmemb's "rom.mem" (and, for
# reset_reload, "rom_b.mem") straight out of the sim working directory, so each
# rule rebuilds software/build/rom.mem in place and stages it. That shared image
# slot is why the scenarios must not run in parallel (.NOTPARALLEL below).

ROOT  := $(CURDIR)/..
GEN   := $(CURDIR)/gen
SIM   := $(CURDIR)/sim
BUILD := $(SIM)/xsim_build
TB    := $(SIM)/tb_core.v

XVLOG := xvlog
XELAB := xelab
XSIM  := xsim

# RISC-V cross-compiler prefix for building the program images. Same
# auto-detection as the top-level Makefile; override with make CROSS=...
CROSS ?= $(firstword \
    $(foreach p,riscv32-unknown-elf- riscv64-unknown-elf- riscv-none-elf-, \
        $(if $(shell command -v $(p)gcc 2>/dev/null),$(p))) \
    riscv64-unknown-elf-)

# TRACE_ROM=1 logs every ROM fetch (~2k per boot) -- for debugging a boot that
# goes off the rails. TIMEOUT_NS bounds a run; the default (~2M cycles) is ~20x
# the longest scenario.
TRACE_ROM  ?= 0
TIMEOUT_NS ?= 20000000

# The generated RTL, minus func.v -- func.v is a bare set of functions that every
# module textually `include`s, so it must not be compiled as its own unit.
GEN_SRCS := $(filter-out $(GEN)/func.v,$(wildcard $(GEN)/*.v))

# --- Vivado -------------------------------------------------------------------

VIVADO       ?= vivado
VIVADO_FLAGS := -mode batch -nojournal -nolog
TCL          := fpga/tcl
VIVADO_DIR   := fpga/vivado
XPR          := $(VIVADO_DIR)/actnow_proj.xpr
XSA          := $(VIVADO_DIR)/actnow.xsa

.PHONY: all sim test boot fifo reset reset_reload rtl \
        project synth impl xsa fpga clean clean-vivado help
.NOTPARALLEL:

all: sim

sim test: boot fifo reset reset_reload
	@echo "=== all fpga RTL sim scenarios passed ==="

help:
	@echo "actnow -- Verilog/Vivado harness for the converted fpga core (chips/fpga/core<4>)"
	@echo ""
	@echo "Usage: make [target] [VAR=value ...]"
	@echo ""
	@echo "Simulation:"
	@echo "  make / make sim       run all four scenarios (RTL analogues of chips/fpga's e2e tests)"
	@echo "  make boot             boot software/boot_only XIP from the raw ROM route, run to WFI"
	@echo "  make fifo             two interrupt/FIFO batches through software/application"
	@echo "  make reset            batch, external reset, batch again (software/application)"
	@echo "  make reset_reload     boot software/hang, flip ROM bank, reset into software/application"
	@echo ""
	@echo "RTL generation:"
	@echo "  make rtl              regenerate gen/*.v from chips/fpga/core.act (convert_verilog.sh)"
	@echo ""
	@echo "Vivado flow (KR260/ZynqMP, part xck26-sfvc784-2LV-c):"
	@echo "  make project          create $(XPR) (static/ + gen/ + block design)"
	@echo "  make synth            synthesize            -> $(VIVADO_DIR)/reports/post_synth_*.rpt"
	@echo "  make impl             place & route         -> $(VIVADO_DIR)/reports/post_route_*.rpt"
	@echo "  make xsa              export the hardware platform (bitstream included) -> $(XSA)"
	@echo "  make fpga             project -> synth -> impl -> xsa"
	@echo ""
	@echo "Cleaning:"
	@echo "  make clean            remove the xsim build directory and run logs"
	@echo "  make clean-vivado     remove $(VIVADO_DIR)/ (the whole Vivado project)"
	@echo ""
	@echo "Variables:"
	@echo "  CROSS=<prefix>    RISC-V cross-compiler prefix (default: auto-detected)"
	@echo "  TRACE_ROM=1       log every ROM fetch"
	@echo "  TIMEOUT_NS=<ns>   simulated-time bound per sim run (default: $(TIMEOUT_NS))"
	@echo "  CHP2FPGA=<path>   chp2fpga binary used by 'make rtl' (default: the patched build)"
	@echo "  VIVADO=<path>     Vivado binary (default: vivado)"

# --- RTL generation -----------------------------------------------------------

# convert_verilog.sh defaults to the *patched* chp2fpga
# (/share/fpga_proto/chp/chp2fpga.x86_64_linux6_17_0). The stock build drops the
# data of enum-payload channels (mode_mem_t's op/size), which stalls every
# op/size-dependent path in the generated core, and emits an rr arbiter that
# starves soc's reset_ext branch. See sim/BUG2_mem_word_truncation.md.
rtl:
	@$(if $(CHP2FPGA),CHP2FPGA=$(CHP2FPGA) ,)./convert_verilog.sh

# --- simulation ---------------------------------------------------------------

$(BUILD):
	@mkdir -p $(BUILD)

# Compile the generated RTL + testbench once; every scenario elaborates the same
# work library with a different TEST parameter. -i $(GEN) so each module's
# `include "func.v"` resolves.
$(BUILD)/.compiled: $(GEN_SRCS) $(TB) | $(BUILD)
	@echo "--- xvlog: $(words $(GEN_SRCS)) generated modules + $(notdir $(TB)) ---"
	@cd $(BUILD) && $(XVLOG) --relax -i $(GEN) $(GEN_SRCS) $(TB)
	@touch $@

# $(1) = scenario name, $(2) = TEST id, $(3) = program for bank A (rom.mem),
# $(4) = program for bank B (rom_b.mem), empty for the single-bank scenarios.
#
# The software image slot (software/build/rom.mem) is shared and rebuilt in
# place, so it is staged into $(BUILD) immediately after each build -- same
# reason chips/fpga/Makefile rebuilds ROM_IMAGE per e2e rule.
define run_scenario
	@echo "--- $(1) ---"
	@rm -f $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT)/software PROG=$(3) CROSS=$(CROSS)
	@cp $(ROOT)/software/build/rom.mem $(BUILD)/rom.mem
	$(if $(4),@rm -f $(ROOT)/software/build/rom.mem)
	$(if $(4),@$(MAKE) -s -C $(ROOT)/software PROG=$(4) CROSS=$(CROSS))
	$(if $(4),@cp $(ROOT)/software/build/rom.mem $(BUILD)/rom_b.mem)
	@cd $(BUILD) && $(XELAB) tb_core -relax -s $(1)_snap --timescale 1ns/1ps \
	    -generic_top "TEST=$(2)" -generic_top "TRACE_ROM=$(TRACE_ROM)" \
	    -generic_top "TIMEOUT_NS=$(TIMEOUT_NS)" >/dev/null
	@cd $(BUILD) && $(XSIM) $(1)_snap -R | tee $(1).log
	@if grep -q "FAIL" $(BUILD)/$(1).log; then \
		echo "$(1): FAIL"; exit 1; \
	elif ! grep -q "PASS:" $(BUILD)/$(1).log; then \
		echo "$(1): FAIL (no completion)"; exit 1; \
	else \
		echo "$(1): PASS"; \
	fi
endef

boot: $(BUILD)/.compiled
	$(call run_scenario,boot,0,boot_only,)

fifo: $(BUILD)/.compiled
	$(call run_scenario,fifo,1,application,)

reset: $(BUILD)/.compiled
	$(call run_scenario,reset,2,application,)

reset_reload: $(BUILD)/.compiled
	$(call run_scenario,reset_reload,3,hang,application)

# --- Vivado flow --------------------------------------------------------------
#
# The three Tcl steps are the ones run_vivado_flow.sh drives, plus an XSA export
# (write_hw_platform -fixed -include_bit: the hardware handoff for Vitis /
# PetaLinux / a PYNQ overlay, bitstream embedded). Each opens and closes the
# project itself, so they compose in any order -- but synth/impl/xsa need the
# project to exist, hence the $(XPR) prereq.
# Nothing here re-runs on source changes: Vivado owns that decision inside the
# project (reset_run + launch_runs), so these stay phony and always run when asked.

project: $(XPR)

$(XPR): $(wildcard static/*.v) $(GEN_SRCS) $(TCL)/create_project.tcl $(TCL)/create_bd_skeleton.tcl
	@echo "=== Vivado: create project ==="
	$(VIVADO) $(VIVADO_FLAGS) -source $(TCL)/create_project.tcl

synth: $(XPR)
	@echo "=== Vivado: synthesis ==="
	$(VIVADO) $(VIVADO_FLAGS) -source $(TCL)/run_synth.tcl

impl: $(XPR)
	@echo "=== Vivado: implementation ==="
	$(VIVADO) $(VIVADO_FLAGS) -source $(TCL)/run_impl.tcl

xsa: $(XPR)
	@echo "=== Vivado: export hardware platform ==="
	$(VIVADO) $(VIVADO_FLAGS) -source $(TCL)/export_xsa.tcl

fpga: project synth impl xsa
	@echo "=== Vivado flow complete: $(XSA) ==="

# --- cleaning -----------------------------------------------------------------

clean:
	rm -drf $(BUILD)

clean-vivado:
	rm -drf $(VIVADO_DIR)
