set proj_file "./fpga/vivado/actnow_proj.xpr"
open_project $proj_file

file mkdir ./fpga/vivado/reports

reset_run synth_1
set_property strategy Flow_PerfOptimized_high [get_runs synth_1]

launch_runs synth_1 -jobs 8
wait_on_run synth_1

open_run synth_1
report_timing_summary -file ./fpga/vivado/reports/post_synth_timing_summary.rpt
report_utilization    -file ./fpga/vivado/reports/post_synth_utilization.rpt

close_project
