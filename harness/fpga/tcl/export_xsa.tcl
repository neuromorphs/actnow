set proj_file "./fpga/vivado/actnow_proj.xpr"
open_project $proj_file

# Not a reset_run: run_impl.tcl has already routed impl_1, so this only adds the
# write_bitstream step on top of the existing route. Running it standalone on an
# unrouted project still works -- Vivado runs the missing steps first. The
# bitstream itself is not the deliverable here, but a fixed (non-reconfigurable)
# platform must carry one, so -include_bit below needs it present.
launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1

open_run impl_1

# The handoff to the software side (Vitis / PetaLinux / PYNQ overlay): the
# hardware definition (address map, IP, the PS configuration from the block
# design) with the bitstream embedded, as one archive.
set xsa_file "./fpga/vivado/actnow.xsa"
write_hw_platform -fixed -include_bit -force $xsa_file
validate_hw_platform $xsa_file
puts "Hardware platform: [file normalize $xsa_file]"

close_project
