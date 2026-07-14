// -----------------------------------------------------------------------------
// aer_rx_simple : SciDVS word-serial AER receiver for KR260 bring-up.
// 4-phase active-low REQ/ACK handshake receiver (KR260 = receiver: data+REQ in,
// ACK out), word-serial deserialize (Y row word / X col word, MSB=select),
// exposing DEBUG COUNTERS for AXI-GPIO readout so we can see the link working:
//   req_count  : REQ falling edges seen  (proves physical link + SciDVS sending)
//   word_count : complete handshakes     (proves ACK closes the 4-phase loop)
//   evt_count  : X (column) words        (= decoded pixel events)
//   last_event : {5'b0, pol, y[6:0], x[6:0]}  last decoded event
// -----------------------------------------------------------------------------
`timescale 1ns/1ps
module aer_rx_simple #(
    parameter int AER_W = 9,
    parameter int SAMP_DELAY = 24    // clocks after REQ-low before sampling data
)(                                    // (>50ns @100MHz: data settles, esp. Y addr)
    input  logic        clk,
    input  logic        resetn,        // active-low
    // Async AER bus from ECP3 (PMOD/RPi, 3.3V)
    input  logic [AER_W-1:0] aer_data_i,
    input  logic        aer_req_n_i,
    output logic        aer_ack_n_o,
    // Debug counters (to AXI GPIO inputs)
    output logic [31:0] req_count,
    output logic [31:0] word_count,
    output logic [31:0] evt_count,
    output logic [31:0] last_event,
    // Live event tap (added for the actnow harness -- kr260_aer_interface only
    // needed the counters). One-cycle pulse per decoded event, alongside the
    // same {pol, y[6:0], x[6:0]} payload last_event carries.
    output logic        evt_valid,
    output logic [14:0] evt_data
);
    logic rst_n; assign rst_n = resetn;

    // 2-FF sync REQ
    logic req_meta, req_s, req_s_d;
    always_ff @(posedge clk) begin
        req_meta <= aer_req_n_i;
        req_s    <= req_meta;
        req_s_d  <= req_s;
    end
    wire req_fall = (req_s_d == 1'b1) && (req_s == 1'b0);

    // req_count: every REQ falling edge (independent of the FSM)
    always_ff @(posedge clk)
        if (!rst_n) req_count <= 32'd0;
        else if (req_fall) req_count <= req_count + 1'b1;

    // 4-phase receiver FSM
    typedef enum logic [2:0] {S_IDLE,S_SAMP,S_ACKLO,S_WAITHI,S_EMIT} st_t;
    st_t st;
    logic [AER_W-1:0] word_q;
    logic word_strobe;
    logic [7:0] dly;
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            st<=S_IDLE; aer_ack_n_o<=1'b1; word_q<='0; word_strobe<=1'b0; dly<='0;
            word_count<=32'd0;
        end else begin
            word_strobe<=1'b0;
            case (st)
                S_IDLE:  begin aer_ack_n_o<=1'b1; dly<='0; if (req_s==1'b0) st<=S_SAMP; end
                S_SAMP:  begin if (dly==SAMP_DELAY[7:0]) begin word_q<=aer_data_i; st<=S_ACKLO; end else dly<=dly+1'b1; end
                S_ACKLO: begin aer_ack_n_o<=1'b0; if (req_s==1'b1) st<=S_WAITHI; end
                S_WAITHI:begin aer_ack_n_o<=1'b1; st<=S_EMIT; end
                S_EMIT:  begin word_strobe<=1'b1; word_count<=word_count+1'b1; st<=S_IDLE; end
                default: st<=S_IDLE;
            endcase
        end
    end

    // Deserialize: Y word sets row, X word -> event
    logic [6:0] cur_y; logic cur_y_valid;
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            cur_y<='0; cur_y_valid<=1'b0; evt_count<=32'd0; last_event<=32'd0;
            evt_valid<=1'b0; evt_data<='0;
        end else begin
            evt_valid <= 1'b0;
            if (word_strobe) begin
                if (word_q[AER_W-1]==1'b0) begin           // Y word
                    cur_y<=word_q[6:0]; cur_y_valid<=1'b1;
                end else if (cur_y_valid) begin            // X word -> event
                    last_event <= {17'd0, word_q[0], cur_y, word_q[7:1]}; // {pol,y,x}
                    evt_count  <= evt_count + 1'b1;
                    evt_valid  <= 1'b1;
                    evt_data   <= {word_q[0], cur_y, word_q[7:1]};
                end
            end
        end
    end
endmodule
