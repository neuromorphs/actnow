`timescale 1ns/1ps

// Pack a decoded AER event into the 32-bit word every downstream consumer sees:
//
//   bits [14:0]  {pol, y[6:0], x[6:0]}  -- bit-identical to aer_rx's last_event,
//                                          so the host-side parser is unchanged
//   bits [31:15] ts[16:0]               -- PL timestamp, TICK_NS resolution
//
// The timestamp is free here (one counter) and is what makes temporal work
// possible in the core's ISR at all -- refractory filtering, correlation-based
// noise rejection -- since a CHP program has no clock of its own. Nothing
// downstream is required to look at it: the low 15 bits stand alone.
//
// TICK_DIV = clocks per timestamp tick (100 @ 100 MHz = 1 us). At 1 us the
// 17-bit counter wraps every ~131 ms, which is far longer than any plausible
// inter-event interval the firmware would reason about -- but the firmware must
// treat ts as modular (compare with a wrapping subtract), not as absolute time.
module evt_pack #(
    parameter integer TICK_DIV = 100
)(
    input  wire        clk,
    input  wire        rst,          // active-high, synchronous
    input  wire        evt_valid,
    input  wire [14:0] evt_data,
    output reg         out_valid,
    output reg  [31:0] out_data
);
    reg [31:0] tick_cnt;
    reg [16:0] ts;

    always @(posedge clk) begin
        if (rst) begin
            tick_cnt <= 32'd0;
            ts       <= 17'd0;
        end else if (tick_cnt == TICK_DIV[31:0] - 1) begin
            tick_cnt <= 32'd0;
            ts       <= ts + 1'b1;
        end else begin
            tick_cnt <= tick_cnt + 1'b1;
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            out_valid <= 1'b0;
            out_data  <= 32'd0;
        end else begin
            out_valid <= evt_valid;
            if (evt_valid) out_data <= {ts, evt_data};
        end
    end
endmodule
