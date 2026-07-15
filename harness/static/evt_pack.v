`timescale 1ns/1ps

// Pack a decoded AER event into the requirements ABI:
//
//   [31]    padding
//   [30:24] x[6:0]
//   [23:17] y[6:0]
//   [16:1]  timestep[15:0]
//   [0]     polarity
//
// TICK_DIV = clocks per timestamp tick (100 @ 100 MHz = 1 us).
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
    reg [15:0] ts;

    always @(posedge clk) begin
        if (rst) begin
            tick_cnt <= 32'd0;
            ts       <= 16'd0;
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
            if (evt_valid) out_data <= {1'b0, evt_data[6:0], evt_data[13:7], ts, evt_data[14]};
        end
    end
endmodule
