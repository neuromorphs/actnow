#!/usr/bin/env python3
"""Live 3-panel view of stabilize.csv (a handheld recording with real
camera jitter): raw capture | the real chip's fixed 45-degree rotate45
(actsim output, same real firmware/hardware dvs_rotate_view.py shows) |
a dynamically-stabilized copy that estimates and cancels whatever
rotation the events actually show, continuously re-locking to the
recording's starting orientation.

Why the middle panel exists at all: the real multiply-free RV32 core (no
RV32M -- see software/dvs_rotate/main.c's header) can only ever perform a
fixed, pre-known 45-degree shift-based rotation on-chip; it structurally
cannot estimate an unknown, time-varying angle (that needs real multiplies
and trig: corner detection, optical flow, a least-squares rigid-transform
fit). So this launches chips/fpga/tests/e2e/e2e_fpga_rotate_capture.act's
actsim run itself -- the exact same real firmware capability
dvs_rotate_view.py's --from-actsim mode shows, not a reimplementation --
as a side-by-side "what the hardware alone can do" reference, while the
third/right panel is a Python-side algorithm layered on top of the same
recording that does what the chip can't: figure out the angle and correct
it, dynamically, every frame.

Stabilization algorithm (right panel) -- feature tracking, the common
approach to video stabilization, run incrementally frame to frame so the
correction stays causal (only past frames inform it):

  1. Events are batched into --rate-sized groups, each accumulated (with
     --track-decay) into a small grayscale tracking frame.
  2. cv2.goodFeaturesToTrack finds corners in the previous tracking frame;
     cv2.calcOpticalFlowPyrLK follows them into the current one.
  3. cv2.estimateAffinePartial2D (RANSAC) fits one rigid rotation +
     translation across the matched pairs -- the frame's motion, whatever
     dominates the tracked points (so a large moving/rotating object in
     frame counts the same as camera shake would: there's no separate
     "this part is the real camera" signal available, only the events).
     If too few points survive to fit anything (--min-features, default
     3 -- the minimum estimateAffinePartial2D needs), the *previous*
     frame's correction is reused rather than dropping to zero, so the
     stabilized panel is always being corrected by something instead of
     silently going uncorrected for a frame.
  4. Every frame, --gain (default 1.0 = full cancellation) times that
     motion is cancelled by warping the stabilized accumulator
     (cv2.warpAffine) before the new batch is stamped onto it -- a
     running lock back toward frame zero's orientation, not a jitter-only
     high-pass filter (an earlier version of this tried EMA-smoothing the
     motion and only cancelling the residual, on the theory that a slow
     rotation was "intentional" pan to preserve -- but that filter has
     unity DC gain, so its cumulative correction converges to the same
     total as the raw motion's cumulative sum and can't remove a real net
     rotation, only smooth the wiggle around it. "Always stabilized"
     means cancelling the measured motion directly, every frame.)

Rendering matches dvs_rotate_view.py (ON=green, OFF=red, exponential
decay). An on-screen readout shows the real chip's status (running/done),
the raw cumulative angle measured, and how many tracked points fed the
current frame's estimate.

Usage: dvs_stabilize_live.py [--csv stabilize.csv] [--rate 300] [--fps 30]
                              [--scale 6] [--decay 0.88]
                              [--track-decay 0.55] [--gain 1.0]
                              [--min-features 3] [--cross PREFIX]
live keys: q=quit (kills the actsim run if still in progress)
"""
import argparse
import math
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from dvs_motion_blob_live import load_events, resolve_csv  # reused unchanged

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(REPO_ROOT, "chips", "fpga")
DEFAULT_CSV = "stabilize.csv"
EVENTS_PATH = os.path.join(DATA_DIR, "rotate_capture_events.mem")
RESULTS_PATH = os.path.join(DATA_DIR, "rotate_capture_results.mem")

SX, SY = 126, 112
BATCH = 10000  # e2e_fpga_rotate_capture.act's ISR reads/writes exactly BATCH words per interrupt


def unpack(word):
    """Inverse of evt_pack.v's low 15 bits: {pol, y[6:0], x[6:0]} -- matches
    dvs_rotate_view.py's unpack()."""
    x = word & 0x7F
    y = (word >> 7) & 0x7F
    pol = (word >> 14) & 0x1
    return x, y, pol


