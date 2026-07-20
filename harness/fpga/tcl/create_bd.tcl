# Requirements-only KR260 block design:
#   PS AXI-Lite -> firmware BRAM + control/status GPIO + two DMA register banks
#   core result and raw camera AXI-Streams -> independent S2MM DMAs -> PS DDR

set design_name "actnow_kr260"

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
    delete_bd_objs [get_bd_intf_ports -quiet *]
    delete_bd_objs [get_bd_ports -quiet *]
}

create_bd_design $design_name
current_bd_design $design_name

# ---- Zynq PS with KR260 board preset ----
create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e ps
catch {
    apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e \
        -config {apply_board_preset "1"} [get_bd_cells ps]
}

set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0 {0} \
    CONFIG.PSU__USE__M_AXI_GP1 {0} \
    CONFIG.PSU__USE__M_AXI_GP2 {1} \
    CONFIG.PSU__USE__S_AXI_GP2 {1} \
    CONFIG.PSU__USE__S_AXI_GP3 {1} \
    CONFIG.PSU__SAXIGP2__DATA_WIDTH {64} \
    CONFIG.PSU__SAXIGP3__DATA_WIDTH {64} \
    CONFIG.PSU__USE__IRQ0 {1} \
    CONFIG.PSU__FPGA_PL0_ENABLE {1} \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {100} \
] [get_bd_cells ps]

# ---- reset infrastructure ----
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_ps
create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic rst_inv
set_property -dict [list CONFIG.C_OPERATION {not} CONFIG.C_SIZE {1}] [get_bd_cells rst_inv]

connect_bd_net [get_bd_pins ps/pl_clk0]    [get_bd_pins rst_ps/slowest_sync_clk]
connect_bd_net [get_bd_pins ps/pl_resetn0] [get_bd_pins rst_inv/Op1]
connect_bd_net [get_bd_pins rst_inv/Res]   [get_bd_pins rst_ps/ext_reset_in]

create_bd_port -dir O -type clk pl_clk0_out
connect_bd_net [get_bd_ports pl_clk0_out] [get_bd_pins ps/pl_clk0]

create_bd_port -dir O -type rst pl_resetn0_out
set_property CONFIG.POLARITY ACTIVE_LOW [get_bd_ports pl_resetn0_out]
connect_bd_net [get_bd_ports pl_resetn0_out] [get_bd_pins rst_ps/peripheral_aresetn]

set pl_freq [get_property CONFIG.FREQ_HZ [get_bd_pins ps/pl_clk0]]
if {$pl_freq eq ""} { set pl_freq 100000000 }
set_property CONFIG.FREQ_HZ $pl_freq [get_bd_ports pl_clk0_out]

# ---- result DMA, receive-only/simple mode ----
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma dma_res
set_property -dict [list \
    CONFIG.c_include_sg {0} \
    CONFIG.c_sg_include_stscntrl_strm {0} \
    CONFIG.c_include_mm2s {0} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_include_s2mm_dre {0} \
    CONFIG.c_s2mm_burst_size {16} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_s_axis_s2mm_tdata_width {32} \
    CONFIG.c_addr_width {32} \
] [get_bd_cells dma_res]

create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma dma_raw
set_property -dict [list \
    CONFIG.c_include_sg {0} \
    CONFIG.c_sg_include_stscntrl_strm {0} \
    CONFIG.c_include_mm2s {0} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_include_s2mm_dre {0} \
    CONFIG.c_s2mm_burst_size {16} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_s_axis_s2mm_tdata_width {32} \
    CONFIG.c_addr_width {32} \
] [get_bd_cells dma_raw]

# ---- firmware BRAM: PS writes port A, PL core reads exported port B ----
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl bram_ctrl
set_property -dict [list \
    CONFIG.SINGLE_PORT_BRAM {1} \
    CONFIG.DATA_WIDTH {32} \
] [get_bd_cells bram_ctrl]

apply_bd_automation -rule xilinx.com:bd_rule:bram_cntlr \
    -config {BRAM "Auto"} [get_bd_intf_pins bram_ctrl/BRAM_PORTA]

set bram_gen [get_bd_cells -quiet -filter {VLNV =~ "*blk_mem_gen*"}]
if {[llength $bram_gen] == 0} {
    error "BRAM generator was not created"
}
set_property -dict [list \
    CONFIG.Memory_Type {True_Dual_Port_RAM} \
    CONFIG.Write_Width_A {32} \
    CONFIG.Read_Width_A {32} \
    CONFIG.Write_Depth_A {8192} \
    CONFIG.Write_Width_B {32} \
    CONFIG.Read_Width_B {32} \
    CONFIG.Enable_B {Use_ENB_Pin} \
    CONFIG.Use_RSTB_Pin {true} \
    CONFIG.Register_PortB_Output_of_Memory_Primitives {false} \
] $bram_gen
make_bd_intf_pins_external -name BRAM_PORTB [get_bd_intf_pins $bram_gen/BRAM_PORTB]

# ---- GPIO: one control register, twelve status counters in six dual GPIOs ----
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio gpio_ctrl
set_property -dict [list \
    CONFIG.C_ALL_OUTPUTS {1} \
    CONFIG.C_GPIO_WIDTH {32} \
] [get_bd_cells gpio_ctrl]

