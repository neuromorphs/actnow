# KR260 AER-input constraints matching the user's physical wiring
# (SciDVS J11 CAVIAR -> KR260 RPi 40-pin header J21). All LVCMOS33.
# rpi_gpio index / RPi header pin / K26 package pin from the KR260 board XDC.

# ---- AER data bus (inputs to KR260) ----
# AER0  J11.3  -> RPi pin 3   gpio2  AE15
set_property -dict {PACKAGE_PIN AE15 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[0]}]
# AER1  J11.4  -> RPi pin 26  gpio7  AH13
set_property -dict {PACKAGE_PIN AH13 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[1]}]
# AER2  J11.5  -> RPi pin 5   gpio3  AE14
set_property -dict {PACKAGE_PIN AE14 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[2]}]
# AER3  J11.6  -> RPi pin 24  gpio8  AC14
set_property -dict {PACKAGE_PIN AC14 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[3]}]
# AER4  J11.7  -> RPi pin 7   gpio4  AG14
set_property -dict {PACKAGE_PIN AG14 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[4]}]
# AER5  J11.8  -> RPi pin 21  gpio9  AC13
set_property -dict {PACKAGE_PIN AC13 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[5]}]
# AER6  J11.9  -> RPi pin 29  gpio5  AH14
set_property -dict {PACKAGE_PIN AH14 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[6]}]
# AER7  J11.10 -> RPi pin 23  gpio11 AF13
set_property -dict {PACKAGE_PIN AF13 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[7]}]
# AER8/select J11.11 -> RPi pin 31 gpio6 AG13
set_property -dict {PACKAGE_PIN AG13 IOSTANDARD LVCMOS33} [get_ports {aer_data_i[8]}]

# ---- Handshake ----  (REQ/ACK pins assumed RPi 19/32 — CONFIRM)
# REQ  J11.21 -> RPi pin 19  gpio10 AE13   (input to KR260)
set_property -dict {PACKAGE_PIN AE13 IOSTANDARD LVCMOS33} [get_ports {aer_req_n_i}]
# ACK  J11.23 -> RPi pin 32  gpio12 AA13   (output from KR260)
set_property -dict {PACKAGE_PIN AA13 IOSTANDARD LVCMOS33} [get_ports {aer_ack_n_o}]

# ---- Async bus: no timing to close (2-FF synced in RTL) ----
set_false_path -from [get_ports aer_req_n_i]
set_false_path -to   [get_ports aer_ack_n_o]
set_false_path -from [get_ports {aer_data_i[*]}]
