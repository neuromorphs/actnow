set proj_file "./fpga/vivado/actnow_proj.xpr"
open_project $proj_file

file mkdir ./fpga/vivado/reports

reset_run impl_1

set_property strategy Performance_ExplorePostRoutePhysOpt [get_runs impl_1]

launch_runs impl_1 -jobs 8
wait_on_run impl_1

open_run impl_1
report_timing_summary -file ./fpga/vivado/reports/post_route_timing_summary.rpt
report_route_status   -file ./fpga/vivado/reports/post_route_status.rpt
report_utilization    -file ./fpga/vivado/reports/post_route_utilization.rpt

close_project
