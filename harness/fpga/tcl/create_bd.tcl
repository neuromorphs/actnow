# ============================================================
# Vivado Block Design Tcl
# KR260 / ZynqMP + external AXI4-Lite + AXI BRAM + AXI FIFOs
# ============================================================

set design_name "async_rv32_harness_kr260"

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
    puts "Deleting old in-memory BD design: $design_name"
    current_bd_design $design_name
    delete_bd_objs [get_bd_cells -quiet *]
    delete_bd_objs [get_bd_intf_ports -quiet *]
    delete_bd_objs [get_bd_ports -quiet *]
}

# ------------------------------------------------------------
# Create new block design
# ------------------------------------------------------------
create_bd_design $design_name
current_bd_design $design_name

# ============================================================
# External AXI4-Lite input from external core
# ============================================================
create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI_EXT

set_property -dict [list \
    CONFIG.PROTOCOL {AXI4LITE} \
    CONFIG.DATA_WIDTH {32} \
    CONFIG.ADDR_WIDTH {32} \
    CONFIG.HAS_BURST {0} \
    CONFIG.HAS_LOCK {0} \
    CONFIG.HAS_CACHE {0} \
    CONFIG.HAS_PROT {1} \
    CONFIG.HAS_QOS {0} \
    CONFIG.HAS_REGION {0} \
    CONFIG.HAS_WSTRB {1} \
    CONFIG.HAS_BRESP {1} \
    CONFIG.HAS_RRESP {1} \
    CONFIG.SUPPORTS_NARROW_BURST {0} \
] [get_bd_intf_ports S_AXI_EXT]

# External AXI-Lite clock/reset from the external core side.
create_bd_port -dir I -type clk ext_axi_aclk
create_bd_port -dir I -type rst ext_axi_aresetn

set_property -dict [list \
    CONFIG.FREQ_HZ {100000000} \
    CONFIG.ASSOCIATED_BUSIF {S_AXI_EXT} \
    CONFIG.ASSOCIATED_RESET {ext_axi_aresetn} \
] [get_bd_ports ext_axi_aclk]

set_property CONFIG.POLARITY ACTIVE_LOW [get_bd_ports ext_axi_aresetn]

# ============================================================
# Zynq UltraScale+ MPSoC
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e zynq_ultra_ps_e_0

catch {
    apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e \
        -config {apply_board_preset "1"} \
        [get_bd_cells zynq_ultra_ps_e_0]
}

catch {
    set_property -dict [list \
        CONFIG.PSU__USE__M_AXI_GP0 {1} \
        CONFIG.PSU__USE__M_AXI_GP1 {0} \
        CONFIG.PSU__USE__M_AXI_GP2 {0} \
        CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {100} \
    ] [get_bd_cells zynq_ultra_ps_e_0]
}

# ============================================================
# Reset infrastructure
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset proc_sys_reset_0

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
               [get_bd_pins proc_sys_reset_0/slowest_sync_clk]

create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic rst_inv_0
set_property -dict [list \
    CONFIG.C_OPERATION {not} \
    CONFIG.C_SIZE {1} \
] [get_bd_cells rst_inv_0]

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_resetn0] \
               [get_bd_pins rst_inv_0/Op1]

connect_bd_net [get_bd_pins rst_inv_0/Res] \
               [get_bd_pins proc_sys_reset_0/ext_reset_in]

# ============================================================
# Export internal PL clock/reset for stream-side custom logic
#
# This is important because the exported AXI-Stream ports are
# clocked by the FIFO stream clock, which is the PS pl_clk0 domain.
# ============================================================
create_bd_port -dir O -type clk pl_clk0_out
connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
               [get_bd_ports pl_clk0_out]

set pl_clk_freq [get_property CONFIG.FREQ_HZ [get_bd_pins zynq_ultra_ps_e_0/pl_clk0]]
if {$pl_clk_freq eq ""} {
    set pl_clk_freq 100000000
}

set_property -dict [list \
    CONFIG.FREQ_HZ $pl_clk_freq \
] [get_bd_ports pl_clk0_out]

create_bd_port -dir O -type rst pl_resetn0_out
set_property CONFIG.POLARITY ACTIVE_LOW [get_bd_ports pl_resetn0_out]

connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] \
               [get_bd_ports pl_resetn0_out]

# ============================================================
# Connect required PS AXI master clock pins
# ============================================================
set fpd_hpm_clk [get_bd_pins -quiet zynq_ultra_ps_e_0/maxihpm0_fpd_aclk]
if {[llength $fpd_hpm_clk] > 0} {
    connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] $fpd_hpm_clk
}

set lpd_hpm_clk [get_bd_pins -quiet zynq_ultra_ps_e_0/maxihpm0_lpd_aclk]
if {[llength $lpd_hpm_clk] > 0} {
    connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] $lpd_hpm_clk
}

