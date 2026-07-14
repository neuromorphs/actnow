#!/usr/bin/env python3
"""Runs on the KR260 (PYNQ). Brings the ActNow DVS harness up and streams both
event streams to a host over UDP.

    stream A (:3333)  raw camera events, straight off the AER receiver
    stream B (:3334)  what the ActNow core made of the same events

Sequence:
  1. load the overlay from the .xsa (bitstream + hwh, so PYNQ resolves the DMAs,
     the BRAM controller and the GPIOs by name);
  2. write the firmware image (software/build/rom.mem) into the firmware BRAM;
  3. pulse the core's reset_ext -- this is the ONLY way the core ever starts:
     soc.act blocks on reset_ext before executing anything, so until this pulse
     the core is idle, fetches nothing, and produces nothing. The same pulse is
     also how a firmware change takes effect -- write the BRAM, pulse, done, no
     bitstream rebuild;
  4. set the decimation for the core's stream (the core is ~100x slower than the
     sensor -- see BD_AER_BRAINSTORM.md D6);
  5. run one DMA-receive loop per stream, forwarding each completed packet as a
     UDP datagram in kr260_aer_interface's existing format, so its
     host/aer_udp_viewer.py works unchanged on either port.

Datagram (identical to kr260_aer_send.py):
    header "<4sIH" = magic b"AER1", seq (uint32), n (uint16)
    then n x "<BBB" = (x, y, pol)

The PL word carries a timestamp on top of that ({ts[16:0], pol, y[6:0], x[6:0]}),
which the firmware can use for temporal filtering; it is dropped here because the
existing viewer does not want it. See TODO.md if you want it on the wire.

NOTE: none of this has run on hardware yet -- no board, no camera at the time of
writing. HW_BRINGUP.md lists what to check, in order.
"""

import argparse
import socket
import struct
import threading
import time

from pynq import Overlay, allocate

HDR = struct.Struct("<4sIH")
MAGIC = b"AER1"

# One PL event word: {ts[16:0], pol, y[6:0], x[6:0]} -- see static/evt_pack.v.
def decode(word):
    x = word & 0x7F
    y = (word >> 7) & 0x7F
    pol = (word >> 14) & 0x1
    ts = (word >> 15) & 0x1FFFF
    return x, y, pol, ts


# PKT_WORDS in static/axis_pack_fifo.v: the PL never sends a longer packet, so a
# buffer this size can always take a whole one.
PKT_WORDS = 256


def load_firmware(ol, mem_path):
    """Write bootloader+program into the firmware BRAM (AXI-BRAM-Ctrl, port A).

    software/build/rom.mem is one 32-bit *binary* word per line, which is what
    the simulator's $readmemb wants; here we just parse it back to ints.
    """
    with open(mem_path) as f:
        words = [int(line.strip(), 2) for line in f if line.strip()]

    bram = ol.bram_ctrl          # pynq MMIO over the BRAM controller's window
    for i, w in enumerate(words):
        bram.write(i * 4, w)

    # Read one word back: an all-zero readback means the PS is not actually
    # talking to the memory the core reads, and the core will never boot.
    if bram.read(0) != words[0]:
        raise RuntimeError("firmware BRAM readback mismatch -- check the address map")
    return len(words)


def reset_core(ol):
    """One 0->1->0 pulse on gpio_ctrl bit 0 -> one send on the core's reset_ext
    channel -> the core (re)boots and fetches its reset vector from the BRAM.

    Mandatory, not optional: the core does not self-boot (see the module docstring),
    so without this it never executes an instruction."""
    ch = ol.gpio_ctrl.channel1
    ch.write(0, 0xFFFFFFFF)
    time.sleep(0.01)
    ch.write(1, 0xFFFFFFFF)
    time.sleep(0.01)
    ch.write(0, 0xFFFFFFFF)


def set_decim(ol, core_n, raw_n=0):
    """decim register: [15:0] = core stream, [31:16] = raw stream. 0/1 = keep all."""
    ol.gpio_ctrl.channel2.write(((raw_n & 0xFFFF) << 16) | (core_n & 0xFFFF), 0xFFFFFFFF)


def counters(ol):
    return {
        "req": ol.gpio_s0.channel1.read(),
        "evt": ol.gpio_s0.channel2.read(),
        "core_drop": ol.gpio_s1.channel1.read(),
        "results": ol.gpio_s1.channel2.read(),
        "fetch": ol.gpio_s2.channel1.read(),
        "last_event": ol.gpio_s2.channel2.read(),
    }


def stream_loop(dma, dst, sock, stop, name):
    """One DMA -> UDP pump. The PL closes a packet when a burst ends (tlast), so
    a transfer completes as soon as the events stop coming -- we never sit on a
    half-filled buffer waiting for a quiet scene to produce more."""
    buf = allocate(shape=(PKT_WORDS,), dtype="u4")
    seq = 0
    try:
        while not stop.is_set():
            dma.recvchannel.transfer(buf)
            dma.recvchannel.wait()
            n_bytes = dma.recvchannel.transferred
            n = n_bytes // 4 if n_bytes else PKT_WORDS

            body = bytearray()
            for w in buf[:n]:
                x, y, pol, _ts = decode(int(w))
                body += struct.pack("<BBB", x, y, pol)

            sock.sendto(HDR.pack(MAGIC, seq, n) + bytes(body), dst)
            seq += 1
    finally:
        buf.freebuffer()
        print(f"[{name}] stopped after {seq} packets")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="host to send both streams to")
    ap.add_argument("--port-raw", type=int, default=3333)
    ap.add_argument("--port-core", type=int, default=3334)
    ap.add_argument("--xsa", default="actnow.xsa")
    ap.add_argument("--firmware", default="rom.mem",
                    help="software/build/rom.mem (bootloader + program)")
    ap.add_argument("--decim", type=int, default=8,
                    help="feed the core every Nth event (it is ~100x slower than the sensor)")
    ap.add_argument("--stats-s", type=float, default=2.0)
    args = ap.parse_args()

    print(f"loading overlay: {args.xsa}")
    ol = Overlay(args.xsa)

    n = load_firmware(ol, args.firmware)
    print(f"firmware: {n} words written to the BRAM")

    set_decim(ol, core_n=args.decim, raw_n=0)
    reset_core(ol)
    print(f"core reset; decimation: core=every {args.decim}th event, raw=all")

    time.sleep(0.2)
    c = counters(ol)
    if c["fetch"] == 0:
        print("WARNING: the core has fetched nothing from the BRAM -- it never booted. "
              "The reset_ext pulse is what starts it; check reset_count. See HW_BRINGUP.md step 3.")
    else:
        print(f"core is fetching ({c['fetch']} ROM reads) -- it booted")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stop = threading.Event()
    threads = [
        threading.Thread(target=stream_loop, args=(ol.dma_raw, (args.host, args.port_raw),
                                                   sock, stop, "raw"), daemon=True),
        threading.Thread(target=stream_loop, args=(ol.dma_res, (args.host, args.port_core),
                                                   sock, stop, "core"), daemon=True),
    ]
    for t in threads:
        t.start()

    print(f"streaming: raw -> {args.host}:{args.port_raw}, core -> {args.host}:{args.port_core}")
    try:
        while True:
            time.sleep(args.stats_s)
            c = counters(ol)
            print("evt={evt} results={results} core_drop={core_drop} "
                  "fetch={fetch} last=0x{last_event:08x}".format(**c))
    except KeyboardInterrupt:
        stop.set()
        print("\nstopping")


if __name__ == "__main__":
    main()
