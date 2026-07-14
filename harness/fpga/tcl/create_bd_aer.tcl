# ============================================================
# Vivado Block Design -- KR260 / ZynqMP: DVS(AER) -> {raw stream, ActNow core}
#
# Grown from create_bd_skeleton.tcl (PS + clock/reset), which stays as the
# minimal reference. What this adds is everything the PS side of the two streams
# needs; the PL-side logic itself lives in static/*.v (actnow_pl), not here.
#
#   PS (Zynq US+)
#    ├─ M_AXI_HPM0_LPD ─▶ control: 2x AXI-DMA (S2MM), AXI-BRAM-Ctrl, 4x AXI-GPIO
#    ├─ S_AXI_HP0      ◀─ data:    both DMAs write events/results into DDR
#    └─ pl_clk0 (100 MHz) + pl_resetn0  ─▶ the whole PL
#
#   dma_raw : s_axis_raw (external) -> DDR   -- stream A, the untouched camera data
#   dma_res : s_axis_res (external) -> DDR   -- stream B, what the core produced
#   bram    : PS writes firmware via AXI-BRAM-Ctrl (port A);
#             port B (external) is read by the core's rom_bram_adapter
#   gpio    : control (core warm-reset, decimation) + status counters
#
# Both DMAs are simple-mode (no scatter-gather), 32-bit streams: PYNQ's DMA
# driver drives exactly this shape, and a 32-bit event is one beat.
# ============================================================

set design_name "actnow_aer_kr260"

# ------------------------------------------------------------
# Clear old block design if it exists
# ------------------------------------------------------------
set old_bd_files [get_files -quiet "*${design_name}.bd"]
if {[llength $old_bd_files] > 0} {
    puts "Removing old BD file(s): $old_bd_files"
    catch { close_bd_design [get_bd_designs -quiet $design_name] }
    remove_files $old_bd_files
}

set old_bd_designs [get_bd_designs -quiet $design_name]
if {[llength $old_bd_designs] > 0} {
    current_bd_design $design_name
    delete_bd_objs [get_bd_cells -quiet *]
    delete_bd_objs [get_bd_ports -quiet *]
}

create_bd_design $design_name
current_bd_design $design_name

# ============================================================
# Zynq UltraScale+ MPSoC
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e ps

catch {
    apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e \
        -config {apply_board_preset "1"} [get_bd_cells ps]
}

# LPD master for control, HP0 slave for the DMA writes, one 100 MHz PL clock.
set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0 {0} \
    CONFIG.PSU__USE__M_AXI_GP1 {0} \
    CONFIG.PSU__USE__M_AXI_GP2 {1} \
    CONFIG.PSU__USE__S_AXI_GP2 {1} \
    CONFIG.PSU__SAXIGP2__DATA_WIDTH {64} \
    CONFIG.PSU__USE__IRQ0 {1} \
    CONFIG.PSU__FPGA_PL0_ENABLE {1} \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {100} \
] [get_bd_cells ps]

# ============================================================
# Reset infrastructure (synchronised to pl_clk0)
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ps
connect_bd_net [get_bd_pins ps/pl_clk0]     [get_bd_pins rst_ps/slowest_sync_clk]
connect_bd_net [get_bd_pins ps/pl_resetn0]  [get_bd_pins rst_ps/ext_reset_in]

# Clock/reset for the PL logic in fpga_top (actnow_pl).
create_bd_port -dir O -type clk pl_clk0_out
connect_bd_net [get_bd_ports pl_clk0_out] [get_bd_pins ps/pl_clk0]
create_bd_port -dir O -type rst pl_resetn0_out
connect_bd_net [get_bd_ports pl_resetn0_out] [get_bd_pins rst_ps/peripheral_aresetn]

# ============================================================
# 2x AXI-DMA, receive-only (S2MM), simple mode
# ============================================================
foreach d {dma_raw dma_res} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma $d
    set_property -dict [list \
        CONFIG.c_include_sg {0} \
        CONFIG.c_sg_include_stscntrl_strm {0} \
        CONFIG.c_include_mm2s {0} \
        CONFIG.c_include_s2mm {1} \
        CONFIG.c_include_s2mm_dre {0} \
        CONFIG.c_s2mm_burst_size {16} \
        CONFIG.c_m_axi_s2mm_data_width {32} \
        CONFIG.c_addr_width {32} \
    ] [get_bd_cells $d]
}

