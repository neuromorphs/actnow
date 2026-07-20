`timescale 1ns/1ps

// Stress variant of tb_pl for the requirements-only harness. It boots the real
// firmware, then sends a continuous AER burst without waiting at batch
// boundaries. The core must keep re-arming its fifo_in interrupt and return one
// result for every event accepted by the PL-to-core stream.

module tb_pl_stress;

    parameter integer TIMEOUT_NS = 8_000_000;
    parameter integer NEV        = 60;

    reg clk = 1'b0;
    reg resetn = 1'b0;
    always #5 clk = ~clk;

    reg  [8:0] aer_data  = 9'b0;
    reg        aer_req_n = 1'b1;
    wire       aer_ack_n;

    wire        res_tvalid, res_tlast;
    wire [31:0] res_tdata;
    reg         res_tready = 1'b1;
    wire        raw_tvalid, raw_tlast;
    wire [31:0] raw_tdata;
    reg         raw_tready = 1'b1;

    wire        bram_clk, bram_en;
    wire [3:0]  bram_we;
    wire [31:0] bram_addr, bram_wrdata;
    reg  [31:0] bram_rddata;
    localparam integer BRAM_WORDS = 8192;
    reg [31:0] bram [0:BRAM_WORDS-1];

    always @(posedge clk)
        if (bram_en) bram_rddata <= bram[bram_addr[14:2]];

    reg  [31:0] ctrl = 32'd0;
    wire [31:0] req_count, word_count, evt_count, last_event;
    wire [31:0] core_drop_count, core_push_count;
    wire [31:0] fetch_count, result_count, rd_err_count, reset_count;
    wire [31:0] raw_drop_count, raw_push_count;

    actnow_pl dut (
        .clk               (clk),
        .resetn            (resetn),
        .aer_data_i        (aer_data),
        .aer_req_n_i       (aer_req_n),
        .aer_ack_n_o       (aer_ack_n),
        .m_axis_res_tvalid (res_tvalid),
        .m_axis_res_tready (res_tready),
        .m_axis_res_tdata  (res_tdata),
        .m_axis_res_tlast  (res_tlast),
        .m_axis_raw_tvalid (raw_tvalid),
        .m_axis_raw_tready (raw_tready),
        .m_axis_raw_tdata  (raw_tdata),
        .m_axis_raw_tlast  (raw_tlast),
        .bram_clk          (bram_clk),
        .bram_en           (bram_en),
        .bram_we           (bram_we),
        .bram_addr         (bram_addr),
        .bram_wrdata       (bram_wrdata),
        .bram_rddata       (bram_rddata),
        .ctrl              (ctrl),
        .req_count         (req_count),
        .word_count        (word_count),
        .evt_count         (evt_count),
        .last_event        (last_event),
        .core_drop_count   (core_drop_count),
        .core_push_count   (core_push_count),
        .fetch_count       (fetch_count),
        .result_count      (result_count),
        .rd_err_count      (rd_err_count),
        .reset_count       (reset_count),
        .raw_drop_count    (raw_drop_count),
        .raw_push_count    (raw_push_count)
    );

    localparam integer MAXN = 256;
    reg [31:0] core_seen [0:MAXN-1];
    integer ncore = 0;
    integer nres = 0;
    integer nres_pkt = 0;
    integer nraw = 0;
    integer nraw_pkt = 0;
    integer errors = 0;

    function [31:0] transform_req_word(input [31:0] word);
        integer y;
        begin
            y = word[23:17];
            transform_req_word = (word & 32'hFF_01_FF_FF) |
                                 ((7'd111 - y[6:0]) << 17);
        end
    endfunction

    always @(posedge clk) if (resetn && dut.core_in_tvalid && dut.core_in_tready) begin
        core_seen[ncore] <= dut.core_in_tdata;
        ncore <= ncore + 1;
    end

    always @(posedge clk) if (resetn && res_tvalid && res_tready) begin
        if (nres >= ncore) begin
            $display("FAIL: result #%0d arrived before matching core input was recorded", nres);
            errors <= errors + 1;
        end else if (res_tdata !== transform_req_word(core_seen[nres])) begin
            $display("FAIL: result #%0d want 0x%08h got 0x%08h",
                     nres, transform_req_word(core_seen[nres]), res_tdata);
            errors <= errors + 1;
        end
        if (res_tlast) nres_pkt <= nres_pkt + 1;
        nres <= nres + 1;
    end

    always @(posedge clk) if (resetn && raw_tvalid && raw_tready) begin
        if (raw_tdata[31] !== 1'b0 || raw_tdata[30:24] !== (nraw % 126) ||
            raw_tdata[23:17] !== ((nraw / 2) % 112) || raw_tdata[0] !== nraw[0]) begin
            $display("FAIL: raw word #%0d has bad ABI: 0x%08h", nraw, raw_tdata);
            errors <= errors + 1;
        end
        if (raw_tlast) nraw_pkt <= nraw_pkt + 1;
        nraw <= nraw + 1;
    end

    task aer_word(input [8:0] w);
        begin
            aer_data = w;
            #30;
            aer_req_n = 1'b0;
            wait (aer_ack_n == 1'b0);
            #30;
            aer_req_n = 1'b1;
            wait (aer_ack_n == 1'b1);
            #30;
        end
    endtask

    task aer_event(input [6:0] x, input [6:0] y, input pol);
        begin
            aer_word({1'b0, 1'b0, y});
            aer_word({1'b1, x, pol});
        end
    endtask

    task check_abi(input integer idx, input [6:0] x, input [6:0] y, input pol);
        reg [31:0] w;
        begin
            w = core_seen[idx];
            if (w[31] !== 1'b0 || w[30:24] !== x || w[23:17] !== y || w[0] !== pol) begin
                $display("FAIL: core word #%0d bad ABI: got 0x%08h want x=%0d y=%0d p=%0d",
                         idx, w, x, y, pol);
                errors = errors + 1;
            end
        end
    endtask

    integer i;
    initial begin
        $display("=== tb_pl_stress: continuous %0d-event AER burst ===", NEV);

        for (i = 0; i < BRAM_WORDS; i = i + 1) bram[i] = 32'b0;
        $readmemb("rom.mem", bram);

        resetn = 1'b0;
        repeat (16) @(posedge clk);
        resetn = 1'b1;

        ctrl = 32'h1;
        repeat (4) @(posedge clk);
        ctrl = 32'h0;

        repeat (25000) @(posedge clk);
        $display("[%0t] boot window done: fetch=%0d. Sending burst...", $time, fetch_count);

        for (i = 0; i < NEV; i = i + 1)
            aer_event(i[6:0] % 7'd126, (i / 2) % 112, i[0]);

        while (ncore < NEV) @(posedge clk);
        for (i = 0; i < NEV; i = i + 1)
            check_abi(i, i[6:0] % 7'd126, (i / 2) % 112, i[0]);

        for (i = 0; i < 50000 && nres < ncore; i = i + 1) @(posedge clk);

        $display("[%0t] counters: evt=%0d push=%0d drop=%0d fetch=%0d results=%0d ncore=%0d nres=%0d packets=%0d",
                 $time, evt_count, core_push_count, core_drop_count, fetch_count,
                 result_count, ncore, nres, nres_pkt);

        if (evt_count != NEV)       begin $display("FAIL: evt_count=%0d want %0d", evt_count, NEV); errors = errors + 1; end
        if (core_push_count != NEV) begin $display("FAIL: core_push_count=%0d want %0d", core_push_count, NEV); errors = errors + 1; end
        if (core_drop_count != 0)   begin $display("FAIL: core_drop_count=%0d want 0", core_drop_count); errors = errors + 1; end
        if (raw_push_count != NEV)  begin $display("FAIL: raw_push_count=%0d want %0d", raw_push_count, NEV); errors = errors + 1; end
        if (raw_drop_count != 0)    begin $display("FAIL: raw_drop_count=%0d want 0", raw_drop_count); errors = errors + 1; end
        if (nraw != NEV)            begin $display("FAIL: nraw=%0d want %0d", nraw, NEV); errors = errors + 1; end
        if (nraw_pkt == 0)          begin $display("FAIL: raw stream never asserted tlast"); errors = errors + 1; end
        if (rd_err_count != 0)      begin $display("FAIL: rd_err_count=%0d want 0", rd_err_count); errors = errors + 1; end
        if (nres != ncore)          begin $display("FAIL: nres=%0d want ncore=%0d", nres, ncore); errors = errors + 1; end
        if (nres_pkt == 0)          begin $display("FAIL: result stream never asserted tlast"); errors = errors + 1; end

        if (errors == 0)
            $display("[%0t] PASS: continuous burst serviced (%0d results / %0d accepted)",
                     $time, nres, ncore);
        else
            $display("[%0t] FAIL: %0d error(s)", $time, errors);

        #100 $finish;
    end

    initial begin
        #TIMEOUT_NS;
        $display("FAIL: timeout -- evt=%0d push=%0d drop=%0d fetch=%0d results=%0d ncore=%0d nres=%0d",
                 evt_count, core_push_count, core_drop_count, fetch_count, result_count, ncore, nres);
        $finish;
    end

endmodule
