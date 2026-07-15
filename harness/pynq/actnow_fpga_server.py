#!/usr/bin/env python3
"""KR260/PYNQ side of the requirements-only ActNow harness.

Loads the overlay, writes firmware into the ROM BRAM, pulses reset_ext, drains
the result DMA, and forwards completed packets to a host over UDP.
"""

import argparse
import socket
import struct
import time

from pynq import Overlay, allocate

MAGIC = b"ACT1"
HDR = struct.Struct("<4sIH")
PKT_WORDS = 256


def load_firmware(overlay, mem_path):
    with open(mem_path) as f:
        words = [int(line.strip(), 2) for line in f if line.strip()]

    bram = overlay.bram_ctrl
    for i, word in enumerate(words):
        bram.write(i * 4, word)

    if words and bram.read(0) != words[0]:
        raise RuntimeError("firmware BRAM readback mismatch")
    return len(words)


def reset_core(overlay):
    gpio = overlay.gpio_ctrl.channel1
    gpio.write(0, 0xFFFFFFFF)
    time.sleep(0.01)
    gpio.write(1, 0xFFFFFFFF)
    time.sleep(0.01)
    gpio.write(0, 0xFFFFFFFF)


def counters(overlay):
    return {
        "req": overlay.gpio_s0.channel1.read(),
        "words": overlay.gpio_s0.channel2.read(),
        "evt": overlay.gpio_s1.channel1.read(),
        "last": overlay.gpio_s1.channel2.read(),
        "drop": overlay.gpio_s2.channel1.read(),
        "push": overlay.gpio_s2.channel2.read(),
        "fetch": overlay.gpio_s3.channel1.read(),
        "results": overlay.gpio_s3.channel2.read(),
        "rd_err": overlay.gpio_s4.channel1.read(),
        "resets": overlay.gpio_s4.channel2.read(),
    }


def dma_to_udp(overlay, host, port, stats_s):
    dst = (host, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    buf = allocate(shape=(PKT_WORDS,), dtype="u4")
    seq = 0
    t_stat = time.time()
    try:
        while True:
            overlay.dma_res.recvchannel.transfer(buf)
            overlay.dma_res.recvchannel.wait()
            n_bytes = getattr(overlay.dma_res.recvchannel, "transferred", 0)
            n_words = n_bytes // 4 if n_bytes else PKT_WORDS
            body = struct.pack("<%dI" % n_words, *[int(w) for w in buf[:n_words]])
            sock.sendto(HDR.pack(MAGIC, seq, n_words) + body, dst)
            seq = (seq + 1) & 0xFFFFFFFF

            now = time.time()
            if now - t_stat >= stats_s:
                t_stat = now
                c = counters(overlay)
                print("evt={evt} push={push} results={results} drop={drop} "
                      "fetch={fetch} rd_err={rd_err}".format(**c), flush=True)
    finally:
        buf.freebuffer()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3334)
    ap.add_argument("--xsa", default="actnow.xsa")
    ap.add_argument("--firmware", default="rom.mem")
    ap.add_argument("--stats-s", type=float, default=2.0)
    args = ap.parse_args()

    print(f"loading overlay: {args.xsa}", flush=True)
    overlay = Overlay(args.xsa)

    n = load_firmware(overlay, args.firmware)
    print(f"firmware: wrote {n} words", flush=True)

    reset_core(overlay)
    time.sleep(0.2)
    c = counters(overlay)
    if c["fetch"] == 0:
        print("WARNING: core has not fetched from ROM after reset", flush=True)
    else:
        print(f"core booted: fetch={c['fetch']}", flush=True)

    print(f"streaming result words to {args.host}:{args.port}", flush=True)
    dma_to_udp(overlay, args.host, args.port, args.stats_s)


if __name__ == "__main__":
    main()
