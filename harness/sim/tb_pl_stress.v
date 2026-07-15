`timescale 1ns/1ps

// Stress variant of tb_pl: boot the core, then push a *continuous* burst of AER
// events (no per-batch wait, unlike tb_pl which sends exactly BATCH then blocks
// on expect_results). This reproduces the hardware condition -- the SciDVS
// delivers events every ~5 us regardless of whether the ISR has finished the
// previous batch -- that tb_pl never exercises.
//
// Reports how many results come back for NEV pushed events. Healthy core:
// results ~= NEV (minus decimation/drops). Bug: results stalls at 3 (one batch),
// which is what the board shows.

module tb_pl_stress;

    parameter integer TIMEOUT_NS = 8_000_000;
    parameter integer NEV        = 60;   // events to blast continuously

    reg clk = 1'b0;
    reg resetn = 1'b0;
    always #5 clk = ~clk;   // 100 MHz

    reg  [8:0] aer_data  = 9'b0;
    reg        aer_req_n = 1'b1;
    wire       aer_ack_n;

    wire        raw_tvalid, res_tvalid, raw_tlast, res_tlast;
    wire [31:0] raw_tdata,  res_tdata;
    reg         raw_tready = 1'b1;
    reg         res_tready = 1'b1;   // result DMA modelled as always draining

    wire        bram_clk, bram_en;
    wire [3:0]  bram_we;
    wire [31:0] bram_addr, bram_wrdata;
    reg  [31:0] bram_rddata;
    localparam integer BRAM_WORDS = 8192;
    reg [31:0] bram [0:BRAM_WORDS-1];
    always @(posedge clk)
        if (bram_en) bram_rddata <= bram[bram_addr[14:2]];

    reg  [31:0] ctrl  = 32'd0;
    reg  [31:0] decim = 32'd0;
    wire [31:0] req_count, word_count, evt_count, last_event;
    wire [31:0] raw_drop_count, core_drop_count, core_push_count;
    wire [31:0] fetch_count, result_count, rd_err_count, reset_count;

    actnow_pl dut (
        .clk(clk), .resetn(resetn),
        .aer_data_i(aer_data), .aer_req_n_i(aer_req_n), .aer_ack_n_o(aer_ack_n),
        .m_axis_raw_tvalid(raw_tvalid), .m_axis_raw_tready(raw_tready),
        .m_axis_raw_tdata(raw_tdata), .m_axis_raw_tlast(raw_tlast),
        .m_axis_res_tvalid(res_tvalid), .m_axis_res_tready(res_tready),
        .m_axis_res_tdata(res_tdata), .m_axis_res_tlast(res_tlast),
        .bram_clk(bram_clk), .bram_en(bram_en), .bram_we(bram_we),
        .bram_addr(bram_addr), .bram_wrdata(bram_wrdata), .bram_rddata(bram_rddata),
        .ctrl(ctrl), .decim(decim),
        .req_count(req_count), .word_count(word_count), .evt_count(evt_count),
        .last_event(last_event), .raw_drop_count(raw_drop_count),
        .core_drop_count(core_drop_count), .core_push_count(core_push_count),
        .fetch_count(fetch_count), .result_count(result_count),
        .rd_err_count(rd_err_count), .reset_count(reset_count)
    );

    integer nres = 0, nraw = 0;
    always @(posedge clk) if (resetn && raw_tvalid && raw_tready) nraw <= nraw + 1;
    always @(posedge clk) if (resetn && res_tvalid && res_tready) begin
        nres <= nres + 1;
        $display("[%0t] RES #%0d: 0x%08h  (result_count=%0d core_push=%0d core_drop=%0d)",
                 $time, nres, res_tdata, result_count, core_push_count, core_drop_count);
    end

    task aer_word(input [8:0] w);
        begin
            aer_data = w; #30; aer_req_n = 1'b0;
            wait (aer_ack_n == 1'b0); #30; aer_req_n = 1'b1;
            wait (aer_ack_n == 1'b1); #30;
        end
    endtask
    task aer_event(input [6:0] x, input [6:0] y, input pol);
        begin
            aer_word({1'b0, 1'b0, y});
            aer_word({1'b1, x, pol});
        end
    endtask

    integer i;
    integer last_report;
    initial begin
        $display("=== tb_pl_stress: continuous %0d-event burst ===", NEV);
        for (i = 0; i < BRAM_WORDS; i = i + 1) bram[i] = 32'b0;
        $readmemb("rom.mem", bram);

        resetn = 1'b0; repeat (16) @(posedge clk); resetn = 1'b1;
        $display("[%0t] reset released; pulsing reset_ext", $time);
        ctrl = 32'h1; repeat (4) @(posedge clk); ctrl = 32'h0;

        // Give the core time to boot and configure fifo_in before flooding, so we
        // isolate steady-state re-arm rather than the boot/queue interaction.
        repeat (25000) @(posedge clk);   // ~250 us
        $display("[%0t] boot window done: fetch=%0d. Blasting %0d events...",
                 $time, fetch_count, NEV);

        for (i = 0; i < NEV; i = i + 1)
            aer_event(i[6:0] % 126, (i/2)%112, i[0]);

        $display("[%0t] all %0d events sent; raw=%0d evt=%0d push=%0d drop=%0d results=%0d",
                 $time, NEV, nraw, evt_count, core_push_count, core_drop_count, result_count);

        // Let the core drain whatever it can.
        last_report = -1;
        for (i = 0; i < 400; i = i + 1) begin
            repeat (1000) @(posedge clk);   // 10 us
            if (nres != last_report) begin
                last_report = nres;
            end
        end

        $display("[%0t] DONE: pushed=%0d core_push=%0d core_drop=%0d results(nres)=%0d result_count=%0d",
                 $time, NEV, core_push_count, core_drop_count, nres, result_count);
        if (nres <= 3)
            $display("REPRO: core serviced only %0d results for %0d pushed events -- single-batch hang reproduced", nres, core_push_count);
        else if (nres >= core_push_count - 3)
            $display("HEALTHY: core serviced ~all pushed events (%0d results / %0d pushed)", nres, core_push_count);
        else
            $display("PARTIAL: %0d results / %0d pushed -- some servicing then stall", nres, core_push_count);
        $finish;
    end

    initial begin
        #TIMEOUT_NS;
        $display("[%0t] TIMEOUT: nres=%0d result_count=%0d core_push=%0d core_drop=%0d fetch=%0d",
                 $time, nres, result_count, core_push_count, core_drop_count, fetch_count);
        $finish;
    end
endmodule
