`timescale 1ns/1ps

// Answers the core's raw ROM read route (base 4, XIP boot) out of a BRAM whose
// other port is an AXI-BRAM-Ctrl the PS writes. That is what makes a firmware
// change a *file copy* rather than a bitstream rebuild:
//
//   PS: write software/build/rom.mem into the BRAM  ->  pulse reset_ext
//   core: reboots, fetches its reset vector from base 4, boots the new image
//
// which is precisely the e2e_fpga_reset_reload_test / `make reset_reload`
// scenario, in hardware.
//
// Channel protocol (chp2fpga): addr, then mode, then rdata; each transfer
// completes on a rising edge where valid & ready are both high. The core only
// ever reads here (mode.op is always R, size WORD), so mode is accepted and
// discarded -- the ROM route is read-only by construction (chips/fpga/harness.act
// leaves the demux's route-0 wdata unconnected).
//
// READ_LAT: BRAM read latency in clocks. blk_mem_gen without the optional output
// register is 1; 2 is used by default as margin (the address is held stable, so
// waiting an extra cycle only costs a cycle, never correctness).
module rom_bram_adapter #(
    parameter integer READ_LAT   = 2,
    parameter integer ADDR_WORDS = 8192   // BRAM depth in 32-bit words (32 KiB)
)(
    input  wire        clk,
    input  wire        rst,               // active-high, synchronous

    // core rom_* channel group
    output reg         rom_addr_ready,
    input  wire        rom_addr_valid,
    input  wire [3:0]  rom_addr_base,
    input  wire [15:0] rom_addr_offset,
    output reg         rom_mode_ready,
    input  wire        rom_mode_valid,
    output reg         rom_rdata_valid,
    input  wire        rom_rdata_ready,
    output reg  [31:0] rom_rdata,

    // BRAM port B (native), driven read-only
    output wire        bram_clk,
    output wire        bram_en,
    output wire [3:0]  bram_we,
    output wire [31:0] bram_addr,         // BYTE address, as AXI-BRAM-Ctrl expects
    output wire [31:0] bram_wrdata,
    input  wire [31:0] bram_rddata,

    // status
    output reg  [31:0] fetch_count
);
    localparam [1:0] S_ADDR = 2'd0, S_MODE = 2'd1, S_READ = 2'd2, S_DATA = 2'd3;

    reg [1:0]  st;
    reg [15:0] off_q;
    reg [3:0]  lat;

    // The BRAM is read-only from this side; the PS owns writes via port A.
    assign bram_clk    = clk;
    assign bram_en     = 1'b1;
    assign bram_we     = 4'b0000;
    assign bram_wrdata = 32'd0;
    // Word index = offset >> 2, back to a byte address for the BRAM controller.
    // Offsets beyond the BRAM simply wrap; a real out-of-range fetch means the
    // firmware image is bigger than ADDR_WORDS, which fetch_count + a stuck boot
    // will make obvious.
    localparam [31:0] ADDR_MASK = (ADDR_WORDS * 4) - 1;
    assign bram_addr   = {16'd0, off_q} & ADDR_MASK;

    always @(posedge clk) begin
        if (rst) begin
            st              <= S_ADDR;
            off_q           <= 16'd0;
            lat             <= 4'd0;
            rom_rdata       <= 32'd0;
            fetch_count     <= 32'd0;
        end else begin
            case (st)
                S_ADDR: if (rom_addr_valid) begin
                            off_q       <= rom_addr_offset;
                            fetch_count <= fetch_count + 1'b1;
                            st          <= S_MODE;
                        end
                S_MODE: if (rom_mode_valid) begin
                            lat <= 4'd0;
                            st  <= S_READ;
                        end
                S_READ: if (lat == READ_LAT[3:0]) begin
                            rom_rdata <= bram_rddata;
                            st        <= S_DATA;
                        end else begin
                            lat <= lat + 1'b1;
                        end
                S_DATA: if (rom_rdata_ready) st <= S_ADDR;
            endcase
        end
    end

    always @(*) begin
        rom_addr_ready  = (st == S_ADDR);
        rom_mode_ready  = (st == S_MODE);
        rom_rdata_valid = (st == S_DATA);
    end
endmodule
