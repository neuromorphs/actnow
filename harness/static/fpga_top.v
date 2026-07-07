`timescale 1ns/1ps

module fpga_top;

    wire        soc_clock;
    wire        soc_reset;
    wire [15:0] soc_event_id_ready;
    wire [15:0] soc_event_id_valid;
    wire [15:0] soc_event_id;
    wire        soc_imem_resp_ready;
    wire        soc_imem_resp_valid;
    wire [31:0] soc_imem_resp;
    wire        soc_imem_req_valid;
    wire        soc_imem_req_ready;
    wire [31:0] soc_imem_req;

    \soc soc_i (
        .\clock             (soc_clock),
        .\reset             (soc_reset),
        .\event_id_0_ready  (soc_event_id_ready[0]),
        .\event_id_0_valid  (soc_event_id_valid[0]),
        .\event_id_0        (soc_event_id[0]),
        .\event_id_1_ready  (soc_event_id_ready[1]),
        .\event_id_1_valid  (soc_event_id_valid[1]),
        .\event_id_1        (soc_event_id[1]),
        .\event_id_2_ready  (soc_event_id_ready[2]),
        .\event_id_2_valid  (soc_event_id_valid[2]),
        .\event_id_2        (soc_event_id[2]),
        .\event_id_3_ready  (soc_event_id_ready[3]),
        .\event_id_3_valid  (soc_event_id_valid[3]),
        .\event_id_3        (soc_event_id[3]),
        .\event_id_4_ready  (soc_event_id_ready[4]),
        .\event_id_4_valid  (soc_event_id_valid[4]),
        .\event_id_4        (soc_event_id[4]),
        .\event_id_5_ready  (soc_event_id_ready[5]),
        .\event_id_5_valid  (soc_event_id_valid[5]),
        .\event_id_5        (soc_event_id[5]),
        .\event_id_6_ready  (soc_event_id_ready[6]),
        .\event_id_6_valid  (soc_event_id_valid[6]),
        .\event_id_6        (soc_event_id[6]),
        .\event_id_7_ready  (soc_event_id_ready[7]),
        .\event_id_7_valid  (soc_event_id_valid[7]),
        .\event_id_7        (soc_event_id[7]),
        .\event_id_8_ready  (soc_event_id_ready[8]),
        .\event_id_8_valid  (soc_event_id_valid[8]),
        .\event_id_8        (soc_event_id[8]),
        .\event_id_9_ready  (soc_event_id_ready[9]),
        .\event_id_9_valid  (soc_event_id_valid[9]),
        .\event_id_9        (soc_event_id[9]),
        .\event_id_10_ready (soc_event_id_ready[10]),
        .\event_id_10_valid (soc_event_id_valid[10]),
        .\event_id_10       (soc_event_id[10]),
        .\event_id_11_ready (soc_event_id_ready[11]),
        .\event_id_11_valid (soc_event_id_valid[11]),
        .\event_id_11       (soc_event_id[11]),
        .\event_id_12_ready (soc_event_id_ready[12]),
        .\event_id_12_valid (soc_event_id_valid[12]),
        .\event_id_12       (soc_event_id[12]),
        .\event_id_13_ready (soc_event_id_ready[13]),
        .\event_id_13_valid (soc_event_id_valid[13]),
        .\event_id_13       (soc_event_id[13]),
        .\event_id_14_ready (soc_event_id_ready[14]),
        .\event_id_14_valid (soc_event_id_valid[14]),
        .\event_id_14       (soc_event_id[14]),
        .\event_id_15_ready (soc_event_id_ready[15]),
        .\event_id_15_valid (soc_event_id_valid[15]),
        .\event_id_15       (soc_event_id[15]),
        .\imem_resp_ready   (soc_imem_resp_ready),
        .\imem_resp_valid   (soc_imem_resp_valid),
        .\imem_resp         (soc_imem_resp),
        .\imem_req_valid    (soc_imem_req_valid),
        .\imem_req_ready    (soc_imem_req_ready),
        .\imem_req          (soc_imem_req)
    );

endmodule
