`timescale 1ns/1ps

// Standalone functional testbench for the converted fpga core (harness/gen/
// soc4.v = chp2fpga of chips/fpga/soc<4>). No block design: this drives the
// clock/reset directly and models the external world the core boots against.
//
// It is the RTL analogue of the four ACT e2e tests under chips/fpga/tests/e2e/:
// the same chip, the same compiled programs, the same scenarios -- only the
// "outside" (ROM, rom_selector, the base-6 output FIFO, the pushes into the
// input FIFO) is modelled here in Verilog instead of in CHP. Pick a scenario
// with the TEST parameter (harness/sim/Makefile does this for you):
//
//   TEST=0  boot          <- e2e_fpga_boot_test          (software/boot_only)
//   TEST=1  fifo          <- e2e_fpga_fifo_test          (software/application)
//   TEST=2  reset         <- e2e_fpga_reset_test         (software/application)
//   TEST=3  reset_reload  <- e2e_fpga_reset_reload_test  (hang -> application)
//
// The ROM image is read from "rom.mem" in the simulation working directory
// (the Makefile stages software/build/rom.mem there); reset_reload's corrected
// second bank comes from "rom_b.mem" alongside it.
//
// PASS/FAIL is reported on stdout and judged by the Makefile from the log.
//
// chp2fpga channel convention: a transfer completes on a rising clock edge when
// *_valid & *_ready are both high (see gen/memf0.v's commu_compl = valid&ready).

module tb_core;

    // ---- scenario selection ----
    // Override at elaboration: xelab -generic_top "TEST=1" ...
    parameter integer TEST = 0;
    localparam integer T_BOOT = 0, T_FIFO = 1, T_RESET = 2, T_RELOAD = 3;

    // Log every ROM fetch. Off by default (a boot is ~2k fetches); turn on with
    //   xelab -generic_top "TRACE_ROM=1" ...   (make TRACE_ROM=1 <target>)
    parameter integer TRACE_ROM = 0;

    // Give up after this much simulated time. Default ~2M cycles: boot_only
    // reaches WFI in ~12k, the application scenarios in ~100k.
    parameter integer TIMEOUT_NS = 20_000_000;

    // ---- clock / reset ----
    reg clk = 1'b0;
    reg rst = 1'b1;
    always #5 clk = ~clk;   // 100 MHz

    // op_mem_t's enum encoding, as emitted by chp2fpga (defenum op_mem_t
    // { W, R, R_AND_W } -> 0, 1, 2; see gen/memf0.v's \op comparisons).
    localparam [1:0] OP_W = 2'd0, OP_R = 2'd1;

    // ---- core interface wires ----
    // rom_* (core master, read-only)
    wire        rom_addr_valid;
    reg         rom_addr_ready;
    wire [3:0]  rom_addr_base;
    wire [15:0] rom_addr_offset;
    wire        rom_mode_valid;
    reg         rom_mode_ready;
    reg         rom_rdata_valid;
    wire        rom_rdata_ready;
    reg  [31:0] rom_rdata;
    // io_* (core master, read/write) -- base 6, where the program writes results
    wire        io_addr_valid;
    reg         io_addr_ready;
    wire [3:0]  io_addr_base;
    wire [15:0] io_addr_offset;
    wire        io_mode_valid;
    reg         io_mode_ready;
    wire [1:0]  io_mode_op;
    wire        io_wdata_valid;
    reg         io_wdata_ready;
    wire [31:0] io_wdata;
    wire        io_rdata_ready;
    // fifo_push (core slave) -- base 5's input event FIFO
    reg         fifo_push_valid;
    wire        fifo_push_ready;
    reg  [31:0] fifo_push_data;
    // reset_ext (core slave) -- warm reboot
    reg         reset_ext_valid;
    wire        reset_ext_ready;
    reg         reset_ext_data;

    // ---- behavioral external ROM (dual-bank) ----
    // 64 KiB address space (offset is 16 bits); one 32-bit word per entry.
    // Bank A is always the booted image; bank B exists only for the reload
    // scenario, where `bank_b` models core/peripherals/rom_selector.act's
    // flip_bank -- an external dual-bank boot flash, independent of reset.
    localparam integer ROM_WORDS = 16384;
    reg [31:0] rom_a [0:ROM_WORDS-1];
    reg [31:0] rom_b [0:ROM_WORDS-1];
    reg        bank_b = 1'b0;
    reg [15:0] off_q;

    wire [31:0] rom_word = bank_b ? rom_b[off_q[15:2]] : rom_a[off_q[15:2]];

    localparam [1:0] S_ADDR = 2'd0, S_MODE = 2'd1, S_DATA = 2'd2;
    reg [1:0] rs;
    integer nfetch = 0;

    // combinational channel outputs from the ROM model
    always @(*) begin
        rom_addr_ready  = (rs == S_ADDR);
        rom_mode_ready  = (rs == S_MODE);
        rom_rdata_valid = (rs == S_DATA);
        rom_rdata       = rom_word;               // word index = offset >> 2
    end

    always @(posedge clk) begin
        if (rst) begin
            rs    <= S_ADDR;
            off_q <= 16'b0;
        end else begin
            case (rs)
                S_ADDR: if (rom_addr_valid) begin
                            off_q <= rom_addr_offset;
                            nfetch <= nfetch + 1;
                            if (TRACE_ROM)
                                $display("[%0t] ROM read #%0d: bank=%0s base=%0d offset=0x%04h -> 0x%08h",
                                         $time, nfetch, bank_b ? "B" : "A", rom_addr_base, rom_addr_offset,
                                         bank_b ? rom_b[rom_addr_offset[15:2]] : rom_a[rom_addr_offset[15:2]]);
                            rs <= S_MODE;
                        end
                S_MODE: if (rom_mode_valid) rs <= S_DATA;
                S_DATA: if (rom_rdata_ready) rs <= S_ADDR;
            endcase
        end
    end

    // ---- behavioral base-6 output (the testbench's fifo_out) ----
    // core/peripherals/fifo_out.act's role: the program writes its results to
    // base 6, and the "outside" observes them. A write is addr, then mode, then
    // wdata (the demux's route-2 quad); a *read* of an output-only sink is a
    // bug, exactly as fifo_out asserts -- flagged here rather than answered
    // (io_rdata_valid is tied low, so an unanswered read would just stall).
    localparam integer MAX_RESULTS = 64;
    reg [31:0] results [0:MAX_RESULTS-1];
    integer nresults = 0;

    localparam [1:0] IO_ADDR = 2'd0, IO_MODE = 2'd1, IO_WDATA = 2'd2;
    reg [1:0] io_s;

    always @(*) begin
        io_addr_ready  = (io_s == IO_ADDR);
        io_mode_ready  = (io_s == IO_MODE);
        io_wdata_ready = (io_s == IO_WDATA);
    end

    always @(posedge clk) begin
        if (rst) begin
            io_s <= IO_ADDR;
        end else begin
            case (io_s)
                IO_ADDR: if (io_addr_valid) io_s <= IO_MODE;
                IO_MODE: if (io_mode_valid) begin
                             if (io_mode_op == OP_W) begin
                                 io_s <= IO_WDATA;
                             end else begin
                                 $display("FAIL: base-6 read attempted from the output-only route (op=%0d)", io_mode_op);
                                 #50 $finish;
                             end
                         end
                IO_WDATA: if (io_wdata_valid) begin
                             results[nresults] <= io_wdata;
                             nresults <= nresults + 1;
                             $display("[%0t] base-6 output #%0d: %0d (0x%08h)", $time, nresults, io_wdata, io_wdata);
                             io_s <= IO_ADDR;
                         end
            endcase
        end
    end

    // ---- DUT ----
    \soc4 uut (
         .\clock (clk)
        ,.\reset (rst)

        // reset_ext: driven by send_reset_ext (reset / reset_reload scenarios)
        ,.\reset_ext_ready (reset_ext_ready)
        ,.\reset_ext_valid (reset_ext_valid)
        ,.\reset_ext       (reset_ext_data)

        // rom_* read channel <-> behavioral ROM
        ,.\rom_addr_valid  (rom_addr_valid)
        ,.\rom_addr_ready  (rom_addr_ready)
        ,.\rom_addr.base   (rom_addr_base)
        ,.\rom_addr.offset (rom_addr_offset)
        ,.\rom_mode_valid  (rom_mode_valid)
        ,.\rom_mode_ready  (rom_mode_ready)
        ,.\rom_rdata_ready (rom_rdata_ready)
        ,.\rom_rdata_valid (rom_rdata_valid)
        ,.\rom_rdata       (rom_rdata)

        // fifo_push: driven by send_push (base 5's input event FIFO)
        ,.\fifo_push_ready (fifo_push_ready)
        ,.\fifo_push_valid (fifo_push_valid)
        ,.\fifo_push       (fifo_push_data)

        // io_* : base 6, captured by the output model above
        ,.\io_addr_valid   (io_addr_valid)
        ,.\io_addr_ready   (io_addr_ready)
        ,.\io_addr.base    (io_addr_base)
        ,.\io_addr.offset  (io_addr_offset)
        ,.\io_mode_valid   (io_mode_valid)
        ,.\io_mode_ready   (io_mode_ready)
        ,.\io_mode.op      (io_mode_op)
        ,.\io_wdata_valid  (io_wdata_valid)
        ,.\io_wdata_ready  (io_wdata_ready)
        ,.\io_wdata        (io_wdata)
        ,.\io_rdata_ready  (io_rdata_ready)
        ,.\io_rdata_valid  (1'b0)
        ,.\io_rdata        (32'b0)
    );

    // ---- completion detection: the core's own decode signal ----
    // \is_wfi pulses high the cycle the WFI (or EBREAK) is decoded. boot_only has
    // no peripheral interaction to observe, so -- exactly like e2e_fpga_boot_test,
    // which greps core's "decoded wfi" log line -- WFI *is* the pass condition
    // there. The application scenarios hit WFI on every ISR exit, so they judge
    // from the base-6 results instead.
    //
    // (core's \running bool is gone as of the core-dvs merge: reset_ext is now the
    // only way to boot, so there is no "am I running yet" state left to track.)
    wire dut_is_wfi = uut.\s .\is_wfi ;

    integer cycles = 0;
    integer i;

    always @(posedge clk) begin
        if (!rst) begin
            cycles <= cycles + 1;
            if (dut_is_wfi && TEST == T_BOOT) begin
                $display("[%0t] PASS: core decoded WFI (is_wfi=1) after %0d cycles", $time, cycles);
                #50 $finish;
            end
        end
    end

    // ---- channel drivers ----
    // Both input channels complete on the rising edge where valid & ready are
    // both high -- so sample the handshake exactly the way the generated RTL
    // does (commu_compl, registered at the posedge), then drop valid the moment
    // the completion is visible, before the next edge could repeat it.
    reg push_ack, reset_ext_ack;
    always @(posedge clk) begin
        push_ack      <= !rst && fifo_push_valid  && fifo_push_ready;
        reset_ext_ack <= !rst && reset_ext_valid  && reset_ext_ready;
    end

    task send_push(input [31:0] v);
        begin
            @(negedge clk);
            fifo_push_data  = v;
            fifo_push_valid = 1'b1;
            wait (push_ack);
            fifo_push_valid = 1'b0;
            $display("[%0t] pushed %0d into the input FIFO", $time, v);
            @(negedge clk);
        end
    endtask

    task send_reset_ext;
        begin
            @(negedge clk);
            reset_ext_data  = 1'b1;
            reset_ext_valid = 1'b1;
            wait (reset_ext_ack);
            reset_ext_valid = 1'b0;
            $display("[%0t] external reset accepted", $time);
            @(negedge clk);
        end
    endtask

    // ---- result checking (the fout.pop?result; assert(...) of the ACT tests) ----
    integer npopped = 0;

    function [31:0] pack_req_word(input [6:0] x, input [6:0] y, input pol);
        begin
            pack_req_word = ({25'd0, x} << 24) | ({25'd0, y} << 17) | {31'd0, pol};
        end
    endfunction

    function [31:0] rotate_req_word(input [31:0] word);
        integer x, y, tx, ty, rx, ry, nx, ny;
        begin
            x = word[30:24];
            y = word[23:17];
            tx = x - 63;
            ty = y - 56;
            rx = (tx - ty) >>> 1;
            ry = (tx + ty) >>> 1;
            nx = rx + 63;
            ny = ry + 56;
            if (nx < 0) nx = 0;
            if (nx > 125) nx = 125;
            if (ny < 0) ny = 0;
            if (ny > 111) ny = 111;
            rotate_req_word = (word & 32'h80_01_FF_FF) | (nx[6:0] << 24) | (ny[6:0] << 17);
        end
    endfunction

    task expect_result(input [31:0] want);
        begin
            while (nresults <= npopped) @(posedge clk);
            if (results[npopped] !== want) begin
                $display("FAIL: base-6 result %0d: want %0d, got %0d",
                         npopped, want, results[npopped]);
                #50 $finish;
            end
            npopped = npopped + 1;
        end
    endtask

    // One batch of the application's ISR contract: push BATCH=4 event words,
    // get each of them back with its requirements-ABI x/y coordinate rotated.
    task run_batch(input [31:0] a, input [31:0] b, input [31:0] c, input [31:0] d);
        begin
            $display("[%0t] --- batch: pushing 0x%08h, 0x%08h, 0x%08h, 0x%08h into the input FIFO ---",
                     $time, a, b, c, d);
            send_push(a);
            send_push(b);
            send_push(c);
            send_push(d);
            expect_result(rotate_req_word(a));
            expect_result(rotate_req_word(b));
            expect_result(rotate_req_word(c));
            expect_result(rotate_req_word(d));
            $display("[%0t] batch ok: rotated results returned from the base-6 output", $time);
        end
    endtask

    task pass(input [511:0] what);
        begin
            $display("[%0t] PASS: %0s (%0d cycles, %0d ROM fetches)", $time, what, cycles, nfetch);
            #50 $finish;
        end
    endtask

    // ---- stimulus ----
    initial begin
        fifo_push_valid = 1'b0;
        fifo_push_data  = 32'b0;
        reset_ext_valid = 1'b0;
        reset_ext_data  = 1'b0;

        // Zero-fill first, matching core/peripherals/mem.act's READ_ONLY preload
        // (it zero-fills SIZE_MEM_WORDS before loading the image). Without this,
        // reads past the image return X and poison pc/regs.
        for (i = 0; i < ROM_WORDS; i = i + 1) begin
            rom_a[i] = 32'b0;
            rom_b[i] = 32'b0;
        end
        // One 32-bit binary word per line (software/build/rom.mem); the Makefile
        // stages the right image(s) next to the sim working dir.
        $readmemb("rom.mem", rom_a);
        if (TEST == T_RELOAD) $readmemb("rom_b.mem", rom_b);

        case (TEST)
            T_BOOT:   $display("=== tb_core[boot]: booting software/boot_only through the converted fpga core ===");
            T_FIFO:   $display("=== tb_core[fifo]: two interrupt/FIFO batches through software/application ===");
            T_RESET:  $display("=== tb_core[reset]: batch, external reset, batch again (software/application) ===");
            T_RELOAD: $display("=== tb_core[reset_reload]: boot software/hang, flip ROM bank, reset into software/application ===");
            default:  begin $display("FAIL: unknown TEST=%0d", TEST); $finish; end
        endcase

        rst = 1'b1;
        repeat (8) @(posedge clk);
        rst = 1'b0;
        $display("[%0t] reset released", $time);

        // Cold boot. core.act blocks on reset_ext before executing anything -- there
        // is no implicit power-on-and-go -- so every scenario must assert it once
        // to boot the core at all, exactly as the ACT e2e tests now do. (In the
        // real KR260 build this is the PS pulsing gpio_ctrl bit 0; see
        // static/reset_ext_send.v.)
        $display("[%0t] --- cold boot: asserting reset_ext ---", $time);
        send_reset_ext();

        case (TEST)
            // boot_only: nothing more to drive; the WFI watcher above ends the run.
            T_BOOT: ;

            // Two batches through the program's real ISR, no reconfiguration in
            // between -- e2e_fpga_fifo_test.
            T_FIFO: begin
                run_batch(pack_req_word(7'd10, 7'd20, 1'b1),
                          pack_req_word(7'd11, 7'd20, 1'b0),
                          pack_req_word(7'd12, 7'd21, 1'b1),
                          pack_req_word(7'd13, 7'd21, 1'b0));
                repeat (3000) @(posedge clk);   // model real-world latency between events
                run_batch(pack_req_word(7'd30, 7'd40, 1'b1),
                          pack_req_word(7'd31, 7'd40, 1'b0),
                          pack_req_word(7'd32, 7'd41, 1'b1),
                          pack_req_word(7'd33, 7'd41, 1'b0));
                pass("two interrupt/FIFO batches completed");
            end

            // Batch, warm reset, batch again: only passes if the rebooted
            // application re-registered its ISR vector, trigger level and enable
            // bit from a clean interrupt controller -- e2e_fpga_reset_test.
            T_RESET: begin
                run_batch(pack_req_word(7'd10, 7'd20, 1'b1),
                          pack_req_word(7'd11, 7'd20, 1'b0),
                          pack_req_word(7'd12, 7'd21, 1'b1),
                          pack_req_word(7'd13, 7'd21, 1'b0));
                repeat (3000) @(posedge clk);
                $display("[%0t] --- asserting external reset ---", $time);
                send_reset_ext();
                run_batch(pack_req_word(7'd30, 7'd40, 1'b1),
                          pack_req_word(7'd31, 7'd40, 1'b0),
                          pack_req_word(7'd32, 7'd41, 1'b1),
                          pack_req_word(7'd33, 7'd41, 1'b0));
                pass("reboot after external reset confirmed");
            end

            // Dual-bank boot recovery: hang (bank A) is booted, does nothing;
            // flip the ROM bank to the corrected application (bank B), reset, and
            // run a batch -- e2e_fpga_reset_reload_test.
            T_RELOAD: begin
                // The cold boot above ran with rom_selector still on bank A, so the
                // core is executing the *broken* image -- the condition to recover from.
                $display("[%0t] --- booted into hang (bank A) -- letting it idle ---", $time);
                repeat (3000) @(posedge clk);
                $display("[%0t] --- operator notices no progress: flipping ROM bank to the corrected program ---", $time);
                bank_b = 1'b1;
                $display("[%0t] --- asserting external reset ---", $time);
                send_reset_ext();
                run_batch(pack_req_word(7'd10, 7'd20, 1'b1),
                          pack_req_word(7'd11, 7'd20, 1'b0),
                          pack_req_word(7'd12, 7'd21, 1'b1),
                          pack_req_word(7'd13, 7'd21, 1'b0));
                pass("recovery into the corrected program confirmed");
            end
        endcase
    end

    // ---- timeout ----
    initial begin
        #TIMEOUT_NS;
        $display("FAIL: timeout after %0d cycles -- %0d base-6 results, %0d ROM fetches (0 fetches = the core never booted: was reset_ext accepted?)",
                 cycles, nresults, nfetch);
        $finish;
    end

endmodule
