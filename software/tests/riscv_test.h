#ifndef _ENV_PICORV32_TEST_H
#define _ENV_PICORV32_TEST_H

#ifndef TEST_FUNC_NAME
#  define TEST_FUNC_NAME mytest
#  define TEST_FUNC_TXT "mytest"
#  define TEST_FUNC_RET mytest_ret
#endif

#define RVTEST_RV32U
#define TESTNUM x28

/* Environment for our simulator: no console MMIO, no prints. Pass is signalled
   by test_start.S (custom instruction, opcode 0b0001011); fail traps here. */
#define RVTEST_CODE_BEGIN		\
	.text;				\
	.global TEST_FUNC_NAME;		\
	.global TEST_FUNC_RET;		\
TEST_FUNC_NAME:

#define RVTEST_PASS			\
	jal	zero,TEST_FUNC_RET;

#define RVTEST_FAIL			\
	ebreak;

#define RVTEST_CODE_END
#define RVTEST_DATA_BEGIN .balign 4;
#define RVTEST_DATA_END

#endif
