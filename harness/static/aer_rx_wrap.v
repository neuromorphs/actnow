`timescale 1ns/1ps

// Verilog wrapper so the AER receiver can be used as a BD module reference
// (Vivado disallows a SystemVerilog file as the top file of a module reference).
// Instantiates the SystemVerilog aer_rx_simple; ports match the BD connections.
//
// Unchanged from kr260_aer_interface's wrapper except for the live event tap
// (evt_valid/evt_data): that project only ever read the counters over AXI-GPIO,
// whereas both streams here are built on the decoded event itself.
module aer_rx_wrap (
    input  wire        clk,
    input  wire        resetn,
    input  wire [8:0]  aer_data_i,
    input  wire        aer_req_n_i,
    output wire        aer_ack_n_o,
    output wire [31:0] req_count,
    output wire [31:0] word_count,
    output wire [31:0] evt_count,
    output wire [31:0] last_event,
    output wire        evt_valid,
    output wire [14:0] evt_data      // {pol, y[6:0], x[6:0]}
);
    aer_rx_simple #(.AER_W(9), .SAMP_DELAY(24)) u (
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
endmodule
