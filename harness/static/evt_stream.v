`timescale 1ns/1ps

// One consumer's view of the event stream: optional decimation, an elastic FIFO
// that drops rather than stalls, and a packetized AXI4-Stream master out.
// In this harness it is used as the one AER-to-core FIFO.
//
// Why the dropping matters (the one hard constraint of the whole design): the
// AER bus has a single ACK line, so if any consumer were allowed to backpressure
// the receiver, it would stall the camera. So no consumer may ever stall the RX: when the
// FIFO is full, the event is DROPPED and drop_count increments. A drop is
// visible and bounded; a stalled camera is neither.
//
//   decim = 0 or 1 -> forward every event
//   decim = N > 1  -> forward every Nth event (uniform subsampling)
//
// Decimation is kept as a local option, but actnow_pl ties it to zero for the
// requirements-only build.
module evt_stream #(
    parameter integer DEPTH_LOG2 = 10,   // 1024 events
    parameter integer PKT_WORDS  = 256,  // max events per AXI-Stream packet
    parameter integer HOLD_CYCLES = 0    // optional DMA collection window
)(
    input  wire        clk,
    input  wire        rst,            // active-high, synchronous
    input  wire [15:0] decim,

    // event in (never backpressured -- see above)
    input  wire        in_valid,
    input  wire [31:0] in_data,

    // AXI4-Stream master out
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire [31:0] m_axis_tdata,
    output wire        m_axis_tlast,

    // status (to AXI-GPIO)
    output reg  [31:0] accepted_count,  // events that entered the FIFO
    output reg  [31:0] drop_count       // events dropped because it was full
);
    // ---- decimation ----
    reg [15:0] phase;
    wire       keep = (decim <= 16'd1) ? 1'b1 : (phase == 16'd0);

    always @(posedge clk) begin
        if (rst) begin
            phase <= 16'd0;
        end else if (in_valid) begin
            if (decim <= 16'd1)             phase <= 16'd0;
            else if (phase == decim - 1'b1) phase <= 16'd0;
            else                            phase <= phase + 1'b1;
        end
    end

    // ---- elastic FIFO: take it if there's room, drop it if there isn't ----
    wire fifo_ready;
    wire offer = in_valid && keep;
    wire push  = offer &&  fifo_ready;
    wire drop  = offer && !fifo_ready;

    axis_pack_fifo #(
        .DEPTH_LOG2 (DEPTH_LOG2),
        .PKT_WORDS  (PKT_WORDS),
        .HOLD_CYCLES(HOLD_CYCLES)
    ) fifo_i (
        .clk           (clk),
        .rst           (rst),
        .s_valid       (push),
        .s_ready       (fifo_ready),
        .s_data        (in_data),
        .m_axis_tvalid (m_axis_tvalid),
        .m_axis_tready (m_axis_tready),
        .m_axis_tdata  (m_axis_tdata),
        .m_axis_tlast  (m_axis_tlast)
    );

    always @(posedge clk) begin
        if (rst) begin
            accepted_count <= 32'd0;
            drop_count     <= 32'd0;
        end else begin
            if (push) accepted_count <= accepted_count + 1'b1;
            if (drop) drop_count     <= drop_count + 1'b1;
        end
    end
endmodule
