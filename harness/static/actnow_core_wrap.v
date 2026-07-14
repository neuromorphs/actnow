`timescale 1ns/1ps

// The converted ActNow chip plus the three adapters that give its CHP channels
// something to talk to:
//
//        AXI4-Stream events ─▶ fifo_push (base 5)  ┐
//   BRAM (PS-loaded firmware) ─▶ rom_*  (base 4)   ├─▶  \core4  ─▶ io_* (base 6) ─▶ AXI4-Stream results
//        AXI-GPIO reset bit  ─▶ reset_ext          ┘
//
// fifo_push needs no adapter at all: it is already a valid/ready channel with a
// 32-bit payload, which is AXI4-Stream minus the naming. The core's fifo_in
// raises the interrupt itself once the firmware's configured trigger level of
// events has landed, so "the FIFO filled up" *is* the interrupt -- nothing in
// the PL has to generate one.
module actnow_core_wrap (
    input  wire        clk,
    input  wire        rst,             // active-high, synchronous

    // events in (from evt_stream)
    input  wire        s_axis_tvalid,
    output wire        s_axis_tready,
    input  wire [31:0] s_axis_tdata,

    // results out (to AXI-DMA)
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire [31:0] m_axis_tdata,
    output wire        m_axis_tlast,

    // firmware BRAM, port B (port A is the PS's AXI-BRAM-Ctrl)
    output wire        bram_clk,
    output wire        bram_en,
    output wire [3:0]  bram_we,
    output wire [31:0] bram_addr,
    output wire [31:0] bram_wrdata,
    input  wire [31:0] bram_rddata,

    // control / status
    input  wire        reset_pulse,     // 0->1 edge: warm-reboot the core
    output wire [31:0] fetch_count,
    output wire [31:0] result_count,
    output wire [31:0] rd_err_count,
    output wire [31:0] reset_count
);
    // ---- core rom_* ----
    wire        rom_addr_valid, rom_addr_ready;
    wire [3:0]  rom_addr_base;
    wire [15:0] rom_addr_offset;
    wire        rom_mode_valid, rom_mode_ready;
    wire        rom_rdata_valid, rom_rdata_ready;
    wire [31:0] rom_rdata;

    // ---- core io_* ----
    wire        io_addr_valid, io_addr_ready;
    wire [3:0]  io_addr_base;
    wire [15:0] io_addr_offset;
    wire        io_mode_valid, io_mode_ready;
    wire [1:0]  io_mode_op;
    wire        io_wdata_valid, io_wdata_ready;
    wire [31:0] io_wdata;
    wire        io_rdata_valid, io_rdata_ready;
    wire [31:0] io_rdata;

    // ---- core reset_ext ----
    wire reset_ext_valid, reset_ext_ready, reset_ext_data;

    rom_bram_adapter #(.READ_LAT(2), .ADDR_WORDS(8192)) rom_i (
        .clk             (clk),
        .rst             (rst),
        .rom_addr_ready  (rom_addr_ready),
        .rom_addr_valid  (rom_addr_valid),
        .rom_addr_base   (rom_addr_base),
        .rom_addr_offset (rom_addr_offset),
        .rom_mode_ready  (rom_mode_ready),
        .rom_mode_valid  (rom_mode_valid),
        .rom_rdata_valid (rom_rdata_valid),
        .rom_rdata_ready (rom_rdata_ready),
        .rom_rdata       (rom_rdata),
        .bram_clk        (bram_clk),
        .bram_en         (bram_en),
        .bram_we         (bram_we),
        .bram_addr       (bram_addr),
        .bram_wrdata     (bram_wrdata),
        .bram_rddata     (bram_rddata),
        .fetch_count     (fetch_count)
    );

    // The result path *backpressures* (it does not drop): a result the core
    // computed must never be thrown away, and the core stalling on a store is
    // harmless -- the evt_stream feeding it drops instead, so the AER bus is
    // still never held up. The FIFO also packetizes, so the DMA hands each burst
    // of results to the PS as soon as it ends.
    wire        res_valid, res_ready;
    wire [31:0] res_data;

    axis_pack_fifo #(.DEPTH_LOG2(9), .PKT_WORDS(256)) res_fifo_i (
        .clk           (clk),
        .rst           (rst),
        .s_valid       (res_valid),
        .s_ready       (res_ready),
        .s_data        (res_data),
        .m_axis_tvalid (m_axis_tvalid),
        .m_axis_tready (m_axis_tready),
        .m_axis_tdata  (m_axis_tdata),
        .m_axis_tlast  (m_axis_tlast)
    );

    io_axis_adapter io_i (
        .clk             (clk),
        .rst             (rst),
        .io_addr_ready   (io_addr_ready),
        .io_addr_valid   (io_addr_valid),
        .io_addr_base    (io_addr_base),
        .io_addr_offset  (io_addr_offset),
        .io_mode_ready   (io_mode_ready),
        .io_mode_valid   (io_mode_valid),
        .io_mode_op      (io_mode_op),
        .io_wdata_ready  (io_wdata_ready),
        .io_wdata_valid  (io_wdata_valid),
        .io_wdata        (io_wdata),
        .io_rdata_valid  (io_rdata_valid),
        .io_rdata_ready  (io_rdata_ready),
        .io_rdata        (io_rdata),
        .m_axis_tvalid   (res_valid),
        .m_axis_tready   (res_ready),
        .m_axis_tdata    (res_data),
        .result_count    (result_count),
        .rd_err_count    (rd_err_count)
    );

    reset_ext_send rst_i (
        .clk             (clk),
        .rst             (rst),
        .pulse_in        (reset_pulse),
        .reset_ext_valid (reset_ext_valid),
        .reset_ext_data  (reset_ext_data),
        .reset_ext_ready (reset_ext_ready),
        .reset_count     (reset_count)
    );

    // The converted chip itself (chp2fpga of chips/fpga/core<4>, gen/core4.v).
    // Escaped names throughout -- chp2fpga emits ACT's dotted channel-field names
    // (\rom_addr.base) verbatim.
    \core4 core_i (
         .\clock (clk)
        ,.\reset (rst)

        ,.\reset_ext_ready (reset_ext_ready)
        ,.\reset_ext_valid (reset_ext_valid)
        ,.\reset_ext       (reset_ext_data)

        ,.\rom_addr_valid  (rom_addr_valid)
        ,.\rom_addr_ready  (rom_addr_ready)
        ,.\rom_addr.base   (rom_addr_base)
        ,.\rom_addr.offset (rom_addr_offset)
        ,.\rom_mode_valid  (rom_mode_valid)
        ,.\rom_mode_ready  (rom_mode_ready)
        ,.\rom_rdata_ready (rom_rdata_ready)
        ,.\rom_rdata_valid (rom_rdata_valid)
        ,.\rom_rdata       (rom_rdata)

        // base 5's input FIFO: an AXI4-Stream slave in all but name
        ,.\fifo_push_ready (s_axis_tready)
        ,.\fifo_push_valid (s_axis_tvalid)
        ,.\fifo_push       (s_axis_tdata)

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
        ,.\io_rdata_valid  (io_rdata_valid)
        ,.\io_rdata        (io_rdata)
    );
endmodule
