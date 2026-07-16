#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_heartbeats/main.c
("Objects Have Secret Heartbeats").

The chip emits one status word per BATCH=4 events: {region, period_bin,
confidence} for the region the last event of the batch touched. This host
unpacks those words and paints a per-region period/frequency map (a grid
colored by detected period), and OPTIONALLY sonifies each region as a
"heartbeat"/tone (period -> pitch) if an audio library is importable. Audio is
never a hard dependency -- with no audio lib it falls back to a clean visual
and prints a message.

python_heartbeat_words() below is a byte-for-byte port of the firmware's
integer logic (same region grid, same power-of-two period bins, same leaky
decay, same confidence-by-compare) so the map is provably what the chip would
emit given the same event+timestamp stream.

------------------------------------------------------------------------------
LIVE-ONLY caveat: period detection needs real per-event microsecond timestamps.
The recorded actnow CSVs (chips/fpga/*.csv) store `le`, a wrapped COARSE event
counter -- NOT a usable us timestamp -- so this app is only meaningful on the
live chip / a live AER stream. To validate the logic without live hardware,
--validate generates SYNTHETIC periodic events at KNOWN frequencies (5/13/50 Hz
in distinct regions, plus noise) and confirms each region's detected period_bin
matches its true frequency.

Usage:
  dvs_heartbeats_view.py --validate                 # synthetic self-test (numbers)
  dvs_heartbeats_view.py --from-actsim results.mem  # render real chip output
  dvs_heartbeats_view.py events.csv --ts-col le      # render (host-computed) from a CSV
                                                       (only sensible with real us ts)
  dvs_heartbeats_view.py ... --sonify                # add audio if a lib is present
  dvs_heartbeats_view.py ... --headless --save map.png
"""
import argparse
import numpy as np

# --- must match software/dvs_heartbeats/main.c exactly ---
SX, SY = 126, 112
REGION_SHIFT = 4                       # 16x16-px regions
REGION_COLS = 8                        # 126>>4 = 7 -> cols 0..7
REGION_ROWS = 7                        # 112>>4 = 6 -> rows 0..6
REGION_COL_SHIFT = 3                   # row*REGION_COLS via shift (COLS==8==1<<3)
REGION_CELLS = REGION_COLS * REGION_ROWS   # 56
TS_MASK = 0xFFFF                       # 16-bit timestamp (evt_pack.v ts field, matches firmware)
NBINS = 8
BUMP = 16
BIN_CAP = 255
DECAY_SHIFT = 3
BATCH = 4

# Bin edges (ts units), powers of two -- identical ladder to classify_bin().
BIN_EDGES = [64, 128, 256, 512, 1024, 2048, 4096]  # bin i if dt < EDGES[i]; else last bin

# Human labels for the map legend. The absolute Hz depends on ts LSB size; these
# assume ~32 us / LSB (see main.c's table). They are display-only.
BIN_LABELS = ["<2ms", "2-4ms", "4-8ms", "8-16ms", "16-32ms", "32-65ms", "65-130ms", "slow"]


def classify_bin(dt):
    """Byte-for-byte mirror of main.c's classify_bin (compare ladder)."""
    if dt < 64:   return 0
    if dt < 128:  return 1
    if dt < 256:  return 2
    if dt < 512:  return 3
    if dt < 1024: return 4
    if dt < 2048: return 5
    if dt < 4096: return 6
    return 7


def python_heartbeat_words(x, y, ts):
    """Bit-faithful port of software/dvs_heartbeats/main.c's ISR: one status
    word per BATCH=4 events. x,y,ts are per-event arrays; ts is the 17-bit
    timestamp field (already masked). Returns a list of packed status words."""
    bins = [[0] * NBINS for _ in range(REGION_CELLS)]
    last_ts = [0] * REGION_CELLS
    seen = [0] * REGION_CELLS
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        last_region = 0
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            ti = int(ts[i]) & TS_MASK
            col = xi >> REGION_SHIFT
            row = yi >> REGION_SHIFT
            r = (row << REGION_COL_SHIFT) | col
            last_region = r
            if seen[r]:
                dt = (ti - last_ts[r]) & TS_MASK
                bn = classify_bin(dt)
                for k in range(NBINS):
                    bins[r][k] = bins[r][k] - (bins[r][k] >> DECAY_SHIFT)
                bins[r][bn] = min(bins[r][bn] + BUMP, BIN_CAP)
            else:
                seen[r] = 1
            last_ts[r] = ti

        r = last_region
        best_bin, best_val, total = 0, bins[r][0], bins[r][0]
        for k in range(1, NBINS):
            val = bins[r][k]
            total += val
            if val > best_val:
                best_val, best_bin = val, k
        eighth = total >> 3
        if eighth == 0:
            conf = 8 if best_val > 0 else 0
        else:
            conf, acc = 0, eighth
            while conf < 8 and best_val >= acc:
                conf += 1
                acc += eighth
        words.append((conf << 10) | (best_bin << 6) | r)
    return words


def unpack_status(word):
    region = word & 0x3F
    period_bin = (word >> 6) & 0xF
    conf = (word >> 10) & 0xF
    col = region % REGION_COLS
    row = region // REGION_COLS
    return region, col, row, period_bin, conf


# ---------------------------------------------------------------------------
# Synthetic validation: known frequencies -> expected period_bin.
# ---------------------------------------------------------------------------
def gen_synthetic(ts_per_second=3906, seconds=6.0, noise_frac=0.15, seed=0):
    """Build a synthetic event stream in which three distinct regions blink at
    KNOWN frequencies, plus random noise events. ts_per_second sets the ts-unit
    scale (default ~256 us/LSB -> 3906 LSB/s, chosen so 5/13/50 Hz land in three
    DISTINCT mid-range period bins rather than saturating the 'slow' catch-all).
    Returns (x, y, ts, truth) where truth maps region -> (freq_hz, expected_bin)."""
    rng = np.random.default_rng(seed)
    # Pick three regions and target frequencies.
    specs = [
        # (region col, row, freq_hz)
        (1, 1, 5.0),
        (4, 2, 13.0),
        (6, 5, 50.0),
    ]
    ev = []  # (ts, x, y)
    truth = {}
    total_ts = int(ts_per_second * seconds)
    for (col, row, f) in specs:
        region = (row << REGION_COL_SHIFT) | col
        period_ts = ts_per_second / f
        expected_bin = classify_bin(int(round(period_ts)))
        truth[region] = (f, expected_bin, int(round(period_ts)))
        # center pixel of the region
        px = col * (1 << REGION_SHIFT) + 4
        py = row * (1 << REGION_SHIFT) + 4
        t = rng.uniform(0, period_ts)
        while t < total_ts:
            # small jitter so it's not perfectly periodic
            jit = rng.normal(0, period_ts * 0.03)
            ev.append((int(t + jit) & TS_MASK, px, py))
            t += period_ts
    # Noise events scattered across the frame.
    n_signal = len(ev)
    n_noise = int(n_signal * noise_frac)
    for _ in range(n_noise):
        ev.append((int(rng.integers(0, TS_MASK + 1)),
                   int(rng.integers(0, SX)),
                   int(rng.integers(0, SY))))
    # Sort by timestamp to look like a real stream, then wrap ts into 17 bits.
    ev.sort(key=lambda e: e[0])
    ts = np.array([e[0] & TS_MASK for e in ev], dtype=np.int64)
    x = np.array([e[1] for e in ev], dtype=np.int64)
    y = np.array([e[2] for e in ev], dtype=np.int64)
    return x, y, ts, truth


def validate():
    x, y, ts, truth = gen_synthetic()
    words = python_heartbeat_words(x, y, ts)
    # For each region, take the majority detected period_bin over all its words.
    from collections import Counter, defaultdict
    per_region = defaultdict(Counter)
    for w in words:
        region, col, row, pbin, conf = unpack_status(w)
        if conf >= 4:  # only trust confident reports
            per_region[region][pbin] += 1

    print(f"synthetic stream: {len(x)} events, {len(words)} status words")
    print(f"{'region':>7} {'freq':>6} {'true_bin':>9} {'det_bin':>8} "
          f"{'true_label':>11} {'det_label':>11}  match")
    ok = True
    for region, (f, expected_bin, period_ts) in sorted(truth.items()):
        if per_region[region]:
            det_bin = per_region[region].most_common(1)[0][0]
        else:
            det_bin = -1
        match = (det_bin == expected_bin)
        ok = ok and match
        det_label = BIN_LABELS[det_bin] if det_bin >= 0 else "none"
        print(f"{region:>7} {f:>5.0f}Hz {expected_bin:>9} {det_bin:>8} "
              f"{BIN_LABELS[expected_bin]:>11} {det_label:>11}  {'YES' if match else 'NO'}")
    print()
    print("VALIDATION:", "PASS -- every region's detected period_bin matches its "
          "true frequency" if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# Rendering: per-region period map.
# ---------------------------------------------------------------------------
def render_map(words, save=None, headless=False):
    """Paint the latest per-region argmax bin as a colored grid."""
    # Accumulate the most recent (bin, conf) per region.
    latest = {}
    for w in words:
        region, col, row, pbin, conf = unpack_status(w)
        latest[region] = (pbin, conf)

    # Build a REGION_ROWS x REGION_COLS image of bin indices, alpha by conf.
    grid_bin = np.full((REGION_ROWS, REGION_COLS), -1, dtype=np.int32)
    grid_conf = np.zeros((REGION_ROWS, REGION_COLS), dtype=np.float32)
    for region, (pbin, conf) in latest.items():
        c, r = region % REGION_COLS, region // REGION_COLS
        grid_bin[r, c] = pbin
        grid_conf[r, c] = conf / 8.0

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib unavailable:", e)
        print("Detected period bins per region (row-major):")
        print(grid_bin)
        return

    # color = period band; a perceptually ordered map. Empty = dark.
    disp = np.ma.masked_less(grid_bin, 0)
    fig, ax = plt.subplots(figsize=(REGION_COLS, REGION_ROWS))
    cmap = plt.get_cmap("turbo", NBINS)
    im = ax.imshow(disp, cmap=cmap, vmin=0, vmax=NBINS - 1,
                   alpha=np.clip(grid_conf, 0.2, 1.0), interpolation="nearest")
    ax.set_title('"Objects Have Secret Heartbeats" -- per-region period')
    ax.set_xticks(range(REGION_COLS)); ax.set_yticks(range(REGION_ROWS))
    cbar = fig.colorbar(im, ax=ax, ticks=range(NBINS))
    cbar.ax.set_yticklabels(BIN_LABELS)
    cbar.set_label("dominant period band")
    for r in range(REGION_ROWS):
        for c in range(REGION_COLS):
            if grid_bin[r, c] >= 0:
                ax.text(c, r, BIN_LABELS[grid_bin[r, c]], ha="center", va="center",
                        fontsize=6, color="white")
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=110)
        print(f"wrote {save}")
    if not headless:
        plt.show()


def sonify(words):
    """Map each region's dominant period -> a pitch and play a short chord.
    Optional: only runs if sounddevice or simpleaudio imports. Otherwise prints
    a graceful message and returns."""
    latest = {}
    for w in words:
        region, col, row, pbin, conf = unpack_status(w)
        if conf >= 4:
            latest[region] = pbin
    if not latest:
        print("sonify: no confident regions to sound.")
        return

    # period bin -> pitch: faster flicker (low bin) = higher note.
    base = 220.0
    freqs = [base * (2 ** ((NBINS - 1 - pbin) / 4.0)) for pbin in latest.values()]
    dur = 1.2
    sr = 44100
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    env = np.minimum(1.0, np.minimum(t / 0.02, (dur - t) / 0.3))
    wave = np.zeros_like(t)
    for f in freqs:
        wave += np.sin(2 * np.pi * f * t)
    wave = (wave / max(1, len(freqs))) * env
    audio = (wave * 0.3 * 32767).astype(np.int16)

    try:
        import sounddevice as sd
        print(f"sonify: playing {len(freqs)} region-heartbeats via sounddevice")
        sd.play(audio, sr); sd.wait()
        return
    except Exception:
        pass
    try:
        import simpleaudio as sa
        print(f"sonify: playing {len(freqs)} region-heartbeats via simpleaudio")
        play = sa.play_buffer(audio, 1, 2, sr); play.wait_done()
        return
    except Exception:
        pass
    print("sonify: no audio library (sounddevice/simpleaudio) available -- "
          "skipping sound, visual map only. (pip install sounddevice)")


def load_csv(path, ts_col):
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x = np.array([int(row[idx["x"]]) for row in rows], dtype=np.int64)
    y = np.array([int(row[idx["y"]]) for row in rows], dtype=np.int64)
    if ts_col in idx:
        ts = np.array([int(row[idx[ts_col]]) & TS_MASK for row in rows], dtype=np.int64)
    else:
        ts = np.zeros(len(x), dtype=np.int64)
    return x, y, ts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic known-frequency self-test (prints numbers)")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le -- NOTE: le is a "
                         "coarse counter, only meaningful with real us ts)")
    ap.add_argument("--sonify", action="store_true", help="also play region tones if audio present")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the period map PNG here")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} real chip status words from {args.from_actsim}")
    elif args.csv:
        x, y, ts = load_csv(args.csv, args.ts_col)
        print(f"loaded {len(x)} events from {args.csv}; computing status words in Python "
              f"(mirror of firmware). NOTE: '{args.ts_col}' is not a real us timestamp "
              f"unless captured live -- period map may be meaningless on recorded CSVs.")
        words = python_heartbeat_words(x, y, ts)
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_map(words, save=args.save, headless=args.headless)
    if args.sonify:
        sonify(words)


if __name__ == "__main__":
    main()
