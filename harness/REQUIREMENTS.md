This is what the overall system should look like:

# General setup
- The processor boots from an addressable memory ("ROM") through the generated
  core's rom_* interface
- The DVS camera is connected to the PL of the KR260 through the RP headers.
- In the PL, the data should be received like in the kr260_aer_interface and put
  into a FIFO in the following format:
    - [ padding[0:0], x_pos[6:0], y_pos[6:0], timestep[15:0], p[0:0] ] -> 32
- The FIFO feeds into the fifo_push_* interface of the generated core Verilog
  module (which itself is a FIFO)
- The processor starts processing after the core-internal FIFO runs full
- The processor writes its data to the interface io_* (stream-like, FIFO)
    - It will be connected to a FIFO in the block design

# Software setup
- There should be, similar to the kr260_aer_interface, two parts to the software:
    - An FPGA-side driver/server that only abstracts the Overlay and talks to
      the block design elements in the .xsa file. It should stream the output
      data via UDP to the
    - A host-side client that pushes firmware updates to the processor running
      on the FPGA, and renders the output (also similar to the
      kr260_aer_interface)
- The reading from the output FIFO (described below) should be done using DMA
  reads, as fast as possible.

# Hardware setup

- core verilog module gets generated (gen/ folder)
- fpga_top.v wraps the block design and the core module
- the interface between the two is:
    - a read interface for the program memory ("ROM"), called rom_*. It should
      be writable by the PS of the KR260 board to load new programs into the
      processor. Probably should be implemented using a memory mapped BRAM, and
      should be wrapped in software layer by a simple "write_firmware" method
    - a stream write interface for the fifo_push_* for pushing the 32-bit AER
      words from the FIFO connected to the AER interface to the internal input
      FIFO of the core
    - a stream read interface, leading into an external FIFO (in the block
      design) that is accessible by the PS through DMA (for fast loading)

- the current create_bd.tcl is the one from kr260_aer_interface. In line with
  what was discussed above, it should be expanded with:
    - BRAM + controller for the ROM (flashable with firmware from Pynq)
    - Connection from AER FIFO to internal core FIFO (with "glue logic" if
      required)
    - Output FIFO that can be read via DMA from Pynq for quickly moving data out
      of the PL to the PS and eventually to the host via UDP packets (ethernet)

# General info
- The FPGA is connected and reachable over kria.local
- The default user is ubuntu, which has passwordless sudo
- Any Overlay or Pynq code needs to be called with sudo
