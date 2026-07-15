#!/usr/bin/env python3
"""KR260/PYNQ side of the requirements-only ActNow harness.

Loads the overlay, writes firmware into the ROM BRAM, pulses reset_ext, drains
the result DMA, and forwards completed packets to a host over UDP.
"""

import argparse
import json
import os
import socket
import struct
import threading
import time

from pynq import Overlay, allocate

MAGIC = b"ACT1"
HDR = struct.Struct("<4sIH")
PKT_WORDS = 256
CONTROL_PORT = 3335
CTRL_RESET = 1 << 0
CTRL_PAUSE = 1 << 1


def load_firmware(overlay, mem_path):
    with open(mem_path) as f:
        words = [int(line.strip(), 2) for line in f if line.strip()]

    bram = overlay.bram_ctrl
    for i, word in enumerate(words):
        bram.write(i * 4, word)

    if words and bram.read(0) != words[0]:
        raise RuntimeError("firmware BRAM readback mismatch")
    return len(words)


def write_control(overlay, value):
    overlay.gpio_ctrl.channel1.write(value, 0xFFFFFFFF)


def reset_core(overlay, control=0):
    before = overlay.gpio_s4.channel2.read()
    gpio = overlay.gpio_ctrl.channel1
    gpio.write(control, 0xFFFFFFFF)
    time.sleep(0.01)
    gpio.write(control | CTRL_RESET, 0xFFFFFFFF)
    time.sleep(0.01)
    gpio.write(control, 0xFFFFFFFF)
    for _ in range(100):
        if overlay.gpio_s4.channel2.read() != before:
            return
        time.sleep(0.01)
    raise RuntimeError("core did not acknowledge reset")


def pause_and_drain(overlay):
    write_control(overlay, CTRL_PAUSE)
    previous = None
    stable = 0
    for _ in range(200):
        # The PL keeps accepting camera events while paused, then drains and
        # discards them so the AER receiver is never backpressured. Therefore
        # core_push_count may continue changing indefinitely on an active DVS.
        # Only the core result path must quiesce before its warm reset.
        current = overlay.gpio_s3.channel2.read()
        if current == previous:
            stable += 1
            if stable >= 10:
                time.sleep(0.02)
                return
        else:
            previous = current
            stable = 0
        time.sleep(0.01)
    raise RuntimeError("core output did not quiesce")


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


class Runtime:
    def __init__(self, overlay, firmware):
        self.overlay = overlay
        self.firmware = firmware
        self.running = True

    def stop_dma(self):
        try:
            self.overlay.dma_res.recvchannel.stop()
        except Exception:
            pass

    def command(self, request):
        command = request.get("command")
        if command == "status":
            return {"ok": True, "firmware": self.firmware,
                    "counters": counters(self.overlay)}
        if command == "reset":
            pause_and_drain(self.overlay)
            try:
                reset_core(self.overlay, CTRL_PAUSE)
            finally:
                write_control(self.overlay, 0)
            return {"ok": True, "counters": counters(self.overlay)}
        if command == "reload":
            path = request.get("path", "")
            if not path or not os.path.isfile(path):
                raise ValueError("reload path is not a file")
            pause_and_drain(self.overlay)
            try:
                # The current application executes from SRAM. Write the full
                # ROM only after ingress is stopped and output has drained,
                # then reset at a clean io_* transaction boundary.
                n = load_firmware(self.overlay, path)
                reset_core(self.overlay, CTRL_PAUSE)
                self.firmware = path
            finally:
                write_control(self.overlay, 0)
            return {"ok": True, "words": n, "counters": counters(self.overlay)}
        if command == "shutdown":
            self.running = False
            self.stop_dma()
            return {"ok": True}
        raise ValueError(f"unknown command: {command}")


def control_server(runtime, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(4)
    listener.settimeout(0.5)
    print(f"control: 127.0.0.1:{port}", flush=True)
    try:
        while runtime.running:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            with conn:
                stream = conn.makefile("rwb")
                for line in stream:
                    try:
                        reply = runtime.command(json.loads(line))
                    except Exception as exc:
                        reply = {"ok": False, "error": str(exc)}
                    stream.write(json.dumps(reply).encode() + b"\n")
                    stream.flush()
                    if not runtime.running:
                        break
    finally:
        listener.close()


def dma_to_udp(runtime, host, port, stats_s):
    overlay = runtime.overlay
    dst = (host, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    buf = allocate(shape=(PKT_WORDS,), dtype="u4")
    seq = 0
    t_stat = time.time()
    try:
        while runtime.running:
            try:
                overlay.dma_res.recvchannel.transfer(buf)
                overlay.dma_res.recvchannel.wait()
            except Exception as exc:
                if runtime.running:
                    print(f"DMA transfer failed, recovering: {exc}", flush=True)
                    if not overlay.dma_res.recvchannel.running:
                        overlay.dma_res.recvchannel.start()
                    time.sleep(0.1)
                continue
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
    ap.add_argument("--control-port", type=int, default=CONTROL_PORT)
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

    runtime = Runtime(overlay, args.firmware)
    control = threading.Thread(target=control_server,
                               args=(runtime, args.control_port), daemon=True)
    control.start()
    print(f"streaming result words to {args.host}:{args.port}", flush=True)
    dma_to_udp(runtime, args.host, args.port, args.stats_s)


if __name__ == "__main__":
    main()