foreach g {gpio_s0 gpio_s1 gpio_s2 gpio_s3 gpio_s4 gpio_s5} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio $g
    set_property -dict [list \
        CONFIG.C_IS_DUAL {1} \
        CONFIG.C_ALL_INPUTS {1} \
        CONFIG.C_ALL_INPUTS_2 {1} \
        CONFIG.C_GPIO_WIDTH {32} \
        CONFIG.C_GPIO2_WIDTH {32} \
    ] [get_bd_cells $g]
}

# ---- AXI-Lite control path ----
foreach s {dma_res/S_AXI_LITE dma_raw/S_AXI_LITE bram_ctrl/S_AXI gpio_ctrl/S_AXI \
           gpio_s0/S_AXI gpio_s1/S_AXI gpio_s2/S_AXI gpio_s3/S_AXI gpio_s4/S_AXI \
           gpio_s5/S_AXI} {
    apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
        -config [list Master {/ps/M_AXI_HPM0_LPD} Clk {Auto}] [get_bd_intf_pins $s]
}

# ---- DMA data path into PS DDR ----
connect_bd_intf_net [get_bd_intf_pins dma_res/M_AXI_S2MM] [get_bd_intf_pins ps/S_AXI_HP0_FPD]
connect_bd_intf_net [get_bd_intf_pins dma_raw/M_AXI_S2MM] [get_bd_intf_pins ps/S_AXI_HP1_FPD]
catch { connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins ps/saxihp0_fpd_aclk] }
catch { connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins ps/saxihp1_fpd_aclk] }
catch { connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins dma_res/m_axi_s2mm_aclk] }
catch { connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins dma_raw/m_axi_s2mm_aclk] }

# ---- result stream in from fpga_top/actnow_pl ----
make_bd_intf_pins_external -name s_axis_res [get_bd_intf_pins dma_res/S_AXIS_S2MM]
set_property -dict [list CONFIG.FREQ_HZ $pl_freq] [get_bd_intf_ports s_axis_res]
set_property -dict [list CONFIG.FREQ_HZ $pl_freq CONFIG.ASSOCIATED_BUSIF {s_axis_res}] \
    [get_bd_ports pl_clk0_out]

# ---- raw camera stream in from fpga_top/actnow_pl ----
make_bd_intf_pins_external -name s_axis_raw [get_bd_intf_pins dma_raw/S_AXIS_S2MM]
set_property -dict [list CONFIG.FREQ_HZ $pl_freq] [get_bd_intf_ports s_axis_raw]
set_property -dict [list CONFIG.ASSOCIATED_BUSIF {s_axis_res:s_axis_raw}] \
    [get_bd_ports pl_clk0_out]

# ---- interrupts for PYNQ DMA completion ----
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat irq_concat
set_property CONFIG.NUM_PORTS {2} [get_bd_cells irq_concat]
connect_bd_net [get_bd_pins dma_res/s2mm_introut] [get_bd_pins irq_concat/In0]
connect_bd_net [get_bd_pins dma_raw/s2mm_introut] [get_bd_pins irq_concat/In1]
connect_bd_net [get_bd_pins irq_concat/dout] [get_bd_pins ps/pl_ps_irq0]

# ---- exported GPIO vectors ----
create_bd_port -dir O -from 31 -to 0 gpio_ctrl_out
connect_bd_net [get_bd_ports gpio_ctrl_out] [get_bd_pins gpio_ctrl/gpio_io_o]

for {set i 0} {$i < 12} {incr i} {
    create_bd_port -dir I -from 31 -to 0 gpio_stat${i}_in
}

connect_bd_net [get_bd_ports gpio_stat0_in] [get_bd_pins gpio_s0/gpio_io_i]
connect_bd_net [get_bd_ports gpio_stat1_in] [get_bd_pins gpio_s0/gpio2_io_i]
connect_bd_net [get_bd_ports gpio_stat2_in] [get_bd_pins gpio_s1/gpio_io_i]
connect_bd_net [get_bd_ports gpio_stat3_in] [get_bd_pins gpio_s1/gpio2_io_i]
connect_bd_net [get_bd_ports gpio_stat4_in] [get_bd_pins gpio_s2/gpio_io_i]
connect_bd_net [get_bd_ports gpio_stat5_in] [get_bd_pins gpio_s2/gpio2_io_i]
connect_bd_net [get_bd_ports gpio_stat6_in] [get_bd_pins gpio_s3/gpio_io_i]
connect_bd_net [get_bd_ports gpio_stat7_in] [get_bd_pins gpio_s3/gpio2_io_i]
connect_bd_net [get_bd_ports gpio_stat8_in] [get_bd_pins gpio_s4/gpio_io_i]
connect_bd_net [get_bd_ports gpio_stat9_in] [get_bd_pins gpio_s4/gpio2_io_i]
connect_bd_net [get_bd_ports gpio_stat10_in] [get_bd_pins gpio_s5/gpio_io_i]
connect_bd_net [get_bd_ports gpio_stat11_in] [get_bd_pins gpio_s5/gpio2_io_i]

assign_bd_address
assign_bd_address -offset 0x82000000 -range 0x00008000 \
    -target_address_space [get_bd_addr_spaces ps/Data] \
    [get_bd_addr_segs bram_ctrl/S_AXI/Mem0] -force
regenerate_bd_layout
validate_bd_design
save_bd_design
