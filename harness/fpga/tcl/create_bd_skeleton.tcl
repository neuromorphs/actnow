# ============================================================
# Vivado Block Design Tcl -- MINIMAL SKELETON
# KR260 / ZynqMP: MPSoC + reset only, exporting clock/reset.
#
# Deliberately NOT the full harness (see create_bd.tcl for the BRAM/FIFO/
# SmartConnect design). This skeleton exists so fpga_top.v can instantiate a
# real MPSoC-driven clock/reset domain around the converted `core` while its
# data/peripheral interfaces are tied inactive. The AXI peripherals that will
# eventually feed core's raw rom_*/fifo_push/io_* groups get added later.
# ============================================================

set design_name "actnow_skeleton_kr260"

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
    delete_bd_objs [get_bd_ports -quiet *]
}

# ------------------------------------------------------------
# Create new block design
# ------------------------------------------------------------
create_bd_design $design_name
current_bd_design $design_name

# ============================================================
# Zynq UltraScale+ MPSoC (the "MPSoC IP block")
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e zynq_ultra_ps_e_0

catch {
    apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e \
        -config {apply_board_preset "1"} \
        [get_bd_cells zynq_ultra_ps_e_0]
}

# No AXI masters needed for the skeleton -- keep only the PL clock enabled.
catch {
    set_property -dict [list \
        CONFIG.PSU__USE__M_AXI_GP0 {0} \
        CONFIG.PSU__USE__M_AXI_GP1 {0} \
        CONFIG.PSU__USE__M_AXI_GP2 {0} \
        CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {100} \
    ] [get_bd_cells zynq_ultra_ps_e_0]
}

# ============================================================
# Reset infrastructure (synchronised to pl_clk0)
# ============================================================
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset proc_sys_reset_0

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
               [get_bd_pins proc_sys_reset_0/slowest_sync_clk]

# pl_resetn0 is active-low; proc_sys_reset's ext_reset_in is active-high, so invert.
create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic rst_inv_0
set_property -dict [list \
    CONFIG.C_OPERATION {not} \
    CONFIG.C_SIZE {1} \
] [get_bd_cells rst_inv_0]

connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_resetn0] \
               [get_bd_pins rst_inv_0/Op1]
connect_bd_net [get_bd_pins rst_inv_0/Res] \
               [get_bd_pins proc_sys_reset_0/ext_reset_in]

# Defensive: connect any enabled PS master AXI clocks to pl_clk0 so validate
# doesn't complain even if the board preset left a master enabled.
foreach clkpin {maxihpm0_fpd_aclk maxihpm1_fpd_aclk maxihpm0_lpd_aclk} {
    set p [get_bd_pins -quiet zynq_ultra_ps_e_0/$clkpin]
    if {[llength $p] > 0} {
        connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] $p
    }
}

# ============================================================
# Export clock + reset as BD output ports (consumed by fpga_top.v)
# ============================================================
create_bd_port -dir O -type clk pl_clk0_out
set pl_clk_freq [get_property CONFIG.FREQ_HZ [get_bd_pins zynq_ultra_ps_e_0/pl_clk0]]
if {$pl_clk_freq eq ""} { set pl_clk_freq 100000000 }
set_property CONFIG.FREQ_HZ $pl_clk_freq [get_bd_ports pl_clk0_out]
connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] [get_bd_ports pl_clk0_out]

# Active-high, synchronised reset (peripheral_reset) so fpga_top can drive the
# converted core's active-high `reset` directly, no inversion in RTL.
create_bd_port -dir O -type rst pl_reset0_out
set_property CONFIG.POLARITY ACTIVE_HIGH [get_bd_ports pl_reset0_out]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_reset] \
               [get_bd_ports pl_reset0_out]

# ============================================================
# Validate + save (no slaves -> assign_bd_address is a no-op)
# ============================================================
assign_bd_address
validate_bd_design
save_bd_design

puts ""
puts "Created SKELETON block design: ${design_name}"
puts "Exports: pl_clk0_out (clk), pl_reset0_out (active-high sync reset)."
puts "No AXI peripherals -- core's data interfaces are tied inactive in fpga_top.v."
puts ""
