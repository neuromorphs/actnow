`timescale 1ns/1ps

// The core's raw external read/write route (base 6) -> an AXI4-Stream master,
// which an AXI-DMA lands in DDR for the PS to read out and forward over UDP.
//
// This is the RTL analogue of the testbench fifo_out that chips/fpga's e2e tests
// hang off io_* -- what the program writes to base 6 *is* the processed stream.
//
// A write is three transfers: addr, then mode, then wdata (the demux's route-2
// quad, minus rdata). The word is handed straight to the stream, so the stream's
// tready is the core's wdata_ready: if the PS stops draining, the core blocks on
// its store -- real backpressure, exactly as core/peripherals/fifo_out.act
// specifies ("a CPU write to a full FIFO blocks rather than asserting"). That
// backpressure propagates up the chain to evt_stream, which drops -- never to the
// AER receiver, which must never stall.
//
// A *read* of base 6 is a firmware bug (the route is output-only, as fifo_out's
// own assert says). It is answered with zero rather than left hanging -- an
// unanswered read would deadlock the core and look like a hardware fault -- and
// counted in rd_err_count, which is the thing to check if the processed stream
// ever goes quiet.
module io_axis_adapter (
    input  wire        clk,
    input  wire        rst,             // active-high, synchronous

    // core io_* channel group
    output reg         io_addr_ready,
    input  wire        io_addr_valid,
    input  wire [3:0]  io_addr_base,
    input  wire [15:0] io_addr_offset,
    output reg         io_mode_ready,
    input  wire        io_mode_valid,
    input  wire [1:0]  io_mode_op,
    output wire        io_wdata_ready,
    input  wire        io_wdata_valid,
    input  wire [31:0] io_wdata,
    output reg         io_rdata_valid,
    input  wire        io_rdata_ready,
    output wire [31:0] io_rdata,

    // AXI4-Stream master (processed results)
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire [31:0] m_axis_tdata,

    // status
    output reg  [31:0] result_count,
    output reg  [31:0] rd_err_count
);
    // op_mem_t as chp2fpga encodes it: W=0, R=1, R_AND_W=2 (see gen/memf0.v).
    localparam [1:0] OP_W = 2'd0;

    localparam [1:0] S_ADDR = 2'd0, S_MODE = 2'd1, S_WDATA = 2'd2, S_RDATA = 2'd3;
    reg [1:0] st;

    // The store's data phase *is* the stream beat -- no buffer in between.
    assign m_axis_tvalid  = (st == S_WDATA) && io_wdata_valid;
    assign m_axis_tdata   = io_wdata;
    assign io_wdata_ready = (st == S_WDATA) && m_axis_tready;

    assign io_rdata = 32'd0;

    always @(posedge clk) begin
        if (rst) begin
            st           <= S_ADDR;
            result_count <= 32'd0;
            rd_err_count <= 32'd0;
        end else begin
            case (st)
                S_ADDR: if (io_addr_valid) st <= S_MODE;
                S_MODE: if (io_mode_valid) begin
                            if (io_mode_op == OP_W) begin
                                st <= S_WDATA;
                            end else begin
                                rd_err_count <= rd_err_count + 1'b1;
                                st           <= S_RDATA;
                            end
                        end
                S_WDATA: if (io_wdata_valid && m_axis_tready) begin
                            result_count <= result_count + 1'b1;
                            st           <= S_ADDR;
                        end
                S_RDATA: if (io_rdata_ready) st <= S_ADDR;
            endcase
        end
    end

    always @(*) begin
        io_addr_ready  = (st == S_ADDR);
        io_mode_ready  = (st == S_MODE);
        io_rdata_valid = (st == S_RDATA);
    end
endmodule
