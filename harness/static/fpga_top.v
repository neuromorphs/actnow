`timescale 1ns/1ps

// Synthesizable top for the KR260 build: the block design (PS, two AXI-DMAs,
// AXI-BRAM-Ctrl + firmware BRAM, AXI-GPIO) wrapped around actnow_pl.
module fpga_top (
    input  wire [8:0] aer_data_i,
    input  wire       aer_req_n_i,
    output wire       aer_ack_n_o
);

    wire clk;
    wire resetn;

    wire        m_axis_res_tvalid, m_axis_res_tready, m_axis_res_tlast;
    wire [31:0] m_axis_res_tdata;

    wire        m_axis_raw_tvalid, m_axis_raw_tready, m_axis_raw_tlast;
    wire [31:0] m_axis_raw_tdata;

    wire        bram_clk, bram_en;
    wire [3:0]  bram_we;
    wire [31:0] bram_addr, bram_wrdata, bram_rddata;

    wire [31:0] ctrl;
    wire [31:0] req_count, word_count, evt_count, last_event;
    wire [31:0] core_drop_count, core_push_count;
    wire [31:0] fetch_count, result_count, rd_err_count, reset_count;
    wire [31:0] raw_drop_count, raw_push_count;

    actnow_kr260_wrapper bd_i (
        .pl_clk0_out       (clk),
        .pl_resetn0_out    (resetn),

        .s_axis_res_tvalid (m_axis_res_tvalid),
        .s_axis_res_tready (m_axis_res_tready),
        .s_axis_res_tdata  (m_axis_res_tdata),
        .s_axis_res_tkeep  (4'hF),
        .s_axis_res_tlast  (m_axis_res_tlast),

        .s_axis_raw_tvalid (m_axis_raw_tvalid),
        .s_axis_raw_tready (m_axis_raw_tready),
        .s_axis_raw_tdata  (m_axis_raw_tdata),
        .s_axis_raw_tkeep  (4'hF),
        .s_axis_raw_tlast  (m_axis_raw_tlast),

        .BRAM_PORTB_clk    (bram_clk),
        .BRAM_PORTB_en     (bram_en),
        .BRAM_PORTB_we     (bram_we),
        .BRAM_PORTB_addr   (bram_addr),
        .BRAM_PORTB_din    (bram_wrdata),
        .BRAM_PORTB_dout   (bram_rddata),
        .BRAM_PORTB_rst    (1'b0),

        .gpio_ctrl_out     (ctrl),
        .gpio_stat0_in     (req_count),
        .gpio_stat1_in     (word_count),
        .gpio_stat2_in     (evt_count),
        .gpio_stat3_in     (last_event),
        .gpio_stat4_in     (core_drop_count),
        .gpio_stat5_in     (core_push_count),
        .gpio_stat6_in     (fetch_count),
        .gpio_stat7_in     (result_count),
        .gpio_stat8_in     (rd_err_count),
        .gpio_stat9_in     (reset_count),
        .gpio_stat10_in    (raw_drop_count),
        .gpio_stat11_in    (raw_push_count)
    );

    actnow_pl pl_i (
        .clk               (clk),
        .resetn            (resetn),
        .aer_data_i        (aer_data_i),
        .aer_req_n_i       (aer_req_n_i),
        .aer_ack_n_o       (aer_ack_n_o),
        .m_axis_res_tvalid (m_axis_res_tvalid),
        .m_axis_res_tready (m_axis_res_tready),
        .m_axis_res_tdata  (m_axis_res_tdata),
        .m_axis_res_tlast  (m_axis_res_tlast),
        .m_axis_raw_tvalid (m_axis_raw_tvalid),
        .m_axis_raw_tready (m_axis_raw_tready),
        .m_axis_raw_tdata  (m_axis_raw_tdata),
        .m_axis_raw_tlast  (m_axis_raw_tlast),
        .bram_clk          (bram_clk),
        .bram_en           (bram_en),
        .bram_we           (bram_we),
        .bram_addr         (bram_addr),
        .bram_wrdata       (bram_wrdata),
        .bram_rddata       (bram_rddata),
        .ctrl              (ctrl),
        .req_count         (req_count),
        .word_count        (word_count),
        .evt_count         (evt_count),
        .last_event        (last_event),
        .core_drop_count   (core_drop_count),
        .core_push_count   (core_push_count),
        .fetch_count       (fetch_count),
        .result_count      (result_count),
        .rd_err_count      (rd_err_count),
        .reset_count       (reset_count),
        .raw_drop_count    (raw_drop_count),
        .raw_push_count    (raw_push_count)
    );

endmodule
