/* See LICENSE for license details. */

/*****************************************************************************
 * fib.c
 *-----------------------------------------------------------------------------
 *
 * Executes two Fibonacci algorithms:
 *
 *   * fib_iter -- iterative, a data-dependent loop
 *   * fib_rec  -- recursive, real call stack traffic (SW/LW of ra across calls)
 *
 * No libc: common/crt-style setup is done by common/test_start.S, which sets
 * gp/sp, copies .data, zeroes .bss, then calls mytest(). Returning from
 * mytest() falls through into mytest_ret (WFI = pass); a mismatch traps via
 * EBREAK (fail), tagging x28 with the test number like the .S harness does.
 */

#define noinline __attribute__((noinline))

/* Iterative Fibonacci: fib(0)=0, fib(1)=1, ... */
static noinline int fib_iter(int n)
{
    int a = 0, b = 1;
    for (int i = 0; i < n; i++) {
        int t = a + b;
        a = b;
        b = t;
    }
    return a;
}

/* Recursive Fibonacci -- same result, via the call stack. */
static noinline int fib_rec(int n)
{
    if (n < 2)
        return n;
    return fib_rec(n - 1) + fib_rec(n - 2);
}

/* Fail path: record the test number in x28 (TESTNUM, as the .S harness does)
   and trap via EBREAK. soc logs "EBREAK -- test FAILED (see TESTNUM/x28)". */
static noinline void fail(int testnum)
{
    register int tn asm("x28") = testnum;
    asm volatile("ebreak" : : "r"(tn));
    __builtin_unreachable();
}

#define CHECK(testnum, got, want)            \
    do {                                     \
        if ((got) != (want))                 \
            fail(testnum);                   \
    } while (0)

/* Volatile input defeats constant-folding: gcc can't see the value, so it must
   actually call the fib routines at run time and let the core do the work. */
static volatile int in;

/* Entry point invoked by common/test_start.S. */
void mytest(void)
{
    in =  0; CHECK( 2, fib_iter(in),      0);
    in =  1; CHECK( 3, fib_iter(in),      1);
    in =  2; CHECK( 4, fib_iter(in),      1);
    in = 10; CHECK( 5, fib_iter(in),     55);
    in = 20; CHECK( 6, fib_iter(in),   6765);
    in = 30; CHECK( 7, fib_iter(in), 832040);

    in =  0; CHECK(10, fib_rec(in),       0);
    in =  1; CHECK(11, fib_rec(in),       1);
    in = 10; CHECK(12, fib_rec(in),      55);
    in = 13; CHECK(13, fib_rec(in),     233);
}
