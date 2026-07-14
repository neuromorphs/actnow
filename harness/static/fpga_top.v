`timescale 1ns/1ps

// Synthesizable top for the KR260 skeleton bring-up.
//
// Instantiates the minimal MPSoC block design (actnow_skeleton_kr260, see
// fpga/tcl/create_bd_skeleton.tcl) to source a real clock + synchronous reset,
// and the converted fpga chip `\core4` (chp2fpga output of chips/fpga/core<4>,
// harness/gen/core4.v). For now every one of the core's data/peripheral
// channels is tied INACTIVE -- the core is clocked and released from reset but
// quiescent, so this builds as a structural skeleton. The AXI peripherals that
// will drive core's raw rom_*/fifo_push/io_* groups (via the block design) come
// later.
//
// chp2fpga channel convention: a transfer completes on a clock edge when
// *_valid & *_ready are both high. So to hold a channel idle:
//   - core INPUT channels  -> drive *_valid = 0 (never offer data);
//   - core OUTPUT channels  -> drive *_ready = 0 (never accept -> core blocks
//     on its first fetch and simply parks).
module fpga_top;

    wire clk;
    wire rst;   // active-high, synchronised (pl_reset0_out from the BD)

    // ------------------------------------------------------------
    // Minimal MPSoC block design: clock + reset only.
    // ------------------------------------------------------------
    actnow_skeleton_kr260_wrapper bd_i (
        .pl_clk0_out   (clk),
        .pl_reset0_out (rst)
    );

    // ------------------------------------------------------------
    // Converted fpga core, all interfaces tied inactive.
    // Outputs (\*_valid, data buses, input-channel \*_ready) are left open.
    // ------------------------------------------------------------
    \core4 core_i (
         .\clock (clk)
        ,.\reset (rst)

        // reset_ext: core input channel -> hold valid low
        ,.\reset_ext_valid (1'b0)
        ,.\reset_ext       (1'b0)

        // rom_* : core drives addr/mode out, expects rdata in.
        //   tie output-channel readys low (nothing accepts the fetch),
        //   hold the rdata input channel's valid low.
        ,.\rom_addr_ready  (1'b0)
        ,.\rom_mode_ready  (1'b0)
        ,.\rom_rdata_valid (1'b0)
        ,.\rom_rdata       (32'b0)

        // fifo_push: core input channel -> hold valid low
        ,.\fifo_push_valid (1'b0)
        ,.\fifo_push       (32'b0)

        // io_* : core drives addr/mode/wdata out, expects rdata in.
        ,.\io_addr_ready   (1'b0)
        ,.\io_mode_ready   (1'b0)
        ,.\io_wdata_ready  (1'b0)
        ,.\io_rdata_valid  (1'b0)
        ,.\io_rdata        (32'b0)
    );

endmodule