class Stabilizer:
    """Frame-to-frame rigid motion estimate (goodFeaturesToTrack + Lucas-
    Kanade optical flow + a RANSAC rigid fit), cancelled directly each
    frame (scaled by --gain), falling back to the previous frame's
    correction whenever this frame can't be measured -- see this file's
    module docstring for why a jitter-only high-pass filter (unity DC
    gain) isn't the right tool for "stabilize", and why always applying
    *some* correction matters more here than only correcting when
    confident."""

    def __init__(self, gain, min_features):
        self.gain = gain
        self.min_features = min_features
        self.prev_gray = None
        self.last_corr = (0.0, 0.0, 0.0)
        self.raw_deg = 0.0          # cumulative measured angle, uncorrected
        self.uncancelled_deg = 0.0  # cumulative angle left uncorrected in the stabilized panel

    def step(self, cv2, gray):
        da = dx = dy = 0.0
        n_matched = 0
        measured = False

        if self.prev_gray is not None:
            corners = cv2.goodFeaturesToTrack(
                self.prev_gray, maxCorners=200, qualityLevel=0.01, minDistance=6)
            if corners is not None and len(corners) >= self.min_features:
                nxt, status, _ = cv2.calcOpticalFlowPyrLK(self.prev_gray, gray, corners, None)
                status = status.reshape(-1).astype(bool)
                prev_pts, curr_pts = corners[status], nxt[status]
                n_matched = int(status.sum())
                if n_matched >= self.min_features:
                    m, _ = cv2.estimateAffinePartial2D(prev_pts, curr_pts, method=cv2.RANSAC)
                    if m is not None:
                        da = float(np.arctan2(m[1, 0], m[0, 0]))
                        dx, dy = float(m[0, 2]), float(m[1, 2])
                        measured = True
        self.prev_gray = gray

        if measured:
            corr_da, corr_dx, corr_dy = self.gain * da, self.gain * dx, self.gain * dy
            self.last_corr = (corr_da, corr_dx, corr_dy)
            self.raw_deg += math.degrees(da)
            self.uncancelled_deg += math.degrees(da - corr_da)
        else:
            corr_da, corr_dx, corr_dy = self.last_corr

        return corr_da, corr_dx, corr_dy, n_matched, measured


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV,
                     help="AER capture to replay -- a bare filename (e.g. stabilize.csv, phone.csv) "
                          "resolves alongside this script; default is the handheld recording this "
                          "view was built for")
    ap.add_argument("--rate", type=int, default=300,
                     help="events per tracking/display frame -- this is both the display's pacing "
                          "and the tracker's frame boundary (one goodFeaturesToTrack/optical-flow "
                          "step per batch)")
    ap.add_argument("--fps", type=float, default=30.0, help="display refresh rate")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--decay", type=float, default=0.88, help="display accumulator decay (all 3 panels)")
    ap.add_argument("--track-decay", type=float, default=0.55,
                     help="internal tracking-frame decay -- shorter memory than --decay so "
                          "goodFeaturesToTrack sees roughly this frame's own shape instead of "
                          "several frames blurred together")
    ap.add_argument("--gain", type=float, default=1.0,
                     help="fraction of each frame's estimated motion to cancel: 1.0 fully locks the "
                          "stabilized panel back toward the recording's starting orientation every "
                          "frame; lower values leave some of the measured motion uncorrected")
    ap.add_argument("--min-features", type=int, default=3,
                     help="minimum tracked point pairs required to trust a frame's motion estimate "
                          "(3 is the practical minimum for a stable estimateAffinePartial2D fit); "
                          "below this the previous frame's correction is reused instead of zeroing it")
    ap.add_argument("--cross", default="riscv64-unknown-elf-", help="RISC-V cross-compiler prefix")
    args = ap.parse_args()

    try:
        import cv2
    except Exception as e:
        print("cv2 (GUI-enabled opencv-python) is required for the live view:", e)
        sys.exit(1)

    csv_path = resolve_csv(args.csv)
    print(f"loading recorded events for the real chip's rotate45 input ({csv_path})...")
    xs, ys, pols, les = load_events(csv_path)
    n_events = (len(xs) // BATCH) * BATCH
    print(f"{n_events} events in {csv_path}")

    print(f"writing {EVENTS_PATH} (actsim's input) ...")
    with open(EVENTS_PATH, "w") as f:
        for le in les[:n_events]:
            f.write(f"{le}\n")

    for stale in (RESULTS_PATH,):
        if os.path.exists(stale):
            os.remove(stale)

    print("rebuilding the dvs_rotate ROM image...")
    subprocess.run(
        ["make", f"ROM_TEST=dvs_rotate", f"CROSS={args.cross}", "file-registry"],
        cwd=REPO_ROOT, check=True,
    )

    print("launching actsim -- this is the live run, not a replay...")
    log_path = "/tmp/dvs_stabilize_live_actsim.log"
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        ["actsim", "-cnf=gen/file_registry.conf",
         "chips/fpga/tests/e2e/e2e_fpga_rotate_capture.act", "e2e_fpga_rotate_capture"],
        cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=log_f, stderr=subprocess.STDOUT,
    )
    proc.stdin.write(b"cycle\nquit\n")
    proc.stdin.close()

    # swap=True, flipx=True, flipy=False -- same default orientation as
    # dvs_replay.py / dvs_rotate_view.py's rig.
    W, H = SY, SX
    gap = max(2, args.scale // 3)
    cx, cy = W / 2.0, H / 2.0

    acc_raw = np.zeros((H, W, 3), np.float32)
    acc_chip = np.zeros((H, W, 3), np.float32)
    acc_stab = np.zeros((H, W, 3), np.float32)
    track = np.zeros((H, W), np.float32)
    stab = Stabilizer(gain=args.gain, min_features=args.min_features)

    def stamp(acc, xi, yi, pi):
        if not (0 <= xi < SX and 0 <= yi < SY):
            return None
        col, row = yi, xi
        col = W - 1 - col  # flipx
        if 0 <= col < W and 0 <= row < H:
            acc[row, col] = (0, 1, 0) if pi else (0, 0, 1)  # ON green, OFF red
            return col, row
        return None

    def process_batch(lo, hi, chip_words):
        nonlocal track
        for i, word in zip(range(lo, hi), chip_words):
            pos = stamp(acc_raw, int(xs[i]), int(ys[i]), int(pols[i]))
            if pos is not None:
                cv2.circle(track, pos, 1, 255, -1)
            rx, ry, rpol = unpack(word)
            stamp(acc_chip, rx, ry, rpol)

        gray = np.clip(track, 0, 255).astype(np.uint8)
        corr_da, corr_dx, corr_dy, n_matched, measured = stab.step(cv2, gray)

        M = cv2.getRotationMatrix2D((cx, cy), math.degrees(-corr_da), 1.0)
        M[0, 2] += -corr_dx
        M[1, 2] += -corr_dy
        acc_stab[:] = cv2.warpAffine(acc_stab * args.decay, M, (W, H))
        for i in range(lo, hi):
            stamp(acc_stab, int(xs[i]), int(ys[i]), int(pols[i]))

        track *= args.track_decay
        return n_matched, measured

    def side_by_side():
        sep = np.full((H, gap, 3), 0.15, np.float32)
        return np.concatenate([acc_raw, sep, acc_chip, sep, acc_stab], axis=1)

    total_w = W * 3 + gap * 2
    cv2.namedWindow("DVS stabilize -- raw | chip rotate45 | dynamically stabilized", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS stabilize -- raw | chip rotate45 | dynamically stabilized",
                      total_w * args.scale, H * args.scale)

    results_f = None
    pending = []
    words_seen = 0
    frame_idx = 0
    total_frames = (n_events + args.rate - 1) // args.rate
    finished = False
    last_matched = (0, False)
    t_start = time.time()

    print("live stabilize view running (q to quit)...")
    try:
        while True:
            if results_f is None and os.path.exists(RESULTS_PATH):
                results_f = open(RESULTS_PATH, "r")

            if results_f is not None:
                for line in results_f:
                    line = line.strip()
                    if line:
                        pending.append(int(line))

            while pending and (len(pending) >= args.rate or finished):
                take = min(args.rate, len(pending))
                chip_words = pending[:take]
                pending = pending[take:]
                lo, hi = words_seen, words_seen + take
                acc_raw *= args.decay
                acc_chip *= args.decay
                last_matched = process_batch(lo, hi, chip_words)
                words_seen = hi
                frame_idx += 1

            if not finished and proc.poll() is not None:
                finished = True

            img = (np.clip(side_by_side(), 0, 1) * 255).astype(np.uint8)
            elapsed = time.time() - t_start
            status = "DONE" if (finished and words_seen >= n_events) else "running"
            n_matched, measured = last_matched
            note = f"pts={n_matched}" if measured else "reusing last corr"
            cv2.putText(img, f"raw {words_seen}/{n_events}", (2, 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.putText(img, "chip rotate45 (actsim)", (W + gap + 2, 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.putText(img, f"dynamic  angle {stab.raw_deg:+.1f}deg  {note}", (2 * (W + gap) + 2, 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.putText(img, f"frame {frame_idx}/{total_frames}  t={elapsed:0.1f}s  actsim: {status}",
                        (2, H * 1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

            cv2.imshow("DVS stabilize -- raw | chip rotate45 | dynamically stabilized", img)
            k = cv2.waitKey(max(1, int(1000 / args.fps))) & 0xff
            if k == ord('q'):
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
        log_f.close()
        cv2.destroyAllWindows()

    print(f"processed {words_seen}/{n_events} events; raw cumulative angle {stab.raw_deg:.1f} deg, "
          f"{stab.uncancelled_deg:.1f} deg left uncancelled in the stabilized panel; "
          f"actsim log at {log_path}")


if __name__ == "__main__":
    main()
