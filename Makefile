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

TESTS := $(basename $(notdir $(wildcard tests/*.act)))
FILE_REGISTRY     := tests/files/file_registry.txt
FILE_REGISTRY_GEN := gen/file_ids.act gen/file_registry.conf

# Compiled program image consumed by tests/rom_program_test.act, registered as
# ROM_IMAGE in $(FILE_REGISTRY). It's a build artifact (see software/tests/), so
# the registry generator -- which requires every registered input to exist --
# depends on it below. ROM_TEST picks which program under software/tests/ to
# build (its .S must exist there); override on the command line to run another.
ROM_TEST  ?= simple
ROM_IMAGE := software/tests/build/rom_image.mem

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

.PHONY: all test list clean file-registry software-tests force $(TESTS)

all: test

test: $(TESTS)
	@echo "=== all tests passed ==="

file-registry: $(FILE_REGISTRY_GEN)

$(ROM_IMAGE): force
	@rm -f $(ROM_IMAGE) software/tests/build/rom.mem
	$(MAKE) -C software/tests TEST=$(ROM_TEST) CROSS=$(CROSS)
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
		if ! $(MAKE) -s -C software/tests TEST=$$t CROSS=$(CROSS) >/dev/null 2>&1; then \
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
	$(MAKE) -s -C software/tests TEST=$(ROM_TEST) CROSS=$(CROSS) >/dev/null 2>&1 || true; \
	echo "=== software tests: $$pass passed, $$fail failed ==="; \
	if [ $$fail -ne 0 ]; then echo "failed:$$failed"; exit 1; fi

clean:
	rm -f .actsim_history
	rm -drf gen/
