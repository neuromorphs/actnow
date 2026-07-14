`timescale 1ns/1ps

// Synthesizable top for the KR260 build: the block design (PS, 2x AXI-DMA,
// AXI-BRAM-Ctrl + firmware BRAM, AXI-GPIO -- see fpga/tcl/create_bd_aer.tcl)
// wrapped around actnow_pl (the AER receiver, the event pipeline, and the
// converted ActNow core with its adapters).
//
// This top used to tie every core channel idle, which meant synthesis optimized
// the entire core away (0 LUTs, 0 registers). Everything is genuinely connected
// now, so this is also the first build that yields real utilization and timing
// numbers for \core4.
//
// The AER bus is the only signal group that leaves the chip: 9 data lines +
// /REQ in, /ACK out, LVCMOS33 on the RPi header -- see fpga/xdc/kr260_aer_wired.xdc,
// taken unchanged from kr260_aer_interface (including the AER3<->AER5 correction
// that build had to make to the physical wiring).
module fpga_top (
    input  wire [8:0] aer_data_i,
    input  wire       aer_req_n_i,
    output wire       aer_ack_n_o
);

    wire clk;
    wire resetn;   // active-low (pl_resetn0_out from the BD)

    // stream A (raw events) and stream B (core results) -> the two AXI-DMAs
    wire        m_axis_raw_tvalid, m_axis_raw_tready, m_axis_raw_tlast;
    wire [31:0] m_axis_raw_tdata;
    wire        m_axis_res_tvalid, m_axis_res_tready, m_axis_res_tlast;
    wire [31:0] m_axis_res_tdata;

    // firmware BRAM, port B (port A is the PS's AXI-BRAM-Ctrl, inside the BD)
    wire        bram_clk, bram_en;
    wire [3:0]  bram_we;
    wire [31:0] bram_addr, bram_wrdata, bram_rddata;

    // AXI-GPIO control / status
    wire [31:0] ctrl, decim;
    wire [31:0] req_count, word_count, evt_count, last_event;
    wire [31:0] raw_drop_count, core_drop_count, core_push_count;
    wire [31:0] fetch_count, result_count, rd_err_count, reset_count;

    actnow_aer_kr260_wrapper bd_i (
        .pl_clk0_out       (clk),
        .pl_resetn0_out    (resetn),

        // AXI4-Stream slaves of the two DMAs
        .s_axis_raw_tvalid (m_axis_raw_tvalid),
        .s_axis_raw_tready (m_axis_raw_tready),
        .s_axis_raw_tdata  (m_axis_raw_tdata),
        .s_axis_raw_tkeep  (4'hF),
        .s_axis_raw_tlast  (m_axis_raw_tlast),

        .s_axis_res_tvalid (m_axis_res_tvalid),
        .s_axis_res_tready (m_axis_res_tready),
        .s_axis_res_tdata  (m_axis_res_tdata),
        .s_axis_res_tkeep  (4'hF),
        .s_axis_res_tlast  (m_axis_res_tlast),

        // firmware BRAM, port B
        .BRAM_PORTB_clk    (bram_clk),
        .BRAM_PORTB_en     (bram_en),
        .BRAM_PORTB_we     (bram_we),
        .BRAM_PORTB_addr   (bram_addr),
        .BRAM_PORTB_din    (bram_wrdata),
        .BRAM_PORTB_dout   (bram_rddata),
        .BRAM_PORTB_rst    (1'b0),

        // AXI-GPIO
        .gpio_ctrl_out     (ctrl),
        .gpio_decim_out    (decim),
        .gpio_stat0_in     (req_count),
        .gpio_stat1_in     (evt_count),
        .gpio_stat2_in     (core_drop_count),
        .gpio_stat3_in     (result_count),
        .gpio_stat4_in     (fetch_count),
        .gpio_stat5_in     (last_event)
    );

    actnow_pl pl_i (
        .clk               (clk),
        .resetn            (resetn),

        .aer_data_i        (aer_data_i),
        .aer_req_n_i       (aer_req_n_i),
        .aer_ack_n_o       (aer_ack_n_o),

        .m_axis_raw_tvalid (m_axis_raw_tvalid),
        .m_axis_raw_tready (m_axis_raw_tready),
        .m_axis_raw_tdata  (m_axis_raw_tdata),
        .m_axis_raw_tlast  (m_axis_raw_tlast),

        .m_axis_res_tvalid (m_axis_res_tvalid),
        .m_axis_res_tready (m_axis_res_tready),
        .m_axis_res_tdata  (m_axis_res_tdata),
        .m_axis_res_tlast  (m_axis_res_tlast),

        .bram_clk          (bram_clk),
        .bram_en           (bram_en),
        .bram_we           (bram_we),
        .bram_addr         (bram_addr),
        .bram_wrdata       (bram_wrdata),
        .bram_rddata       (bram_rddata),

        .ctrl              (ctrl),
        .decim             (decim),

        .req_count         (req_count),
        .word_count        (word_count),
        .evt_count         (evt_count),
        .last_event        (last_event),
        .raw_drop_count    (raw_drop_count),
        .core_drop_count   (core_drop_count),
        .core_push_count   (core_push_count),
        .fetch_count       (fetch_count),
        .result_count      (result_count),
        .rd_err_count      (rd_err_count),
        .reset_count       (reset_count)
    );

endmodule
