`timescale 1ns/1ps

// Turns a PS-written control bit into one send on the core's reset_ext channel.
//
// reset_ext is a CHP channel, not a wire: the core accepts `true` on it at a
// choice point between instructions and reboots (pc := ADDR_RESET, interrupt
// controller cleared, internal SRAM untouched). So the PS "pressing reset" means
// completing exactly one rendezvous -- hence the edge detect: a level-held GPIO
// bit must not reset the core over and over.
//
// The request is latched (pending) rather than presented for one cycle, because
// the core may take an arbitrary number of cycles to reach the choice point.
module reset_ext_send (
    input  wire clk,
    input  wire rst,               // active-high, synchronous
    input  wire pulse_in,          // level from AXI-GPIO; a 0->1 edge arms one reset

    output reg  reset_ext_valid,
    output wire reset_ext_data,
    input  wire reset_ext_ready,

    output reg  [31:0] reset_count
);
    assign reset_ext_data = 1'b1;   // the channel carries a bool; only `true` is ever sent

    reg pulse_q;

    always @(posedge clk) begin
        if (rst) begin
            pulse_q         <= 1'b0;
            reset_ext_valid <= 1'b0;
            reset_count     <= 32'd0;
        end else begin
            pulse_q <= pulse_in;

            if (reset_ext_valid && reset_ext_ready) begin
                // handshake complete -- one reset delivered
                reset_ext_valid <= 1'b0;
                reset_count     <= reset_count + 1'b1;
            end else if (pulse_in && !pulse_q) begin
                reset_ext_valid <= 1'b1;
            end
        end
    end
endmodule
