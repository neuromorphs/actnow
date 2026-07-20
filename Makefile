# Compile and simulate the ACT testbenches under tests/.
#
#   make            - run every test in tests/
#   make test       - same as above
#   make wfi_test   - run just tests/wfi_test.act (works for any test by name)
#   make list       - list the test names make has discovered
#   make clean      - remove local simulator artifacts
#   make help       - full usage, targets, and overridable variables
#
# Must be run from this directory (actnow/) -- ACT resolves every `import`
# relative to the working directory the compiler was invoked from, not
# relative to the importing file, so core/soc.act's own imports only resolve
# correctly when this is the cwd. See the README's Toolchain section.

AFLAT  := aflat
ACTSIM := actsim

# Tests live under tests/core (CPU/ISA datapath unit tests), tests/peripherals
# (standalone peripheral/infra unit tests), tests/regression (one-off
# bug-repro tests, kept separate so tests/peripherals stays one file per
# peripheral), and tests/sw (the generic real-program-through-soc runner).
# e2e tests (full boot + real compiled program + real peripheral
# interaction) live under chips/bench/tests/e2e/ instead -- they wire
# through one specific chip variant's harness (chips/bench/core.act), not
# the chip-agnostic tree here -- and are delegated to chips/bench/Makefile
# below, since each needs a specific ROM image (a different
# software/<name>/ program) that the shared
# $(FILE_REGISTRY_GEN)/$(ROM_IMAGE) prerequisite chain can't guarantee (see
# chips/bench/Makefile's own e2e_fifo_test rule comment for why).
TESTS := $(basename $(notdir $(wildcard tests/core/*.act tests/peripherals/*.act tests/regression/*.act tests/sw/*.act)))
FILE_REGISTRY     := tests/files/file_registry.txt
FILE_REGISTRY_GEN := gen/file_ids.act gen/file_registry.conf

# Resolves a bare test name (e.g. "alu_test") to its actual path under
# tests/core, tests/peripherals, tests/regression, or tests/sw -- lets the
# generic per-test rule work by name regardless of which subdirectory a test
# lives in. e2e tests aren't resolved here -- see chips/bench/Makefile.
TEST_SRC = $(firstword $(wildcard tests/core/$(1).act tests/peripherals/$(1).act tests/regression/$(1).act tests/sw/$(1).act))

# Compiled program image consumed by tests/sw/rom_program_test.act, registered as
# ROM_IMAGE in $(FILE_REGISTRY). It's a build artifact (see software/tests/), so
# the registry generator -- which requires every registered input to exist --
# depends on it below. ROM_TEST picks which program under software/tests/ to
# build (its .S must exist there); override on the command line to run another.
ROM_TEST  ?= simple
ROM_IMAGE := software/tests/build/rom_image.mem

# Two independent, permanently-registered ROM images (see e2e_reset_reload_test
# below): one program that's genuinely broken (an infinite loop, never
# services anything) and one that's correct (the same real interrupt/FIFO
# program e2e_fifo_test / e2e_reset_test already exercise). Both are built
# once up front, unlike $(ROM_IMAGE) which gets rebuilt in place per test --
# a rom_selector mux picks which one soc's ROM port actually talks to.
ROM_IMAGE_HANG        := software/hang/build/rom_image.mem
ROM_IMAGE_APPLICATION := software/application/build/rom_image.mem

# BOOT=1 builds the selected program bootloader-enabled: the bootloader copies it
# into internal SRAM and runs it there (fast memory) instead of executing in
# place from external ROM. Works with rom_program_test and software-tests.
# An app-style program under software/<name>/ (its own Makefile, crt0.S +
# application.lds -- see software/application/ or software/multi_event/) is
# always bootloader-loaded, regardless of this flag.
BOOT ?=

# RISC-V cross-compiler prefix for building program images. Auto-detected from
# PATH (core is RV32I, so a 32- or 64-bit multilib toolchain both work); falls
# back to riscv64-unknown-elf-. Override on the command line: make CROSS=...
CROSS ?= $(firstword \
    $(foreach p,riscv32-unknown-elf- riscv64-unknown-elf- riscv-none-elf-, \
        $(if $(shell command -v $(p)gcc 2>/dev/null),$(p))) \
    riscv64-unknown-elf-)

# KR260 hardware bring-up/run defaults. HOST_IP is the address the KR260 should
# send UDP packets back to; override it if your host is not 192.168.10.1.
KRIA      ?= kria.local
KRIA_USER ?= ubuntu
HOST_IP   ?= 192.168.10.1
UDP_PORT  ?= 3334
RAW_UDP_PORT ?= 3336
HTTP_PORT ?= 8088
XSA       ?= harness/fpga/vivado/actnow.xsa
FW_MEM    ?= software/build/rom.mem
DASHBOARD := harness/dashboard
DASH_PY   := $(DASHBOARD)/.venv/bin/python
DASH_VENV := $(DASHBOARD)/.venv/.installed
DASH_DIST := $(DASHBOARD)/frontend/dist/index.html

# RV32I tests run through soc by `make software-tests`: the official RISC-V
# suite under software/tests/unit/ plus our own tests in software/tests/ (.S or
# .c compiled with rv32i gcc). Derived from the source files, excluding the
# M-extension ones (mul*/div*/rem*) -- this core decodes only base RV32I.
# Override to run a subset, e.g. make software-tests SW_TESTS="addi sub".
MEXT_TESTS := mul mulh mulhsu mulhu div divu rem remu
SW_TESTS   := $(filter-out $(MEXT_TESTS),$(basename $(notdir $(wildcard \
                  software/tests/*.S software/tests/*.c \
                  software/tests/unit/*.S software/tests/unit/*.c))))

.PHONY: all test list clean help file-registry software-tests force dashboard kria-headless raw-viewer dashboard-deps dashboard-test e2e_fifo_test e2e_fifo_stress_test e2e_multi_event_test e2e_reset_test e2e_reset_reload_test e2e_gpio_test e2e_boot_test e2e_multi_event_reset_test $(TESTS)

all: test

help:
	@echo "actnow -- ACT/CHP RV32I core test runner"
	@echo ""
	@echo "Usage: make [target] [VAR=value ...]"
	@echo ""
	@echo "Targets:"
	@echo "  make / make all / make test  run every test: tests/core, tests/peripherals,"
	@echo "                               tests/regression, tests/sw, plus all seven e2e tests"
	@echo "  make <name>                  run a single test by name, e.g. make wfi_test"
	@echo "                               (any test under tests/core, tests/peripherals,"
	@echo "                               tests/regression, or tests/sw)"
	@echo "  make list                    list every test name make has discovered"
	@echo "  make software-tests          run the full RV32I suite (riscv-tests + custom)"
	@echo "                               through soc's real fetch/decode/execute pipeline"
	@echo "  make e2e_fifo_test           boot + interrupt/FIFO e2e test (application)"
	@echo "  make e2e_multi_event_test    boot + all-16-events e2e test (multi_event)"
	@echo "  make e2e_reset_test          boot, run a batch, reset, reboot + run a 2nd batch"
	@echo "  make e2e_reset_reload_test   boot a hung program, flip ROM banks, reset into a"
	@echo "                               corrected program, and confirm it runs correctly"
	@echo "  make e2e_gpio_test           boot + GPIO in/out e2e test (gpio_demo)"
	@echo "  make e2e_boot_test           boot-only e2e test, zero peripheral interaction"
	@echo "  make e2e_multi_event_reset_test  16-event back-to-back pressure across reset"
	@echo "  make rom_program_test        run one program image through soc (see ROM_TEST)"
	@echo "  make file-registry           (re)generate gen/file_ids.act + gen/file_registry.conf"
	@echo "  make kria-headless       run the original terminal/diagnostic viewer"
	@echo "  make dashboard               build/deploy and open the live coding dashboard"
	@echo "  make raw-viewer              open the independent raw DVS UDP viewer"
	@echo "  make dashboard-test          type-check/build the UI and run dashboard tests"
	@echo "  make clean                   remove local simulator artifacts (gen/, history)"
	@echo "  make help                    show this message"
	@echo ""
	@echo "Variables (override on the command line, e.g. make BOOT=1 ROM_TEST=addi rom_program_test):"
	@echo "  ROM_TEST=<name>   program to build for rom_program_test (default: simple)"
	@echo "  BOOT=1            run the selected program from internal SRAM via the bootloader"
	@echo "                    instead of executing in place from external ROM"
	@echo "  CROSS=<prefix>    RISC-V cross-compiler prefix (default: auto-detected from PATH)"
	@echo "  SW_TESTS=\"...\"    subset of programs for software-tests (default: all non-M-ext)"
	@echo "  KRIA=<host>       KR260 SSH host for dashboard (default: $(KRIA))"
	@echo "  HOST_IP=<ip>      host UDP address passed to the KR260 (default: $(HOST_IP))"
	@echo "  RAW_UDP_PORT=<n>  independent raw-event UDP port (default: $(RAW_UDP_PORT))"
	@echo ""
	@echo "Must be run from this directory (actnow/) -- see the top of this Makefile and"
	@echo "the README's Toolchain section for why."

test: $(TESTS) e2e_fifo_test e2e_fifo_stress_test e2e_multi_event_test e2e_reset_test e2e_reset_reload_test e2e_gpio_test e2e_boot_test e2e_multi_event_reset_test
	@echo "=== all tests passed ==="

file-registry: $(FILE_REGISTRY_GEN)

$(ROM_IMAGE): force
	@rm -f $(ROM_IMAGE) software/tests/build/rom.mem software/build/rom.mem
ifneq ($(wildcard software/$(ROM_TEST)/Makefile),)
	@mkdir -p $(dir $(ROM_IMAGE))
	$(MAKE) -C software PROG=$(ROM_TEST) CROSS=$(CROSS)
	sed 's/^/0b/' software/build/rom.mem > $(ROM_IMAGE)
else
	$(MAKE) -C software/tests TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS)
endif
force:

dashboard-deps: $(DASH_VENV) $(DASH_DIST)

dashboard-test: dashboard-deps
	$(DASH_PY) -m unittest discover -s $(DASHBOARD)/tests -v
	cd $(DASHBOARD)/frontend && npm exec tsc -- --noEmit

$(DASH_VENV): $(DASHBOARD)/requirements.txt
	python3 -m venv $(DASHBOARD)/.venv
	$(DASHBOARD)/.venv/bin/pip install -r $<
	touch $@

$(DASHBOARD)/frontend/node_modules: $(DASHBOARD)/frontend/package.json
	cd $(DASHBOARD)/frontend && npm install

$(DASH_DIST): $(DASHBOARD)/frontend/node_modules $(shell find $(DASHBOARD)/frontend/src -type f) $(DASHBOARD)/frontend/index.html
	cd $(DASHBOARD)/frontend && npm run build

dashboard: dashboard-deps
	$(MAKE) -C software PROG=application CROSS=$(CROSS)
	$(DASH_PY) $(DASHBOARD)/backend/dashboard.py \
		--kria $(KRIA) \
		--user $(KRIA_USER) \
		--listen-host $(HOST_IP) \
		--udp-port $(UDP_PORT) \
		--raw-udp-port $(RAW_UDP_PORT) \
		--http-port $(HTTP_PORT) \
		--xsa $(XSA) \
		--static $(DASHBOARD)/frontend/dist

kria-headless:
	$(MAKE) -C software PROG=application CROSS=$(CROSS)
	python3 harness/host/actnow_client.py \
		--kria $(KRIA) \
		--user $(KRIA_USER) \
		--listen-host $(HOST_IP) \
		--port $(UDP_PORT) \
		--raw-port $(RAW_UDP_PORT) \
		--xsa $(XSA) \
		--firmware $(FW_MEM) \
		--headless

raw-viewer:
	python3 harness/host/actnow_raw_viewer.py --port $(RAW_UDP_PORT)

# Built once, not force-rebuilt per test like $(ROM_IMAGE) above -- these two
# are permanent fixtures for e2e_reset_reload_test, not swapped out per run.
# Each still lists its program's main.c as a prerequisite so a genuinely
# stale fixture (main.c edited since the last build) gets rebuilt rather
# than silently reused -- without this, `make` only checks whether the
# target file exists at all, never whether it matches current source.
$(ROM_IMAGE_HANG): software/hang/main.c
	@mkdir -p $(dir $@)
	@rm -f software/build/rom.mem
	$(MAKE) -C software PROG=hang CROSS=$(CROSS)
	sed 's/^/0b/' software/build/rom.mem > $@

$(ROM_IMAGE_APPLICATION): software/application/main.c
	@mkdir -p $(dir $@)
	@rm -f software/build/rom.mem
	$(MAKE) -C software PROG=application CROSS=$(CROSS)
	sed 's/^/0b/' software/build/rom.mem > $@

$(FILE_REGISTRY_GEN): $(FILE_REGISTRY) tools/gen_file_registry.py $(ROM_IMAGE) $(ROM_IMAGE_HANG) $(ROM_IMAGE_APPLICATION)
	@python3 tools/gen_file_registry.py $(FILE_REGISTRY) gen

list:
	@echo $(TESTS)

$(TESTS): $(FILE_REGISTRY_GEN)
	@echo "--- $@ ---"
	@$(AFLAT) $(call TEST_SRC,$@)
	@out=$$(printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf $(call TEST_SRC,$@) $@ 2>&1); \
	status=$$?; \
	echo "$$out"; \
	if [ $$status -ne 0 ]; then \
		echo "$@: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "$@: FAIL"; exit 1; \
	else \
		echo "$@: PASS"; \
	fi

# e2e tests wire through chips/bench specifically (chips/bench/core.act) and
# are defined + built there -- chips/bench/Makefile owns the ROM_IMAGE
# rebuild dance each one needs (see its own e2e_fifo_test comment for why),
# this just delegates by name with the same variables a direct invocation
# would use.
e2e_fifo_test e2e_fifo_stress_test e2e_multi_event_test e2e_reset_test e2e_reset_reload_test e2e_gpio_test e2e_boot_test e2e_multi_event_reset_test:
	@$(MAKE) -C chips/bench $@ ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS)

# Run every RV32I software test through soc's real pipeline. For each test we
# rebuild the single shared ROM image slot (build/rom_image.mem) in place, run
# the (image-agnostic) rom_program_test, and classify from soc's log: reaching
# WFI = pass, EBREAK / assertion = fail, neither = did-not-complete. Serial, one
# image at a time. gen/ + rom_program_test are prepared once via the prereq and
# the single aflat; only actsim re-runs per test. The default ROM_TEST image is
# rebuilt at the end so a later `make rom_program_test` stays deterministic.
software-tests: $(FILE_REGISTRY_GEN)
	@echo "=== running $(words $(SW_TESTS)) RV32I software tests through soc ==="
	@$(AFLAT) tests/sw/rom_program_test.act
	@pass=0; fail=0; failed=""; \
	for t in $(SW_TESTS); do \
		rm -f $(ROM_IMAGE) software/tests/build/rom.mem; \
		if ! $(MAKE) -s -C software/tests TEST=$$t BOOT=$(BOOT) CROSS=$(CROSS) >/dev/null 2>&1; then \
			echo "  $$t: BUILD-FAIL"; fail=$$((fail+1)); failed="$$failed $$t"; continue; \
		fi; \
		out=$$(printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf tests/sw/rom_program_test.act rom_program_test 2>&1); \
		if echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
			echo "  $$t: FAIL"; fail=$$((fail+1)); failed="$$failed $$t"; \
		elif echo "$$out" | grep -qi "decoded wfi"; then \
			echo "  $$t: PASS"; pass=$$((pass+1)); \
		else \
			echo "  $$t: FAIL (no completion)"; fail=$$((fail+1)); failed="$$failed $$t"; \
		fi; \
	done; \
	rm -f $(ROM_IMAGE) software/tests/build/rom.mem; \
	$(MAKE) -s -C software/tests TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) >/dev/null 2>&1 || true; \
	echo "=== software tests: $$pass passed, $$fail failed ==="; \
	if [ $$fail -ne 0 ]; then echo "failed:$$failed"; exit 1; fi

clean:
	rm -f .actsim_history
	rm -drf gen/
