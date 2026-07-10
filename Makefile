# Compile and simulate the ACT testbenches under tests/.
#
#   make            - run every test in tests/
#   make test       - same as above
#   make wfi_test   - run just tests/wfi_test.act (works for any test by name)
#   make list       - list the test names make has discovered
#   make clean      - remove local simulator artifacts
#
# Must be run from this directory (actnow/) -- ACT resolves every `import`
# relative to the working directory the compiler was invoked from, not
# relative to the importing file, so soc.act's own imports only resolve
# correctly when this is the cwd. See the README's Toolchain section.

AFLAT  := aflat
ACTSIM := actsim

# e2e_fifo_test.act and e2e_multi_event_test.act are excluded here and given
# their own rules below -- each needs a specific ROM image (a different
# software/<name>/ program) that the shared $(FILE_REGISTRY_GEN)/$(ROM_IMAGE)
# prerequisite chain can't guarantee (see e2e_fifo_test's own rule comment
# for why).
TESTS := $(filter-out e2e_fifo_test e2e_multi_event_test,$(basename $(notdir $(wildcard tests/*.act))))
FILE_REGISTRY     := tests/files/file_registry.txt
FILE_REGISTRY_GEN := gen/file_ids.act gen/file_registry.conf

# Compiled program image consumed by tests/rom_program_test.act, registered as
# ROM_IMAGE in $(FILE_REGISTRY). It's a build artifact (see software/tests/), so
# the registry generator -- which requires every registered input to exist --
# depends on it below. ROM_TEST picks which program under software/tests/ to
# build (its .S must exist there); override on the command line to run another.
ROM_TEST  ?= simple
ROM_IMAGE := software/tests/build/rom_image.mem

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

# RV32I tests run through soc by `make software-tests`: the official RISC-V
# suite under software/tests/unit/ plus our own tests in software/tests/ (.S or
# .c compiled with rv32i gcc). Derived from the source files, excluding the
# M-extension ones (mul*/div*/rem*) -- this core decodes only base RV32I.
# Override to run a subset, e.g. make software-tests SW_TESTS="addi sub".
MEXT_TESTS := mul mulh mulhsu mulhu div divu rem remu
SW_TESTS   := $(filter-out $(MEXT_TESTS),$(basename $(notdir $(wildcard \
                  software/tests/*.S software/tests/*.c \
                  software/tests/unit/*.S software/tests/unit/*.c))))

.PHONY: all test list clean file-registry software-tests force e2e_fifo_test e2e_multi_event_test $(TESTS)

all: test

test: $(TESTS) e2e_fifo_test e2e_multi_event_test
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

$(FILE_REGISTRY_GEN): $(FILE_REGISTRY) tools/gen_file_registry.py $(ROM_IMAGE)
	@python3 tools/gen_file_registry.py $(FILE_REGISTRY) gen

list:
	@echo $(TESTS)

$(TESTS): $(FILE_REGISTRY_GEN)
	@echo "--- $@ ---"
	@$(AFLAT) tests/$@.act
	@out=$$(printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf tests/$@.act $@ 2>&1); \
	status=$$?; \
	echo "$$out"; \
	if [ $$status -ne 0 ]; then \
		echo "$@: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "$@: FAIL"; exit 1; \
	else \
		echo "$@: PASS"; \
	fi

# e2e_fifo_test.act exercises software/application/main.c's real
# interrupt/FIFO flow specifically -- it needs that exact ROM image, not
# whatever ROM_TEST happens to default to. $(ROM_IMAGE) is a shared file
# target that make only rebuilds once per invocation (the first time
# anything needs it) -- a plain prerequisite or target-specific variable
# can't force a *second* rebuild here if some earlier test in the same
# `make test` sweep already claimed it with a different ROM_TEST. And
# against a mismatched image, this doesn't fail loudly: the unconfigured
# interrupt vector just sends pc back to that image's own _start instead of
# a real ISR, this test's own fout.pop blocks forever, and actsim quiesces
# -- a false PASS from the assertion-grep, since nothing ever actually
# asserts. So: force a fresh image via an explicit sub-make (a genuinely
# separate invocation, unaffected by whatever the outer one already built),
# exactly like software-tests already does per test, then restore the
# default afterward so a later `make rom_program_test` stays deterministic.
e2e_fifo_test:
	@echo "--- e2e_fifo_test ---"
	@rm -f $(ROM_IMAGE) software/tests/build/rom.mem software/build/rom.mem
	@$(MAKE) -s ROM_TEST=application CROSS=$(CROSS) file-registry
	@$(AFLAT) tests/e2e_fifo_test.act
	@out=$$(printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf tests/e2e_fifo_test.act e2e_fifo_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROM_IMAGE) software/tests/build/rom.mem software/build/rom.mem; \
	$(MAKE) -s ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fifo_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fifo_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fifo_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fifo_test: PASS"; \
	fi

# Same rationale and pattern as e2e_fifo_test above, pinned to
# software/multi_event/main.c instead.
e2e_multi_event_test:
	@echo "--- e2e_multi_event_test ---"
	@rm -f $(ROM_IMAGE) software/tests/build/rom.mem software/build/rom.mem
	@$(MAKE) -s ROM_TEST=multi_event CROSS=$(CROSS) file-registry
	@$(AFLAT) tests/e2e_multi_event_test.act
	@out=$$(printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf tests/e2e_multi_event_test.act e2e_multi_event_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROM_IMAGE) software/tests/build/rom.mem software/build/rom.mem; \
	$(MAKE) -s ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_multi_event_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_multi_event_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_multi_event_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_multi_event_test: PASS"; \
	fi

# Run every RV32I software test through soc's real pipeline. For each test we
# rebuild the single shared ROM image slot (build/rom_image.mem) in place, run
# the (image-agnostic) rom_program_test, and classify from soc's log: reaching
# WFI = pass, EBREAK / assertion = fail, neither = did-not-complete. Serial, one
# image at a time. gen/ + rom_program_test are prepared once via the prereq and
# the single aflat; only actsim re-runs per test. The default ROM_TEST image is
# rebuilt at the end so a later `make rom_program_test` stays deterministic.
software-tests: $(FILE_REGISTRY_GEN)
	@echo "=== running $(words $(SW_TESTS)) RV32I software tests through soc ==="
	@$(AFLAT) tests/rom_program_test.act
	@pass=0; fail=0; failed=""; \
	for t in $(SW_TESTS); do \
		rm -f $(ROM_IMAGE) software/tests/build/rom.mem; \
		if ! $(MAKE) -s -C software/tests TEST=$$t BOOT=$(BOOT) CROSS=$(CROSS) >/dev/null 2>&1; then \
			echo "  $$t: BUILD-FAIL"; fail=$$((fail+1)); failed="$$failed $$t"; continue; \
		fi; \
		out=$$(printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf tests/rom_program_test.act rom_program_test 2>&1); \
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
