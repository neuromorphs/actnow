set proj_name "actnow_proj"
set proj_dir  "./fpga/vivado"
set top_name  "fpga_top"
set part_name "xck26-sfvc784-2LV-c"

file mkdir $proj_dir

create_project $proj_name $proj_dir -part $part_name -force

add_files [glob ./static/*.v]
add_files [glob ./gen/*.v]

set_property top $top_name [current_fileset]
update_compile_order -fileset sources_1

close_project