# ============================================================
# Firmware BRAM: PS writes it (port A via AXI-BRAM-Ctrl), the core reads it
# (port B, exported to the PL). 32 KiB = 8192 32-bit words -- rom_bram_adapter's
# ADDR_WORDS. This is what makes a firmware change a file copy + a warm reset
# instead of a bitstream rebuild.
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl bram_ctrl
set_property -dict [list \
    CONFIG.SINGLE_PORT_BRAM {1} \
    CONFIG.DATA_WIDTH {32} \
] [get_bd_cells bram_ctrl]

apply_bd_automation -rule xilinx.com:bd_rule:bram_cntlr \
    -config {BRAM "Auto" } [get_bd_intf_pins bram_ctrl/BRAM_PORTA]

# The automation gives us a single-port memory; make it true dual-port so port B
# can be pulled out to the core side.
set bram_gen [get_bd_cells -quiet -filter {VLNV =~ "*blk_mem_gen*"}]
set_property -dict [list \
    CONFIG.Memory_Type {True_Dual_Port_RAM} \
    CONFIG.Enable_B {Use_ENB_Pin} \
    CONFIG.Use_RSTB_Pin {true} \
    CONFIG.Port_B_Clock {100} \
    CONFIG.Port_B_Write_Rate {0} \
    CONFIG.Register_PortB_Output_of_Memory_Primitives {false} \
] $bram_gen

make_bd_intf_pins_external -name BRAM_PORTB [get_bd_intf_pins $bram_gen/BRAM_PORTB]

# ============================================================
# AXI-GPIO: control out, status counters in
#
#   gpio_ctrl : ch1 = ctrl  (bit 0 = core warm-reset pulse)
#               ch2 = decim ([15:0] core stream, [31:16] raw stream)
#   gpio_s0   : ch1 = req_count        ch2 = evt_count
#   gpio_s1   : ch1 = core_drop_count  ch2 = result_count
#   gpio_s2   : ch1 = fetch_count      ch2 = last_event
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio gpio_ctrl
set_property -dict [list CONFIG.C_IS_DUAL {1} \
    CONFIG.C_ALL_OUTPUTS {1} CONFIG.C_ALL_OUTPUTS_2 {1} \
    CONFIG.C_GPIO_WIDTH {32} CONFIG.C_GPIO2_WIDTH {32}] [get_bd_cells gpio_ctrl]

foreach g {gpio_s0 gpio_s1 gpio_s2} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio $g
    set_property -dict [list CONFIG.C_IS_DUAL {1} \
        CONFIG.C_ALL_INPUTS {1} CONFIG.C_ALL_INPUTS_2 {1} \
        CONFIG.C_GPIO_WIDTH {32} CONFIG.C_GPIO2_WIDTH {32}] [get_bd_cells $g]
}

# ============================================================
# AXI connection automation
#   control: PS LPD master  -> every AXI-Lite slave
#   data:    each DMA S2MM  -> PS HP0 (DDR)
# ============================================================
foreach s {dma_raw/S_AXI_LITE dma_res/S_AXI_LITE bram_ctrl/S_AXI \
           gpio_ctrl/S_AXI gpio_s0/S_AXI gpio_s1/S_AXI gpio_s2/S_AXI} {
    apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
        -config [list Master {/ps/M_AXI_HPM0_LPD} Clk {Auto}] [get_bd_intf_pins $s]
}

# Data path: both DMAs share one SmartConnect into HP0. Built explicitly rather
# than by automation -- applying the axi4 rule once per DMA gives each its own
# interconnect, and the second one ends up without a valid master.
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect axi_smc_dma
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1}] [get_bd_cells axi_smc_dma]

connect_bd_intf_net [get_bd_intf_pins dma_raw/M_AXI_S2MM] [get_bd_intf_pins axi_smc_dma/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins dma_res/M_AXI_S2MM] [get_bd_intf_pins axi_smc_dma/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_smc_dma/M00_AXI] [get_bd_intf_pins ps/S_AXI_HP0_FPD]

connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins axi_smc_dma/aclk]
connect_bd_net [get_bd_pins rst_ps/peripheral_aresetn] [get_bd_pins axi_smc_dma/aresetn]
connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins ps/saxihp0_fpd_aclk]
foreach d {dma_raw dma_res} {
    catch { connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins $d/m_axi_s2mm_aclk] }
    catch { connect_bd_net [get_bd_pins rst_ps/peripheral_aresetn] [get_bd_pins $d/axi_resetn] }
}

# ============================================================
# Event streams in from the PL (fpga_top drives these)
#
# The exported stream ports must declare the clock they are synchronous to, and
# at the *actual* PL clock frequency -- the KR260 preset's PL0 lands on 96.97 MHz,
# not the 100 MHz we asked for, and a FREQ_HZ mismatch is a hard BD error.
# ============================================================
make_bd_intf_pins_external -name s_axis_raw [get_bd_intf_pins dma_raw/S_AXIS_S2MM]
make_bd_intf_pins_external -name s_axis_res [get_bd_intf_pins dma_res/S_AXIS_S2MM]

set pl_freq [get_property CONFIG.FREQ_HZ [get_bd_pins ps/pl_clk0]]
puts "== PL clock is $pl_freq Hz"
set_property -dict [list CONFIG.FREQ_HZ $pl_freq \
    CONFIG.ASSOCIATED_BUSIF {s_axis_raw:s_axis_res}] [get_bd_ports pl_clk0_out]
set_property CONFIG.FREQ_HZ $pl_freq [get_bd_intf_ports s_axis_raw]
set_property CONFIG.FREQ_HZ $pl_freq [get_bd_intf_ports s_axis_res]

# ============================================================
# DMA completion interrupts -> PS (PYNQ's DMA driver uses them; polling works too)
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat irq_concat
set_property CONFIG.NUM_PORTS {2} [get_bd_cells irq_concat]
connect_bd_net [get_bd_pins dma_raw/s2mm_introut] [get_bd_pins irq_concat/In0]
connect_bd_net [get_bd_pins dma_res/s2mm_introut] [get_bd_pins irq_concat/In1]
connect_bd_net [get_bd_pins irq_concat/dout]      [get_bd_pins ps/pl_ps_irq0]

# ============================================================
# GPIO vectors out to the PL
# ============================================================
create_bd_port -dir O -from 31 -to 0 gpio_ctrl_out
connect_bd_net [get_bd_ports gpio_ctrl_out] [get_bd_pins gpio_ctrl/gpio_io_o]
create_bd_port -dir O -from 31 -to 0 gpio_decim_out
connect_bd_net [get_bd_ports gpio_decim_out] [get_bd_pins gpio_ctrl/gpio2_io_o]

create_bd_port -dir I -from 31 -to 0 gpio_stat0_in
connect_bd_net [get_bd_ports gpio_stat0_in] [get_bd_pins gpio_s0/gpio_io_i]
create_bd_port -dir I -from 31 -to 0 gpio_stat1_in
connect_bd_net [get_bd_ports gpio_stat1_in] [get_bd_pins gpio_s0/gpio2_io_i]
create_bd_port -dir I -from 31 -to 0 gpio_stat2_in
connect_bd_net [get_bd_ports gpio_stat2_in] [get_bd_pins gpio_s1/gpio_io_i]
create_bd_port -dir I -from 31 -to 0 gpio_stat3_in
connect_bd_net [get_bd_ports gpio_stat3_in] [get_bd_pins gpio_s1/gpio2_io_i]
create_bd_port -dir I -from 31 -to 0 gpio_stat4_in
connect_bd_net [get_bd_ports gpio_stat4_in] [get_bd_pins gpio_s2/gpio_io_i]
create_bd_port -dir I -from 31 -to 0 gpio_stat5_in
connect_bd_net [get_bd_ports gpio_stat5_in] [get_bd_pins gpio_s2/gpio2_io_i]

# ============================================================
# Addresses, validation
# ============================================================
assign_bd_address
regenerate_bd_layout
validate_bd_design
save_bd_design
