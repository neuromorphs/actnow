set proj_name "actnow_proj"
set tcl_dir   [file dirname [file normalize [info script]]]
set harness_dir [file normalize [file join $tcl_dir "../.."]]
set proj_dir  [file join $harness_dir "fpga/vivado"]
set top_name  "fpga_top"
set part_name "xck26-sfvc784-2LV-c"

file mkdir $proj_dir

create_project $proj_name $proj_dir -part $part_name -force

add_files [glob [file join $harness_dir "static/*.v"]]
add_files [glob [file join $harness_dir "gen/*.v"]]

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
