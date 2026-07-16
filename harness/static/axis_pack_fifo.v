`timescale 1ns/1ps

// A FIFO with an AXI4-Stream master output that packetizes what it carries.
// Shared by both streams: evt_stream wraps it (dropping when it's full, because
// nothing may ever stall the AER bus), and the core's result path drives it
// directly (backpressuring, because a result must never be lost).
//
// Why tlast at all: an AXI-DMA in simple mode ends a transfer when the
// destination buffer fills *or* when it sees tlast. With no tlast, a quiet scene
// would leave the PS blocked on a half-filled buffer -- the events would have
// arrived and simply never been handed over. So a packet closes either at
// PKT_WORDS beats (bounds the packet to the PS's buffer size) or the moment the
// FIFO drains (a burst has ended), whichever comes first. Bursty event traffic
// becomes promptly delivered, variable-length packets.
module axis_pack_fifo #(
    parameter integer DEPTH_LOG2 = 10,   // 1024 words
    parameter integer PKT_WORDS  = 256,  // max words per packet
    parameter integer HOLD_CYCLES = 0    // batch until full or this collection window
)(
    input  wire        clk,
    input  wire        rst,             // active-high, synchronous

    input  wire        s_valid,
    output wire        s_ready,         // = not full
    input  wire [31:0] s_data,

    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire [31:0] m_axis_tdata,
    output wire        m_axis_tlast
);
    localparam integer DEPTH = (1 << DEPTH_LOG2);

    reg [31:0] mem [0:DEPTH-1];
    reg [DEPTH_LOG2:0] wptr, rptr;   // one extra bit disambiguates full from empty

    wire full  = (wptr[DEPTH_LOG2] != rptr[DEPTH_LOG2]) &&
                 (wptr[DEPTH_LOG2-1:0] == rptr[DEPTH_LOG2-1:0]);
    wire empty = (wptr == rptr);

    wire push = s_valid && !full;
    wire pop  = m_axis_tvalid && m_axis_tready;

    assign s_ready = !full;

    always @(posedge clk) begin
        if (rst) begin
            wptr <= 0;
        end else if (push) begin
            mem[wptr[DEPTH_LOG2-1:0]] <= s_data;
            wptr                      <= wptr + 1'b1;
        end
    end

    always @(posedge clk) begin
        if (rst)      rptr <= 0;
        else if (pop) rptr <= rptr + 1'b1;
    end

    // The raw stream uses a short collection window to avoid one DMA completion per
    // naturally spaced AER event. Existing users keep HOLD_CYCLES=0 and retain
    // immediate delivery. Once released, a packet drains normally to tlast.
    reg packet_active;
    reg [31:0] hold_count;
    wire [DEPTH_LOG2:0] fill_count = wptr - rptr;
    wire hold_enabled = (HOLD_CYCLES != 0);

    always @(posedge clk) begin
        if (rst) begin
            packet_active <= 1'b0;
            hold_count    <= 32'd0;
        end else if (!hold_enabled) begin
            packet_active <= 1'b1;
            hold_count    <= 32'd0;
        end else if (packet_active) begin
            if (pop && m_axis_tlast) packet_active <= 1'b0;
        end else if (fill_count >= PKT_WORDS) begin
            packet_active <= 1'b1;
            hold_count    <= 32'd0;
        end else if (empty) begin
            hold_count <= 32'd0;
        end else if (hold_count >= HOLD_CYCLES - 1) begin
            packet_active <= 1'b1;
            hold_count    <= 32'd0;
        end else begin
            hold_count <= hold_count + 1'b1;
        end
    end

    assign m_axis_tvalid = !empty && (!hold_enabled || packet_active);
    assign m_axis_tdata  = mem[rptr[DEPTH_LOG2-1:0]];

    // ---- packet boundaries ----
    // "this beat empties the FIFO" -> the burst ends here, so close the packet.
    wire drains_now = ((rptr + 1'b1) == wptr) && !push;

    reg [15:0] pkt_cnt;
    assign m_axis_tlast = m_axis_tvalid &&
                          (drains_now || (pkt_cnt == PKT_WORDS[15:0] - 1'b1));

    always @(posedge clk) begin
        if (rst)                      pkt_cnt <= 16'd0;
        else if (pop && m_axis_tlast) pkt_cnt <= 16'd0;
        else if (pop)                 pkt_cnt <= pkt_cnt + 1'b1;
    end
endmodule
