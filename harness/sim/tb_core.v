`timescale 1ns/1ps

// Standalone functional testbench for the converted fpga core (harness/gen/
// core4.v = chp2fpga of chips/fpga/core<4>). No block design: this drives the
// clock/reset directly and models the external world the core boots against.
//
// It reproduces chips/fpga's ACT e2e boot (software/boot_only) at the RTL
// level: a behavioral ROM answers the raw rom_* read channel from the same
// compiled image, the core XIP-boots, the bootloader copies into the (internal)
// SRAM and jumps, and the program runs to WFI. Completion is observed on the
// core's own preserved decode signal (\is_wfi / \running inside \soc), the RTL
// analogue of soc.act's "decoded wfi" log line -- reachable here because those
// ACT variables survive conversion as named regs in gen/soc.v.
//
// chp2fpga channel convention: a transfer completes on a rising clock edge when
// *_valid & *_ready are both high (see gen/memf0.v's commu_compl = valid&ready).

module tb_core;

    // ---- clock / reset ----
    reg clk = 1'b0;
    reg rst = 1'b1;
    always #5 clk = ~clk;   // 100 MHz

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
    // io_* (core master, read/write) -- unused by boot_only; accept-and-drop
    wire        io_addr_valid;
    wire [3:0]  io_addr_base;
    wire [15:0] io_addr_offset;
    wire        io_mode_valid;
    wire        io_wdata_valid;
    wire [31:0] io_wdata;
    wire        io_rdata_ready;

    // ---- behavioral external ROM ----
    // 64 KiB address space (offset is 16 bits); one 32-bit word per entry.
    localparam integer ROM_WORDS = 16384;
    reg [31:0] rom_mem [0:ROM_WORDS-1];
    reg [15:0] off_q;

    localparam [1:0] S_ADDR = 2'd0, S_MODE = 2'd1, S_DATA = 2'd2;
    reg [1:0] rs;
    integer nfetch = 0;

    // combinational channel outputs from the ROM model
    always @(*) begin
        rom_addr_ready  = (rs == S_ADDR);
        rom_mode_ready  = (rs == S_MODE);
        rom_rdata_valid = (rs == S_DATA);
        rom_rdata       = rom_mem[off_q[15:2]];   // word index = offset >> 2
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
                            if (nfetch < 40)
                                $display("[%0t] ROM read #%0d: base=%0d offset=0x%04h -> 0x%08h",
                                         $time, nfetch, rom_addr_base, rom_addr_offset, rom_mem[rom_addr_offset[15:2]]);
                            rs <= S_MODE;
                        end
                S_MODE: if (rom_mode_valid) rs <= S_DATA;
                S_DATA: if (rom_rdata_ready) rs <= S_ADDR;
            endcase
        end
    end

    // ---- DUT ----
    \core4 uut (
         .\clock (clk)
        ,.\reset (rst)

        // reset_ext: input channel held idle
        ,.\reset_ext_ready ()
        ,.\reset_ext_valid (1'b0)
        ,.\reset_ext       (1'b0)

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

        // fifo_push: input channel held idle
        ,.\fifo_push_ready ()
        ,.\fifo_push_valid (1'b0)
        ,.\fifo_push       (32'b0)

        // io_* : accept-and-drop so a stray access can't wedge the run
        ,.\io_addr_valid   (io_addr_valid)
        ,.\io_addr_ready   (1'b1)
        ,.\io_addr.base    (io_addr_base)
        ,.\io_addr.offset  (io_addr_offset)
        ,.\io_mode_valid   (io_mode_valid)
        ,.\io_mode_ready   (1'b1)
        ,.\io_wdata_valid  (io_wdata_valid)
        ,.\io_wdata_ready  (1'b1)
        ,.\io_wdata        (io_wdata)
        ,.\io_rdata_ready  (io_rdata_ready)
        ,.\io_rdata_valid  (1'b0)
        ,.\io_rdata        (32'b0)
    );

    // ---- completion detection: the core's own decode signals ----
    // \is_wfi pulses high the cycle the WFI (or EBREAK) is decoded; boot_only
    // only ever hits WFI. \running drops to 0 and stays there once parked.
    wire dut_is_wfi  = uut.\s .\is_wfi ;
    wire dut_running = uut.\s .\running ;

    reg seen_running = 1'b0;
    integer cycles = 0;
    integer i;

    always @(posedge clk) begin
        if (!rst) begin
            cycles <= cycles + 1;
            if (dut_running) seen_running <= 1'b1;
            if (dut_is_wfi) begin
                $display("[%0t] PASS: core decoded WFI (is_wfi=1) after %0d cycles", $time, cycles);
                #50 $finish;
            end
        end
    end


    // ---- stimulus ----
    initial begin
        $display("=== tb_core: booting software/boot_only through the converted fpga core ===");
        // Zero-fill first, matching core/peripherals/mem.act's READ_ONLY preload
        // (it zero-fills SIZE_MEM_WORDS before loading the image). Without this,
        // reads past the image return X and poison pc/regs.
        for (i = 0; i < ROM_WORDS; i = i + 1) rom_mem[i] = 32'b0;
        // rom.mem: one 32-bit binary word per line (software/build/rom.mem);
        // run_sim.sh stages it next to the sim working dir.
        $readmemb("rom.mem", rom_mem);
        rst = 1'b1;
        repeat (8) @(posedge clk);
        rst = 1'b0;
        $display("[%0t] reset released", $time);
    end

    // ---- timeout ----
    initial begin
        #1_000_000;   // 1 ms @ 100 MHz = 100k cycles
        $display("FAIL: timeout -- core never reached WFI (seen_running=%0b)", seen_running);
        $finish;
    end

endmodule
