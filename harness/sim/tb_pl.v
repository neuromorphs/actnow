`timescale 1ns/1ps

// End-to-end simulation of the requirements-only PL datapath:
// AER pins -> receiver -> required 32-bit event word -> core.fifo_push ->
// core boots real firmware from BRAM -> core.io_* -> one packetized stream.

module tb_pl;

    parameter integer TIMEOUT_NS = 2_000_000;

    reg clk = 1'b0;
    reg resetn = 1'b0;
    always #5 clk = ~clk;

    reg  [8:0] aer_data  = 9'b0;
    reg        aer_req_n = 1'b1;
    wire       aer_ack_n;

    wire        res_tvalid, res_tlast;
    wire [31:0] res_tdata;
    reg         res_tready = 1'b1;

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
        .reset_count       (reset_count)
    );

    localparam integer MAXN = 256;
    reg [31:0] core_seen [0:MAXN-1];
    reg [31:0] res_seen  [0:MAXN-1];
    integer ncore = 0, nres = 0, nres_pkt = 0;

    always @(posedge clk) if (resetn && dut.core_in_tvalid && dut.core_in_tready) begin
        core_seen[ncore] <= dut.core_in_tdata;
        $display("[%0t] CORE_IN #%0d: 0x%08h (x=%0d y=%0d ts=%0d p=%0d)",
                 $time, ncore, dut.core_in_tdata, dut.core_in_tdata[30:24],
                 dut.core_in_tdata[23:17], dut.core_in_tdata[16:1], dut.core_in_tdata[0]);
        ncore <= ncore + 1;
    end

    always @(posedge clk) if (resetn && res_tvalid && res_tready) begin
        res_seen[nres] <= res_tdata;
        if (res_tlast) nres_pkt <= nres_pkt + 1;
        $display("[%0t] RES     #%0d: 0x%08h", $time, nres, res_tdata);
        nres <= nres + 1;
    end

    task aer_word(input [8:0] w);
        begin
            aer_data  = w;
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

    integer errors = 0;
    integer i;

    function [31:0] rotate_req_word(input [31:0] word);
        integer x, y, tx, ty, rx, ry, nx, ny;
        begin
            x = word[30:24];
            y = word[23:17];
            tx = x - 63;
            ty = y - 56;
            rx = (tx - ty) >>> 1;
            ry = (tx + ty) >>> 1;
            nx = rx + 63;
            ny = ry + 56;
            if (nx < 0) nx = 0;
            if (nx > 125) nx = 125;
            if (ny < 0) ny = 0;
            if (ny > 111) ny = 111;
            rotate_req_word = (word & 32'h80_01_FF_FF) | (nx[6:0] << 24) | (ny[6:0] << 17);
        end
    endfunction

    task check_word(input integer idx, input [6:0] x, input [6:0] y, input pol);
        reg [31:0] w;
        begin
            while (ncore <= idx) @(posedge clk);
            w = core_seen[idx];
            if (w[31] !== 1'b0 || w[30:24] !== x || w[23:17] !== y || w[0] !== pol) begin
                $display("FAIL: core word %0d has bad ABI fields: got 0x%08h want x=%0d y=%0d p=%0d",
                         idx, w, x, y, pol);
                errors = errors + 1;
            end
        end
    endtask

    task expect_results(input integer first_core, input integer n);
        integer k;
        reg [31:0] want;
        begin
            while (nres < first_core + n) begin
                @(posedge clk);
                if ($time > TIMEOUT_NS * 1000) begin
                    $display("FAIL: timed out waiting for %0d results (have %0d)", n, nres);
                    $finish;
                end
            end
            for (k = 0; k < n; k = k + 1) begin
                want = rotate_req_word(core_seen[first_core + k]);
                if (res_seen[first_core + k] !== want) begin
                    $display("FAIL: result %0d: want 0x%08h, got 0x%08h",
                             first_core + k, want, res_seen[first_core + k]);
                    errors = errors + 1;
                end
            end
        end
    endtask

    initial begin
        $display("=== tb_pl: requirements-only AER -> ActNow core -> result stream ===");

        for (i = 0; i < BRAM_WORDS; i = i + 1) bram[i] = 32'b0;
        $readmemb("rom.mem", bram);

        resetn = 1'b0;
        repeat (16) @(posedge clk);
        resetn = 1'b1;

        ctrl = 32'h1;
        repeat (4) @(posedge clk);
        ctrl = 32'h0;

        aer_event(7'd10, 7'd20, 1'b1);
        aer_event(7'd11, 7'd20, 1'b0);
        aer_event(7'd12, 7'd21, 1'b1);
        aer_event(7'd13, 7'd21, 1'b0);

        check_word(0, 7'd10, 7'd20, 1'b1);
        check_word(1, 7'd11, 7'd20, 1'b0);
        check_word(2, 7'd12, 7'd21, 1'b1);
        check_word(3, 7'd13, 7'd21, 1'b0);
        expect_results(0, 4);

        aer_event(7'd30, 7'd40, 1'b1);
        aer_event(7'd31, 7'd40, 1'b0);
        aer_event(7'd32, 7'd41, 1'b1);
        aer_event(7'd33, 7'd41, 1'b0);

        check_word(4, 7'd30, 7'd40, 1'b1);
        check_word(5, 7'd31, 7'd40, 1'b0);
        check_word(6, 7'd32, 7'd41, 1'b1);
        check_word(7, 7'd33, 7'd41, 1'b0);
        expect_results(4, 4);

        repeat (16) @(posedge clk);
        $display("[%0t] counters: req=%0d words=%0d evt=%0d fetch=%0d push=%0d results=%0d",
                 $time, req_count, word_count, evt_count, fetch_count, core_push_count, result_count);

        if (evt_count != 8)       begin $display("FAIL: evt_count=%0d, want 8", evt_count); errors = errors + 1; end
        if (core_push_count != 8) begin $display("FAIL: core_push_count=%0d, want 8", core_push_count); errors = errors + 1; end
        if (rd_err_count != 0)    begin $display("FAIL: %0d illegal base-6 reads", rd_err_count); errors = errors + 1; end
        if (core_drop_count != 0) begin $display("FAIL: %0d core events dropped", core_drop_count); errors = errors + 1; end
        if (nres_pkt == 0)        begin $display("FAIL: result stream never asserted tlast"); errors = errors + 1; end

        if (errors == 0)
            $display("[%0t] PASS: %0d events entered the core, %0d results streamed out", $time, ncore, nres);
        else
            $display("[%0t] FAIL: %0d error(s)", $time, errors);

        #100 $finish;
    end

    initial begin
        #TIMEOUT_NS;
        $display("FAIL: timeout -- results=%0d fetch=%0d evt=%0d push=%0d", nres, fetch_count, evt_count, core_push_count);
        $finish;
    end

endmodule
