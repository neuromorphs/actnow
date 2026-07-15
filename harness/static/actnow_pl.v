`timescale 1ns/1ps

// Everything in the PL except the block design: AER receive, one elastic FIFO
// into the generated core, and one packetized result stream out.
//
//   AER bus -> aer_rx -> evt_pack -> evt_stream -> core.fifo_push
//                                      core.io_* -> m_axis_res -> DMA -> UDP
//
// The event stream drops when full rather than backpressuring the AER receiver,
// so the camera is never stalled by core or PS latency.
module actnow_pl (
    input  wire        clk,
    input  wire        resetn,          // active-low (PS pl_resetn0)

    // asynchronous AER bus from the ECP3 (9 data + /REQ in, /ACK out)
    input  wire [8:0]  aer_data_i,
    input  wire        aer_req_n_i,
    output wire        aer_ack_n_o,

    // core results -> AXI-DMA
    output wire        m_axis_res_tvalid,
    input  wire        m_axis_res_tready,
    output wire [31:0] m_axis_res_tdata,
    output wire        m_axis_res_tlast,

    // firmware BRAM, port B (port A = the PS's AXI-BRAM-Ctrl)
    output wire        bram_clk,
    output wire        bram_en,
    output wire [3:0]  bram_we,
    output wire [31:0] bram_addr,
    output wire [31:0] bram_wrdata,
    input  wire [31:0] bram_rddata,

    // control (AXI-GPIO outputs from the PS)
    input  wire [31:0] ctrl,            // bit 0: warm reset; bit 1: pause/discard ingress

    // status (AXI-GPIO inputs to the PS)
    output wire [31:0] req_count,       // AER /REQ falling edges
    output wire [31:0] word_count,      // completed 4-phase handshakes
    output wire [31:0] evt_count,       // decoded events
    output wire [31:0] last_event,      // {pol,y,x} of the newest event
    output wire [31:0] core_drop_count, // events dropped: core FIFO full (core too slow)
    output wire [31:0] core_push_count, // events actually handed to the core
    output wire [31:0] fetch_count,     // core ROM fetches (proves it is booting)
    output wire [31:0] result_count,    // words the core wrote to base 6
    output wire [31:0] rd_err_count,    // illegal base-6 reads (firmware bug)
    output wire [31:0] reset_count      // warm resets delivered to the core
);
    wire rst = ~resetn;                 // the core and adapters use active-high

    // ---- AER receiver (kr260_aer_interface's proven RX, + an event tap) ----
    wire        evt_valid;
    wire [14:0] evt_data;

    aer_rx_wrap aer_i (
        .clk        (clk),
        .resetn     (resetn),
        .aer_data_i (aer_data_i),
        .aer_req_n_i(aer_req_n_i),
        .aer_ack_n_o(aer_ack_n_o),
        .req_count  (req_count),
        .word_count (word_count),
        .evt_count  (evt_count),
        .last_event (last_event),
        .evt_valid  (evt_valid),
        .evt_data   (evt_data)
    );

    // ---- timestamp + pack into the 32-bit word both streams carry ----
    wire        pkt_valid;
    wire [31:0] pkt_data;

    evt_pack #(.TICK_DIV(100)) pack_i (   // 100 MHz -> 1 us ticks
        .clk       (clk),
        .rst       (rst),
        .evt_valid (evt_valid),
        .evt_data  (evt_data),
        .out_valid (pkt_valid),
        .out_data  (pkt_data)
    );

    // ---- events into the core ----
    wire        core_in_tvalid;
    wire        core_feed_tready;
    wire        core_in_tready = ctrl[1] ? 1'b0 : core_feed_tready;
    wire [31:0] core_in_tdata;

    evt_stream #(.DEPTH_LOG2(10)) core_i (
        .clk            (clk),
        .rst            (rst),
        .decim          (16'd0),
        .in_valid       (pkt_valid),
        .in_data        (pkt_data),
        .m_axis_tvalid  (core_in_tvalid),
        // While paused, drain and discard queued camera words. This lets the
        // current firmware finish without admitting another interrupt batch.
        .m_axis_tready  (ctrl[1] ? 1'b1 : core_feed_tready),
        .m_axis_tdata   (core_in_tdata),
        .m_axis_tlast   (),                 // the core's fifo_push has no packet concept

        .accepted_count (core_push_count),
        .drop_count     (core_drop_count)
    );

    actnow_core_wrap core_wrap_i (
        .clk           (clk),
        .rst           (rst),
        .s_axis_tvalid (core_in_tvalid && !ctrl[1]),
        .s_axis_tready (core_feed_tready),
        .s_axis_tdata  (core_in_tdata),
        .m_axis_tvalid (m_axis_res_tvalid),
        .m_axis_tready (m_axis_res_tready),
        .m_axis_tdata  (m_axis_res_tdata),
        .m_axis_tlast  (m_axis_res_tlast),
        .bram_clk      (bram_clk),
        .bram_en       (bram_en),
        .bram_we       (bram_we),
        .bram_addr     (bram_addr),
        .bram_wrdata   (bram_wrdata),
        .bram_rddata   (bram_rddata),
        .reset_pulse   (ctrl[0]),
        .fetch_count   (fetch_count),
        .result_count  (result_count),
        .rd_err_count  (rd_err_count),
        .reset_count   (reset_count)
    );
endmodule