# ============================================================
# Shared AXI SmartConnect
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect axi_sc_0

set_property -dict [list \
    CONFIG.NUM_SI {2} \
    CONFIG.NUM_MI {3} \
] [get_bd_cells axi_sc_0]

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
               [get_bd_pins axi_sc_0/aclk]

connect_bd_net [get_bd_pins proc_sys_reset_0/interconnect_aresetn] \
               [get_bd_pins axi_sc_0/aresetn]

# Disable low-area mode to avoid warnings if the PS ever issues bursts.
catch {
    set_property CONFIG.ADVANCED_PROPERTIES {__experimental_features__ {disable_low_area_mode 1}} \
        [get_bd_cells axi_sc_0]
}

# ============================================================
# AXI clock converter for external AXI4-Lite input
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_clock_converter axi_clk_conv_ext

catch {
    set_property -dict [list \
        CONFIG.PROTOCOL {AXI4LITE} \
        CONFIG.ADDR_WIDTH {32} \
        CONFIG.DATA_WIDTH {32} \
    ] [get_bd_cells axi_clk_conv_ext]
}

connect_bd_intf_net [get_bd_intf_ports S_AXI_EXT] \
                    [get_bd_intf_pins axi_clk_conv_ext/S_AXI]

connect_bd_intf_net [get_bd_intf_pins axi_clk_conv_ext/M_AXI] \
                    [get_bd_intf_pins axi_sc_0/S00_AXI]

connect_bd_net [get_bd_ports ext_axi_aclk] \
               [get_bd_pins axi_clk_conv_ext/s_axi_aclk]

connect_bd_net [get_bd_ports ext_axi_aresetn] \
               [get_bd_pins axi_clk_conv_ext/s_axi_aresetn]

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
               [get_bd_pins axi_clk_conv_ext/m_axi_aclk]

connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] \
               [get_bd_pins axi_clk_conv_ext/m_axi_aresetn]

# ============================================================
# Connect PS master to SmartConnect
# ============================================================
set ps_master_pin [get_bd_intf_pins -quiet zynq_ultra_ps_e_0/M_AXI_HPM0_FPD]

