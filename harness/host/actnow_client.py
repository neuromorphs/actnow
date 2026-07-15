#!/usr/bin/env python3
"""Host-side client for the requirements-only ActNow KR260 harness.

Copies the FPGA-side server, firmware, and overlay to the KR260 over SSH/SCP,
starts the server with sudo, then renders result event words received over UDP.
"""

import argparse
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

MAGIC = b"ACT1"
HDR = struct.Struct("<4sIH")
SX, SY = 126, 112


def decode_word(word):
    return {
        "x": (word >> 24) & 0x7F,
        "y": (word >> 17) & 0x7F,
        "ts": (word >> 1) & 0xFFFF,
        "p": word & 0x1,
    }


def run_checked(cmd):
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def deploy_and_start(args):
    here = Path(__file__).resolve().parents[1]
    server = here / "pynq" / "actnow_fpga_server.py"
    remote = f"{args.user}@{args.kria}"
    remote_dir = args.remote_dir.rstrip("/")

    run_checked(["ssh", remote, "mkdir", "-p", remote_dir])
    run_checked(["scp", str(server), str(args.firmware), str(args.xsa), f"{remote}:{remote_dir}/"])

    cmd = (
        f"cd {remote_dir} && sudo bash -lc "
        f"'source /etc/profile.d/pynq_venv.sh && python3 actnow_fpga_server.py "
        f"--host {args.listen_host} --port {args.port} "
        f"--xsa {Path(args.xsa).name} --firmware {Path(args.firmware).name}'"
    )
    return subprocess.Popen(["ssh", remote, cmd])


def render(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.2)

    cv2 = None
    np = None
    if not args.headless:
        try:
            import cv2 as _cv2
            import numpy as _np
            cv2, np = _cv2, _np
            cv2.namedWindow("ActNow result stream", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("ActNow result stream", SY * args.scale, SX * args.scale)
            acc = np.zeros((SX, SY, 3), np.float32)
        except Exception as exc:
            print(f"viewer unavailable, falling back to headless: {exc}", flush=True)
            cv2 = None
            acc = None
    else:
        acc = None

    total = 0
    dropped = 0
    expect_seq = None
    last_t = time.time()
    last_total = 0

    print(f"listening UDP :{args.port}", flush=True)
    while True:
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            if cv2 is not None:
                cv2.waitKey(1)
            continue

        if len(data) < HDR.size or data[:4] != MAGIC:
            continue

        _magic, seq, n = HDR.unpack_from(data)
        if expect_seq is not None and seq != expect_seq:
            dropped += (seq - expect_seq) & 0xFFFFFFFF
        expect_seq = (seq + 1) & 0xFFFFFFFF

        body = data[HDR.size:]
        m = min(n, len(body) // 4)
        words = struct.unpack_from("<%dI" % m, body, 0) if m else ()
        total += m

        if cv2 is not None:
            acc *= args.decay
            for word in words:
                ev = decode_word(word)
                x, y, p = ev["x"], ev["y"], ev["p"]
                if 0 <= x < SX and 0 <= y < SY:
                    row = SX - 1 - x
                    col = y
                    acc[row, col, 1 if p else 2] = 1.0
            img = (np.clip(acc, 0, 1) * 255).astype(np.uint8)
            cv2.imshow("ActNow result stream", img)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        now = time.time()
        if now - last_t >= 1.0:
            rate = (total - last_total) / (now - last_t)
            print(f"\r{rate:9.0f} words/s total={total} dropped_pkts={dropped}", end="", flush=True)
            last_t = now
            last_total = total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kria", default="kria.local")
    ap.add_argument("--user", default="ubuntu")
    ap.add_argument("--remote-dir", default="/tmp/actnow_harness")
    ap.add_argument("--listen-host", required=True, help="host/IP the KR260 should send UDP to")
    ap.add_argument("--port", type=int, default=3334)
    ap.add_argument("--xsa", required=True)
    ap.add_argument("--firmware", required=True)
    ap.add_argument("--no-start", action="store_true", help="only listen/render; do not SSH to the KR260")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--scale", type=int, default=5)
    ap.add_argument("--decay", type=float, default=0.88)
    args = ap.parse_args()

    proc = None
    if not args.no_start:
        proc = deploy_and_start(args)

    try:
        render(args)
    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None:
            proc.terminate()
        print("\nstopped", flush=True)


if __name__ == "__main__":
    sys.exit(main())
