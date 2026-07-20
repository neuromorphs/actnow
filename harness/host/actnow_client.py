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
DEFAULT_SOCKET_BUF = 1 << 18


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


def append_limited(chunks, total_words, words, limit):
    if words.size == 0:
        return chunks, total_words
    if words.size >= limit:
        return [words[-limit:]], limit

    chunks.append(words)
    total_words += words.size
    while total_words > limit:
        excess = total_words - limit
        first = chunks[0]
        if first.size <= excess:
            chunks.pop(0)
            total_words -= first.size
        else:
            chunks[0] = first[excess:]
            total_words -= excess
    return chunks, total_words


def draw_words(acc, np, words, flip_up_down=False, colourblind=False):
    if words.size == 0:
        return

    x = ((words >> 24) & 0x7F).astype(np.int16)
    y = ((words >> 17) & 0x7F).astype(np.int16)
    valid = (x < SX) & (y < SY)
    if not np.any(valid):
        return

    rows = x[valid] if flip_up_down else SX - 1 - x[valid]
    cols = y[valid]
    if colourblind:
        chans = np.where((words[valid] & 1) != 0, 2, 0)
    else:
        chans = np.where((words[valid] & 1) != 0, 1, 2)
    acc[rows, cols, chans] = 1.0


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
        f"--raw-port {args.raw_port} "
        f"--xsa {Path(args.xsa).name} --firmware {Path(args.firmware).name}'"
    )
    return subprocess.Popen(["ssh", remote, cmd])


def render(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.socket_buf)
    sock.bind(("0.0.0.0", args.port))
    sock.setblocking(False)

    cv2 = None
    np = None
    if not args.headless:
        try:
            import cv2 as _cv2
            import numpy as _np
            cv2, np = _cv2, _np
            title = getattr(args, "title", "ActNow result stream")
            cv2.namedWindow(title, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(title, SY * args.scale, SX * args.scale)
            acc = np.zeros((SX, SY, 3), np.float32)
        except Exception as exc:
            print(f"viewer unavailable, falling back to headless: {exc}", flush=True)
            cv2 = None
            acc = None
    else:
        acc = None

    total = 0
    packets = 0
    dropped = 0
    expect_seq = None
    last_t = time.monotonic()
    last_total = 0
    last_packets = 0

    frame_period = 1.0 / max(args.fps, 1.0)
    last_frame = time.monotonic()
    pending_chunks = []
    pending_words = 0

    print(f"listening UDP :{args.port}", flush=True)
    while True:
        drained = 0
        while drained < args.max_drain_packets:
            try:
                data, _addr = sock.recvfrom(65535)
            except BlockingIOError:
                break

            drained += 1
            if len(data) < HDR.size or data[:4] != MAGIC:
                continue

            _magic, seq, n = HDR.unpack_from(data)
            if expect_seq is not None and seq != expect_seq:
                dropped += (seq - expect_seq) & 0xFFFFFFFF
            expect_seq = (seq + 1) & 0xFFFFFFFF

            m = min(n, (len(data) - HDR.size) // 4)
            total += m
            packets += 1

            if cv2 is not None and m:
                words = np.frombuffer(data, dtype="<u4", count=m, offset=HDR.size)
                pending_chunks, pending_words = append_limited(
                    pending_chunks, pending_words, words, args.max_frame_words
                )

        now = time.monotonic()
        if cv2 is not None and now - last_frame >= frame_period:
            elapsed_frames = max((now - last_frame) / frame_period, 1.0)
            last_frame = now
            acc *= args.decay ** elapsed_frames

            if pending_chunks:
                words = pending_chunks[0] if len(pending_chunks) == 1 else np.concatenate(pending_chunks)
                draw_words(acc, np, words,
                           getattr(args, "flip_up_down", False),
                           getattr(args, "colourblind", False))
                pending_chunks = []
                pending_words = 0

            img = (np.clip(acc, 0, 1) * 255).astype(np.uint8)
            cv2.imshow(title, img)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        if now - last_t >= 1.0:
            rate = (total - last_total) / (now - last_t)
            pkt_rate = (packets - last_packets) / (now - last_t)
            print(
                f"\r{rate:9.0f} words/s {pkt_rate:7.0f} pkts/s "
                f"total={total} dropped_pkts={dropped}",
                end="",
                flush=True,
            )
            last_t = now
            last_total = total
            last_packets = packets

        if drained == 0:
            if cv2 is None:
                time.sleep(0.005)
            else:
                sleep_s = max(0.0, frame_period - (time.monotonic() - last_frame))
                time.sleep(min(sleep_s, 0.002))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kria", default="kria.local")
    ap.add_argument("--user", default="ubuntu")
    ap.add_argument("--remote-dir", default="/tmp/actnow_harness")
    ap.add_argument("--listen-host", required=True, help="host/IP the KR260 should send UDP to")
    ap.add_argument("--port", type=int, default=3334)
    ap.add_argument("--raw-port", type=int, default=3336,
                    help="raw-event UDP destination passed to the KR260 server")
    ap.add_argument("--xsa", required=True)
    ap.add_argument("--firmware", required=True)
    ap.add_argument("--no-start", action="store_true", help="only listen/render; do not SSH to the KR260")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--scale", type=int, default=5)
    ap.add_argument("--decay", type=float, default=0.88)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--socket-buf", type=int, default=DEFAULT_SOCKET_BUF)
    ap.add_argument("--max-drain-packets", type=int, default=4096)
    ap.add_argument("--max-frame-words", type=int, default=100000)
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