if {[llength $ps_master_pin] == 0} {
    puts "ERROR: Could not find zynq_ultra_ps_e_0/M_AXI_HPM0_FPD."
    puts "Available PS interface pins:"
    puts [get_bd_intf_pins zynq_ultra_ps_e_0/*]
    error "PS master interface not found. Enable M_AXI_HPM0_FPD or adapt the script."
}

connect_bd_intf_net $ps_master_pin \
                    [get_bd_intf_pins axi_sc_0/S01_AXI]

# ============================================================
# AXI BRAM Controller + BRAM
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl axi_bram_ctrl_rom
create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen blk_mem_rom

catch {
    set_property -dict [list \
        CONFIG.DATA_WIDTH {32} \
        CONFIG.SINGLE_PORT_BRAM {1} \
        CONFIG.PROTOCOL {AXI4LITE} \
    ] [get_bd_cells axi_bram_ctrl_rom]
}

catch {
    set_property -dict [list \
        CONFIG.Memory_Type {Single_Port_RAM} \
        CONFIG.Write_Width_A {32} \
        CONFIG.Read_Width_A {32} \
        CONFIG.Write_Depth_A {16384} \
        CONFIG.Enable_A {Always_Enabled} \
    ] [get_bd_cells blk_mem_rom]
}

connect_bd_intf_net [get_bd_intf_pins axi_sc_0/M00_AXI] \
                    [get_bd_intf_pins axi_bram_ctrl_rom/S_AXI]

connect_bd_intf_net [get_bd_intf_pins axi_bram_ctrl_rom/BRAM_PORTA] \
                    [get_bd_intf_pins blk_mem_rom/BRAM_PORTA]

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
               [get_bd_pins axi_bram_ctrl_rom/s_axi_aclk]

connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] \
               [get_bd_pins axi_bram_ctrl_rom/s_axi_aresetn]

# ============================================================
# AXI FIFO MM-S: Input FIFO
# AXI-MM writes from external core/PS become AXI-Stream output.
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_fifo_mm_s axi_fifo_input

catch {
    set_property -dict [list \
        CONFIG.C_S_AXI_DATA_WIDTH {32} \
        CONFIG.C_S_AXI_ADDR_WIDTH {32} \
        CONFIG.C_USE_TX_DATA {1} \
        CONFIG.C_USE_RX_DATA {0} \
        CONFIG.C_TX_FIFO_DEPTH {1024} \
    ] [get_bd_cells axi_fifo_input]
}

connect_bd_intf_net [get_bd_intf_pins axi_sc_0/M01_AXI] \
                    [get_bd_intf_pins axi_fifo_input/S_AXI]

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
               [get_bd_pins axi_fifo_input/s_axi_aclk]

connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] \
               [get_bd_pins axi_fifo_input/s_axi_aresetn]

# Export input FIFO stream output.
set fifo_in_txd [get_bd_intf_pins -quiet axi_fifo_input/AXI_STR_TXD]
if {[llength $fifo_in_txd] > 0} {
    create_bd_intf_port -mode Master -vlnv xilinx.com:interface:axis_rtl:1.0 M_AXIS_EVENTS

    set_property -dict [list \
        CONFIG.FREQ_HZ $pl_clk_freq \
        CONFIG.CLK_DOMAIN [get_property CONFIG.CLK_DOMAIN [get_bd_pins zynq_ultra_ps_e_0/pl_clk0]] \
        CONFIG.TDATA_NUM_BYTES {4} \
        CONFIG.HAS_TKEEP {0} \
        CONFIG.HAS_TLAST {1} \
    ] [get_bd_intf_ports M_AXIS_EVENTS]

    connect_bd_intf_net $fifo_in_txd [get_bd_intf_ports M_AXIS_EVENTS]
} else {
    puts "WARNING: Could not find axi_fifo_input/AXI_STR_TXD."
    puts "Available input FIFO interface pins:"
    puts [get_bd_intf_pins axi_fifo_input/*]
}

# ============================================================
# AXI FIFO MM-S: Output FIFO
# AXI-Stream input from custom logic becomes AXI-MM readable.
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_fifo_mm_s axi_fifo_output

catch {
    set_property -dict [list \
        CONFIG.C_S_AXI_DATA_WIDTH {32} \
        CONFIG.C_S_AXI_ADDR_WIDTH {32} \
        CONFIG.C_USE_TX_DATA {0} \
        CONFIG.C_USE_RX_DATA {1} \
        CONFIG.C_RX_FIFO_DEPTH {1024} \
    ] [get_bd_cells axi_fifo_output]
}

connect_bd_intf_net [get_bd_intf_pins axi_sc_0/M02_AXI] \
                    [get_bd_intf_pins axi_fifo_output/S_AXI]

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
               [get_bd_pins axi_fifo_output/s_axi_aclk]

connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] \
               [get_bd_pins axi_fifo_output/s_axi_aresetn]

# Export output FIFO stream input.
set fifo_out_rxd [get_bd_intf_pins -quiet axi_fifo_output/AXI_STR_RXD]
if {[llength $fifo_out_rxd] > 0} {
    create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:axis_rtl:1.0 S_AXIS_RESULTS

    set_property -dict [list \
        CONFIG.FREQ_HZ $pl_clk_freq \
        CONFIG.CLK_DOMAIN [get_property CONFIG.CLK_DOMAIN [get_bd_pins zynq_ultra_ps_e_0/pl_clk0]] \
        CONFIG.TDATA_NUM_BYTES {4} \
        CONFIG.HAS_TKEEP {0} \
        CONFIG.HAS_TLAST {1} \
    ] [get_bd_intf_ports S_AXIS_RESULTS]

    connect_bd_intf_net [get_bd_intf_ports S_AXIS_RESULTS] $fifo_out_rxd
} else {
    puts "WARNING: Could not find axi_fifo_output/AXI_STR_RXD."
    puts "Available output FIFO interface pins:"
    puts [get_bd_intf_pins axi_fifo_output/*]
}

# Now that stream ports exist, associate them with exported PL clock.
set_property -dict [list \
    CONFIG.ASSOCIATED_BUSIF {M_AXIS_EVENTS:S_AXIS_RESULTS} \
    CONFIG.ASSOCIATED_RESET {pl_resetn0_out} \
] [get_bd_ports pl_clk0_out]

# ============================================================
# Optional interrupt input to PS
# ============================================================
create_bd_port -dir I irq_from_custom

set ps_irq_pin [get_bd_pins -quiet zynq_ultra_ps_e_0/pl_ps_irq0]
if {[llength $ps_irq_pin] > 0} {
    connect_bd_net [get_bd_ports irq_from_custom] $ps_irq_pin
} else {
    puts "WARNING: Could not find zynq_ultra_ps_e_0/pl_ps_irq0."
    puts "Enable PL-to-PS interrupts if needed."
}

# ============================================================
# Address assignment
# ============================================================
assign_bd_address

# ============================================================
# Validate and save
# ============================================================
validate_bd_design
save_bd_design

puts ""
puts "Created block design: ${design_name}"
puts "External AXI port S_AXI_EXT is AXI4-Lite."
puts "AXI-Stream ports are associated with pl_clk0_out."
puts "PYNQ access uses zynq_ultra_ps_e_0/M_AXI_HPM0_FPD."
puts ""
