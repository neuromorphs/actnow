set proj_name "actnow_proj"
set tcl_dir   [file dirname [file normalize [info script]]]
set harness_dir [file normalize [file join $tcl_dir "../.."]]
set proj_dir  [file join $harness_dir "fpga/vivado"]
set top_name  "fpga_top"
set part_name "xck26-sfvc784-2LV-c"

file mkdir $proj_dir

create_project $proj_name $proj_dir -part $part_name -force

# Hand-written PL RTL (static/) + the converted core (gen/). aer_rx_simple.sv is
# SystemVerilog -- unchanged from kr260_aer_interface apart from its event tap --
# and has to be read as such; everything else is plain Verilog.
add_files [glob [file join $harness_dir "static/*.v"]]
add_files [glob [file join $harness_dir "gen/*.v"]]
set sv_files [glob -nocomplain [file join $harness_dir "static/*.sv"]]
if {[llength $sv_files] > 0} {
    add_files $sv_files
    set_property file_type SystemVerilog [get_files $sv_files]
}

# gen/func.v is a bare set of functions that every generated module textually
# `include`s -- it must not be elaborated as a compilation unit of its own.
set func_v [file join $harness_dir "gen/func.v"]
set_property used_in_synthesis false [get_files $func_v]
set_property used_in_simulation false [get_files $func_v]

# AER pin constraints, taken unchanged from kr260_aer_interface -- they encode the
# physical wiring, including the AER3<->AER5 correction that build had to make.
set xdc_files [glob -nocomplain [file join $harness_dir "fpga/xdc/*.xdc"]]
if {[llength $xdc_files] > 0} {
    add_files -fileset constrs_1 -norecurse $xdc_files
}

source [file join $tcl_dir "create_bd.tcl"]

set bd_files [get_files -quiet "*${design_name}.bd"]
if {[llength $bd_files] == 0} {
    error "Block design ${design_name}.bd was not created"
}

generate_target all $bd_files
set wrapper_files [make_wrapper -files $bd_files -top]
add_files -norecurse $wrapper_files

set_property top $top_name [current_fileset]
update_compile_order -fileset sources_1

close_project
