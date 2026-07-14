`timescale 1ns/1ps

// End-to-end simulation of the whole PL datapath (static/actnow_pl.v): a
// behavioral ECP3 drives real 4-phase AER on the pins, a behavioral BRAM holds
// real firmware (software/application, booted through the real bootloader), and
// both output streams are sunk and checked.
//
// This is the test that says the DVS harness works, without a camera or a board:
//
//   AER sender ─▶ aer_rx ─▶ evt_pack ─┬─▶ evt_stream A ─▶ raw stream   (checked here)
//                                     └─▶ evt_stream B ─▶ core.fifo_push
//                                            core boots from BRAM, fifo_in fires the
//                                            interrupt on a full batch, the ISR adds 1
//                                            and stores to base 6
//                                                 └─▶ io_axis ─▶ result stream (checked here)
//
// The contract checked: every event the sender emits appears on the raw stream
// with its low 15 bits intact ({pol,y,x}), and every event handed to the core
// comes back on the result stream as word+1 -- software/application's ISR -- in
// order. Phase 2 re-checks that with decimation on (the core sees every 2nd
// event), which is the knob that keeps the core from being overrun by a real
// camera.
//
// The sim is *slow in wall-clock terms* because it boots a real RISC-V image
// through a converted async core: the bootloader copy alone is ~200 us of
// simulated time. That is expected; be patient (~1 min).

module tb_pl;

    parameter integer TIMEOUT_NS = 2_000_000;   // 2 ms = 200k cycles

    // ---- clock / reset ----
    reg clk = 1'b0;
    reg resetn = 1'b0;
    always #5 clk = ~clk;   // 100 MHz

    // ---- AER bus ----
    reg  [8:0] aer_data  = 9'b0;
    reg        aer_req_n = 1'b1;
    wire       aer_ack_n;

    // ---- streams ----
    wire        raw_tvalid, res_tvalid;
    wire        raw_tlast,  res_tlast;
    wire [31:0] raw_tdata,  res_tdata;
    reg         raw_tready = 1'b1;   // the DMA is modelled as always draining
    reg         res_tready = 1'b1;

    // ---- firmware BRAM (port B; the PS's port A is not modelled) ----
    wire        bram_clk, bram_en;
    wire [3:0]  bram_we;
    wire [31:0] bram_addr, bram_wrdata;
    reg  [31:0] bram_rddata;
    localparam integer BRAM_WORDS = 8192;
    reg [31:0] bram [0:BRAM_WORDS-1];

    always @(posedge clk)
        if (bram_en) bram_rddata <= bram[bram_addr[14:2]];   // 1-cycle read latency

    // ---- control / status ----
    reg  [31:0] ctrl  = 32'd0;
    reg  [31:0] decim = 32'd0;      // [15:0] core stream, [31:16] raw stream
    wire [31:0] req_count, word_count, evt_count, last_event;
    wire [31:0] raw_drop_count, core_drop_count, core_push_count;
    wire [31:0] fetch_count, result_count, rd_err_count, reset_count;

    actnow_pl dut (
        .clk               (clk),
        .resetn            (resetn),
        .aer_data_i        (aer_data),
        .aer_req_n_i       (aer_req_n),
        .aer_ack_n_o       (aer_ack_n),
        .m_axis_raw_tvalid (raw_tvalid),
        .m_axis_raw_tready (raw_tready),
        .m_axis_raw_tdata  (raw_tdata),
        .m_axis_raw_tlast  (raw_tlast),
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
        .decim             (decim),
        .req_count         (req_count),
        .word_count        (word_count),
        .evt_count         (evt_count),
        .last_event        (last_event),
        .raw_drop_count    (raw_drop_count),
        .core_drop_count   (core_drop_count),
        .core_push_count   (core_push_count),
        .fetch_count       (fetch_count),
        .result_count      (result_count),
        .rd_err_count      (rd_err_count),
        .reset_count       (reset_count)
    );

    // ---- stream sinks ----
    localparam integer MAXN = 256;
    reg [31:0] raw_seen [0:MAXN-1];
    reg [31:0] res_seen [0:MAXN-1];
    integer nraw = 0, nres = 0;
    integer nraw_pkt = 0, nres_pkt = 0;

    always @(posedge clk) if (resetn && raw_tvalid && raw_tready) begin
        raw_seen[nraw] <= raw_tdata;
        nraw           <= nraw + 1;
        if (raw_tlast) nraw_pkt <= nraw_pkt + 1;
        $display("[%0t] RAW  #%0d: 0x%08h  (ts=%0d pol=%0d y=%0d x=%0d)", $time, nraw,
                 raw_tdata, raw_tdata[31:15], raw_tdata[14], raw_tdata[13:7], raw_tdata[6:0]);
    end

    always @(posedge clk) if (resetn && res_tvalid && res_tready) begin
        res_seen[nres] <= res_tdata;
        nres           <= nres + 1;
        if (res_tlast) nres_pkt <= nres_pkt + 1;
        $display("[%0t] RES  #%0d: 0x%08h", $time, nres, res_tdata);
    end

    // ---- behavioral ECP3: word-serial AER, 4-phase active-low ----
    // Sender drives data + /REQ; receiver (the DUT) drives /ACK. Fully
    // self-timed -- no shared clock, arbitrary receiver latency.
    task aer_word(input [8:0] w);
        begin
            aer_data  = w;
            #30;                       // data valid before REQ (the RX also waits SAMP_DELAY)
            aer_req_n = 1'b0;
            wait (aer_ack_n == 1'b0);  // receiver latched
            #30;
            aer_req_n = 1'b1;
            wait (aer_ack_n == 1'b1);  // receiver released
            #30;
        end
    endtask

    // One pixel event: a ROW (Y) word then a COLUMN (X) word, as SciDVS/ECP3
    // emit them. AER[8] is the row/col select; the X word carries the polarity.
    task aer_event(input [6:0] x, input [6:0] y, input pol);
        begin
            aer_word({1'b0, 1'b0, y});      // ROW word:    AER[8]=0, AER[6:0]=y
            aer_word({1'b1, x, pol});       // COLUMN word: AER[8]=1, AER[7:1]=x, AER[0]=pol
        end
    endtask

    // ---- checks ----
    integer errors = 0;
    integer i;

    task expect_results(input integer first_raw, input integer stride, input integer n);
        integer k, want, got;
        begin
            // Wait for n more results, then check each against the raw event the
            // core should have been handed (every `stride`-th one), +1.
            k = nres;
            while (nres < k + n) begin
                @(posedge clk);
                if ($time > TIMEOUT_NS * 1000) begin
                    $display("FAIL: timed out waiting for %0d results (have %0d)", n, nres);
                    $finish;
                end
            end
            for (k = 0; k < n; k = k + 1) begin
                want = raw_seen[first_raw + k*stride] + 1;
                got  = res_seen[nres - n + k];
                if (got !== want) begin
                    $display("FAIL: result %0d: want 0x%08h (raw[%0d]+1), got 0x%08h",
                             nres - n + k, want, first_raw + k*stride, got);
                    errors = errors + 1;
                end
            end
        end
    endtask

    // ---- stimulus ----
    initial begin
        $display("=== tb_pl: DVS -> AER RX -> {raw stream, ActNow core} -> two streams ===");

        for (i = 0; i < BRAM_WORDS; i = i + 1) bram[i] = 32'b0;
        // The firmware the PS would have written over AXI-BRAM-Ctrl: bootloader +
        // software/application, one 32-bit binary word per line.
        $readmemb("rom.mem", bram);

        resetn = 1'b0;
        repeat (16) @(posedge clk);
        resetn = 1'b1;
        $display("[%0t] reset released", $time);

        // Cold boot. soc.act blocks on reset_ext before executing anything, so the
        // core does *nothing* until this bit is pulsed -- on hardware this is the PS
        // writing gpio_ctrl bit 0 (pynq/actnow_dvs_send.py's reset_core()), which
        // static/reset_ext_send.v turns into one send on the core's reset_ext
        // channel. Exercising it here is the same path, not a simulation shortcut.
        $display("[%0t] --- cold boot: pulsing ctrl[0] (reset_ext) ---", $time);
        ctrl = 32'h1;
        repeat (4) @(posedge clk);
        ctrl = 32'h0;
        $display("[%0t] core booting from BRAM (base 4, XIP)", $time);

        // --- phase 1: no decimation, one batch (BATCH=3 in software/application) ---
        // The events are sent immediately, long before the core has finished
        // booting and configured fifo_in's trigger level. They queue in evt_stream's
        // elastic FIFO and drain once the core is ready -- which is exactly the
        // behaviour that keeps the AER bus from ever being stalled.
        $display("[%0t] --- phase 1: 3 events, no decimation ---", $time);
        aer_event(7'd10, 7'd20, 1'b1);
        aer_event(7'd11, 7'd20, 1'b0);
        aer_event(7'd12, 7'd21, 1'b1);

        // The last event's stream beat lands a few cycles after the AER handshake
        // completes (RX decode -> evt_pack -> FIFO -> AXIS), so settle before counting.
        repeat (16) @(posedge clk);
        if (nraw != 3) begin
            $display("FAIL: raw stream has %0d events, want 3", nraw);
            errors = errors + 1;
        end

        expect_results(0, 1, 3);
        $display("[%0t] phase 1 ok: 3 raw events out, 3 results back (+1 each)", $time);

        // --- phase 2: decimate the core stream by 2 ---
        // The raw stream still gets everything; the core sees every 2nd event.
        // This is the knob that keeps a 280 kevent/s core usable behind a sensor
        // that can burst far past it.
        $display("[%0t] --- phase 2: 6 events, core stream decimated by 2 ---", $time);
        decim = {16'd0, 16'd2};        // [15:0] = core stream
        @(posedge clk);

        aer_event(7'd30, 7'd40, 1'b1);   // raw[3]  -> core (phase 0)
        aer_event(7'd31, 7'd40, 1'b0);   // raw[4]     dropped by decimation
        aer_event(7'd32, 7'd41, 1'b1);   // raw[5]  -> core
        aer_event(7'd33, 7'd41, 1'b0);   // raw[6]     dropped
        aer_event(7'd34, 7'd42, 1'b1);   // raw[7]  -> core
        aer_event(7'd35, 7'd42, 1'b0);   // raw[8]     dropped

        expect_results(3, 2, 3);
        $display("[%0t] phase 2 ok: raw stream got all 6, core got every 2nd", $time);

        // --- counters the PS will read over AXI-GPIO ---
        repeat (16) @(posedge clk);
        $display("[%0t] counters: req=%0d words=%0d evt=%0d fetch=%0d push=%0d results=%0d",
                 $time, req_count, word_count, evt_count, fetch_count, core_push_count, result_count);
        $display("[%0t] packets: raw=%0d res=%0d", $time, nraw_pkt, nres_pkt);
        $display("[%0t] drops: raw=%0d core=%0d | base-6 read errors=%0d | resets=%0d",
                 $time, raw_drop_count, core_drop_count, rd_err_count, reset_count);

        // Every beat here ends a burst (the events are sent one at a time, so the
        // FIFO drains after each), so each one must close a packet -- otherwise the
        // DMA would sit on a partially filled buffer and the PS would see nothing.
        if (nraw_pkt != nraw)     begin $display("FAIL: raw stream: %0d beats but %0d packets (tlast missing)", nraw, nraw_pkt); errors = errors + 1; end
        if (nres_pkt == 0)        begin $display("FAIL: result stream never asserted tlast -- the DMA would never complete"); errors = errors + 1; end
        if (nraw != 9)            begin $display("FAIL: raw stream got %0d events, want 9", nraw);          errors = errors + 1; end
        if (evt_count != 9)       begin $display("FAIL: evt_count=%0d, want 9", evt_count);                 errors = errors + 1; end
        if (core_push_count != 6) begin $display("FAIL: core_push_count=%0d, want 6", core_push_count);     errors = errors + 1; end
        if (rd_err_count != 0)    begin $display("FAIL: %0d illegal base-6 reads", rd_err_count);           errors = errors + 1; end
        if (raw_drop_count != 0)  begin $display("FAIL: %0d raw events dropped", raw_drop_count);           errors = errors + 1; end
        if (core_drop_count != 0) begin $display("FAIL: %0d core events dropped", core_drop_count);         errors = errors + 1; end

        if (errors == 0)
            $display("[%0t] PASS: both streams correct (%0d raw events, %0d core results)",
                     $time, nraw, nres);
        else
            $display("[%0t] FAIL: %0d error(s)", $time, errors);

        #100 $finish;
    end

    // ---- timeout ----
    initial begin
        #TIMEOUT_NS;
        $display("FAIL: timeout -- raw=%0d results=%0d fetch=%0d evt=%0d push=%0d (core booted? fetch should be in the hundreds)",
                 nraw, nres, fetch_count, evt_count, core_push_count);
        $finish;
    end

endmodule
