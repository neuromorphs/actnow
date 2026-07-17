#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_entropy/main.c
("Entropy's Bloodhound" -- an arrow-of-time detector that sniffs the
thermodynamic asymmetry of event-camera data.  Per pixel it tracks the last
event polarity; an ON->OFF transition ("decay") increments C_fwd; an OFF->ON
transition ("kindle") increments C_rev.  Over each window of WINDOW_BATCHES=256
batches the windowed difference D = C_fwd - C_rev is a time-asymmetry
statistic; a MARGIN=16 threshold gates the verdict.  The renderer shows a
thermodynamic verdict gauge (needle, bar meters, scrolling D history).

python_entropy_words() below is a bit-faithful port of the firmware's integer
logic (same per-pixel state array, same saturating counters, same window/latch
advance, same word packing) so what we emit is provably what the chip would
emit given the same event stream.

------------------------------------------------------------------------------
Usage:
  dvs_entropy_view.py --validate                  # synthetic self-test (numbers)
  dvs_entropy_view.py --from-actsim results.mem   # render real chip status words
  dvs_entropy_view.py events.csv                  # render (host-computed) from a CSV
  dvs_entropy_view.py ... --headless --save entropy.png
"""
import argparse
import numpy as np

# --- must match software/dvs_entropy/main.c exactly ---
SX, SY = 126, 112
BATCH = 4

# Per-pixel state: 0=unseen, 1=last OFF, 2=last ON.
# idx = (y << 7) | x; array length 16384 spans every 7-bit (x, y) pair
# (max index (127<<7)|127 = 16383) so masked fields can never index out of
# bounds even on a glitched event word.  The live 126x112 sensor uses 14112.
STATE_SIZE    = 16384
WINDOW_BATCHES = 256    # window = 256 batches = 1024 events
COUNT_CAP      = 1023   # saturating ceiling for c_fwd / c_rev
MARGIN         = 16     # |D| threshold for a decisive verdict
WSEQ_MASK      = 0xF    # wseq wraps 0..15


def python_entropy_words(x, y, pol):
    """Bit-faithful port of software/dvs_entropy/main.c's ISR.

    x, y, pol are per-event arrays.  Processes only complete batches
    (n - n%BATCH events).

    Per batch, in event order:
      xi = int(x[i]) & 0x7F; yi = int(y[i]) & 0x7F; pi = int(pol[i]) & 1
      idx = (yi << 7) | xi; s = state[idx]
      if s==2 and pi==0: c_fwd += 1 (saturating at COUNT_CAP)
      elif s==1 and pi==1: c_rev += 1 (saturating at COUNT_CAP)
      state[idx] = 2 if pi else 1

    After each batch:
      batch_in_window += 1
      if batch_in_window >= WINDOW_BATCHES:
          batch_in_window = 0; lat_fwd = c_fwd; lat_rev = c_rev
          c_fwd = 0; c_rev = 0; wseq = (wseq+1) & WSEQ_MASK

    Then emit one word from LATCHED counts:
      D = lat_fwd - lat_rev
      v = 1 if D >= MARGIN else (2 if D <= -MARGIN else 0)
      word = (wseq << 22) | (v << 20) | (lat_rev << 10) | lat_fwd

    Also tracks (independently, no cap) total_fwd and total_rev over the
    processed prefix.  Returns (words, total_fwd, total_rev).

    Word index i (0-based) always has wseq == ((i+1) // WINDOW_BATCHES) & 0xF
    because the latch/advance fires BEFORE the emit.
    """
    state = [0] * STATE_SIZE
    c_fwd = 0
    c_rev = 0
    lat_fwd = 0
    lat_rev = 0
    batch_in_window = 0
    wseq = 0
    total_fwd = 0
    total_rev = 0
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            pi = int(pol[i]) & 1
            idx = (yi << 7) | xi
            s = state[idx]
            if s == 2 and pi == 0:
                if c_fwd < COUNT_CAP:
                    c_fwd += 1
                total_fwd += 1
            elif s == 1 and pi == 1:
                if c_rev < COUNT_CAP:
                    c_rev += 1
                total_rev += 1
            state[idx] = 2 if pi else 1

        batch_in_window += 1
        if batch_in_window >= WINDOW_BATCHES:
            batch_in_window = 0
            lat_fwd = c_fwd
            lat_rev = c_rev
            c_fwd = 0
            c_rev = 0
            wseq = (wseq + 1) & WSEQ_MASK

        D = lat_fwd - lat_rev
        v = 1 if D >= MARGIN else (2 if D <= -MARGIN else 0)
        word = (wseq << 22) | (v << 20) | (lat_rev << 10) | lat_fwd
        words.append(word)

    return words, total_fwd, total_rev


def unpack_status(word):
    """Mirror of the firmware's FIFO_OUT packing.
    bits[9:0]=fwd, bits[19:10]=rev, bits[21:20]=verdict, bits[25:22]=wseq."""
    fwd     = word & 0x3FF
    rev     = (word >> 10) & 0x3FF
    verdict = (word >> 20) & 3
    wseq    = (word >> 22) & 0xF
    return fwd, rev, verdict, wseq


# ---------------------------------------------------------------------------
# Renderer: dark backdrop, verdict needle gauge, bar meters, D history chart.
# ---------------------------------------------------------------------------

def render_entropy(words, save=None, headless=False):
    """Compose one figure: verdict gauge, bar meters, per-window D history."""
    if not words:
        print("no words to render")
        return

    last_fwd, last_rev, last_verdict, _ = unpack_status(words[-1])
    last_D = last_fwd - last_rev

    # Collect one (fwd, rev) sample per wseq change.
    history = []
    prev_wseq = None
    for word in words:
        fwd, rev, verdict, wseq = unpack_status(word)
        if wseq != prev_wseq:
            history.append((fwd, rev))
            prev_wseq = wseq

    GOLD   = "#e8b84b"
    INDIGO = "#5a5fd4"
    DIM    = "#555566"
    BG     = "#0d0b10"
    TEXT   = "#e8dfc8"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last verdict={last_verdict} fwd={last_fwd} rev={last_rev} D={last_D}")
        if history:
            print("per-window D history:", [f - r for f, r in history])
        return

    fig = plt.figure(figsize=(10, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(3, 1, hspace=0.45,
                          top=0.88, bottom=0.09, left=0.10, right=0.95)
    ax_gauge = fig.add_subplot(gs[0])
    ax_bars  = fig.add_subplot(gs[1])
    ax_hist  = fig.add_subplot(gs[2])

    for ax in (ax_gauge, ax_bars, ax_hist):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#332d40")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Gauge: horizontal arrow-of-time needle ---
    ax_gauge.set_xlim(-1.2, 1.2)
    ax_gauge.set_ylim(-0.3, 0.5)
    ax_gauge.set_yticks([])
    ax_gauge.set_xticks([-1, 0, 1])
    ax_gauge.set_xticklabels(["← BACKWARD", "UNDECIDED", "FORWARD →"],
                              color=TEXT, fontsize=8)
    ax_gauge.axhline(0, color="#332d40", linewidth=1.0)
    ax_gauge.axvline(0, color=DIM, linewidth=0.6, linestyle="--")

    if last_verdict == 1:
        needle_x = 0.85
        needle_color = GOLD
        verdict_label = "TIME RUNS FORWARD"
    elif last_verdict == 2:
        needle_x = -0.85
        needle_color = INDIGO
        verdict_label = "TIME RUNS BACKWARD"
    else:
        needle_x = 0.0
        needle_color = DIM
        verdict_label = "UNDECIDED"

    ax_gauge.annotate(
        "", xy=(needle_x, 0.0), xytext=(0.0, 0.0),
        arrowprops=dict(arrowstyle="-|>", color=needle_color,
                        lw=2.5, mutation_scale=22),
    )
    ax_gauge.text(0.0, 0.30, verdict_label, ha="center", va="bottom",
                  color=needle_color, fontsize=11, fontweight="bold",
                  transform=ax_gauge.transData)
    ax_gauge.text(0.0, 0.20, f"D = {last_D:+d}  (fwd={last_fwd}, rev={last_rev})",
                  ha="center", va="bottom", color=TEXT, fontsize=8,
                  transform=ax_gauge.transData)
    ax_gauge.set_title("arrow of time", color=TEXT, fontsize=9, pad=4)

    # --- Bar meters: fwd (gold) and rev (indigo) ---
    ax_bars.set_xlim(0, COUNT_CAP)
    ax_bars.set_ylim(-0.5, 1.5)
    ax_bars.set_yticks([0, 1])
    ax_bars.set_yticklabels(["kindle  (OFF→ON, rev)", "decay  (ON→OFF, fwd)"],
                             color=TEXT, fontsize=8)
    ax_bars.set_xlabel("count (0..1023)", color=TEXT, fontsize=8)
    ax_bars.barh(1, last_fwd, color=GOLD,   height=0.5, alpha=0.85)
    ax_bars.barh(0, last_rev, color=INDIGO, height=0.5, alpha=0.85)
    ax_bars.text(last_fwd + 8, 1, str(last_fwd), va="center",
                 color=GOLD, fontsize=8)
    ax_bars.text(last_rev + 8, 0, str(last_rev), va="center",
                 color=INDIGO, fontsize=8)
    ax_bars.set_title("last window counts", color=TEXT, fontsize=9, pad=4)

    # --- D history: stepped filled area ---
    ax_hist.axhline(0, color=DIM, linewidth=0.7)
    ax_hist.axhline(MARGIN,  color=GOLD,   linewidth=0.5, linestyle="--", alpha=0.5)
    ax_hist.axhline(-MARGIN, color=INDIGO, linewidth=0.5, linestyle="--", alpha=0.5)
    if history:
        ds = [f - r for f, r in history]
        xs = list(range(len(ds)))
        ds_arr = np.array(ds, dtype=float)
        # Stepped line.
        ax_hist.step(xs, ds_arr, where="post", color=TEXT, linewidth=1.0)
        # Fill above/below zero.
        ax_hist.fill_between(xs, ds_arr, 0,
                             where=(ds_arr >= 0), step="post",
                             color=GOLD, alpha=0.35)
        ax_hist.fill_between(xs, ds_arr, 0,
                             where=(ds_arr < 0), step="post",
                             color=INDIGO, alpha=0.35)
    ax_hist.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_hist.set_ylabel("D = fwd − rev", color=TEXT, fontsize=8)
    ax_hist.set_title("per-window D history", color=TEXT, fontsize=9, pad=4)

    fig.suptitle('"Entropy\'s Bloodhound"', color="#e8dfc8", fontsize=12,
                 fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: lettered exact-integer checks, zero tolerance.
# ---------------------------------------------------------------------------

def validate():
    ok = True

    # ------------------------------------------------------------------
    # Build FADE WORLD: 32x32 block, x in 40..71, y in 40..71 (1024 px).
    # Pass 1: every pixel gets one ON event (raster order).
    # Pass 2: every pixel gets one OFF event (raster order).
    # Total 2048 events, multiple of BATCH.
    # ------------------------------------------------------------------
    pixels = [(x, y) for y in range(40, 72) for x in range(40, 72)]  # 1024
    xs_a = np.array([p[0] for p in pixels] + [p[0] for p in pixels],
                    dtype=np.int64)
    ys_a = np.array([p[1] for p in pixels] + [p[1] for p in pixels],
                    dtype=np.int64)
    ps_a = np.array([1] * 1024 + [0] * 1024, dtype=np.int64)

    words_a, tf_a, tr_a = python_entropy_words(xs_a, ys_a, ps_a)

    # (a) FADE-WORLD ARROW
    last_fwd_a, last_rev_a, last_v_a, _ = unpack_status(words_a[-1])
    a_ok = (tf_a == 1024) and (tr_a == 0) and (last_v_a == 1)
    print(f"  (a) FADE-WORLD ARROW: total_fwd={tf_a} (want 1024), "
          f"total_rev={tr_a} (want 0), "
          f"last verdict={last_v_a} (want 1) -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # (b) TIME MIRROR: reversed order, pol NOT flipped.
    xs_b = xs_a[::-1].copy()
    ys_b = ys_a[::-1].copy()
    ps_b = ps_a[::-1].copy()
    words_b, tf_b, tr_b = python_entropy_words(xs_b, ys_b, ps_b)
    last_fwd_b, last_rev_b, last_v_b, _ = unpack_status(words_b[-1])
    b_ok = (tf_b == tr_a) and (tr_b == tf_a) and (last_v_b == 2)
    print(f"  (b) TIME MIRROR (reversal): total_fwd={tf_b} (want {tr_a}), "
          f"total_rev={tr_b} (want {tf_a}), "
          f"last verdict={last_v_b} (want 2) -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # (c) POLARITY MIRROR: same order, pol -> 1-pol.
    ps_c = (1 - ps_a).astype(np.int64)
    words_c, tf_c, tr_c = python_entropy_words(xs_a, ys_a, ps_c)
    last_fwd_c, last_rev_c, last_v_c, _ = unpack_status(words_c[-1])
    c_ok = (tf_c == tr_a) and (tr_c == tf_a) and (last_v_c == 2)
    print(f"  (c) POLARITY MIRROR (flip): total_fwd={tf_c} (want {tr_a}), "
          f"total_rev={tr_c} (want {tf_a}), "
          f"last verdict={last_v_c} (want 2) -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # (d) LOSCHMIDT INVARIANCE: reversed order AND flipped pol.
    ps_d = (1 - ps_a[::-1]).astype(np.int64)
    xs_d = xs_a[::-1].copy()
    ys_d = ys_a[::-1].copy()
    words_d, tf_d, tr_d = python_entropy_words(xs_d, ys_d, ps_d)
    last_fwd_d, last_rev_d, last_v_d, _ = unpack_status(words_d[-1])
    d_ok = (tf_d == tf_a) and (tr_d == tr_a) and (last_v_d == 1)
    print(f"  (d) LOSCHMIDT INVARIANCE (reversal+flip): total_fwd={tf_d} (want {tf_a}), "
          f"total_rev={tr_d} (want {tr_a}), "
          f"last verdict={last_v_d} (want 1) -> "
          f"{'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # (e) CHATTER CANCELLATION: 8 hot pixels, strictly alternating polarity,
    # interleaved round-robin, 4096 events.
    hot = [(5,5),(120,5),(5,105),(120,105),(63,56),(30,80),(90,20),(60,10)]
    # Per-pixel pol starts at 1 for even pixel index, 0 for odd (arbitrary,
    # but strict alternation per pixel is what matters).
    n_hot = 4096
    xs_e_list = []
    ys_e_list = []
    ps_e_list = []
    pix_pol = [i % 2 for i in range(len(hot))]  # initial pol per pixel
    for ev in range(n_hot):
        pi = ev % len(hot)
        px, py = hot[pi]
        xs_e_list.append(px)
        ys_e_list.append(py)
        ps_e_list.append(pix_pol[pi])
        pix_pol[pi] ^= 1

    xs_e = np.array(xs_e_list, dtype=np.int64)
    ys_e = np.array(ys_e_list, dtype=np.int64)
    ps_e = np.array(ps_e_list, dtype=np.int64)
    words_e, tf_e, tr_e = python_entropy_words(xs_e, ys_e, ps_e)
    chatter_bound_ok = abs(tf_e - tr_e) <= 8
    all_undecided = all(unpack_status(w)[2] == 0 for w in words_e)
    e_ok = chatter_bound_ok and all_undecided
    print(f"  (e) CHATTER CANCELLATION: total_fwd={tf_e}, total_rev={tr_e}, "
          f"|D|={abs(tf_e-tr_e)} (want <=8), "
          f"all verdict==0={all_undecided} -> "
          f"{'OK' if e_ok else 'FAIL'}")
    ok = ok and e_ok

    # (f) WSEQ ARITHMETIC: stream (a) concatenated with stream (e) events,
    # run as one contiguous stream so wseq advances monotonically.
    xs_f = np.concatenate([xs_a, xs_e])
    ys_f = np.concatenate([ys_a, ys_e])
    ps_f = np.concatenate([ps_a, ps_e])
    words_f, _, _ = python_entropy_words(xs_f, ys_f, ps_f)
    wseq_ok = True
    for i, word in enumerate(words_f):
        expected = ((i + 1) // WINDOW_BATCHES) & WSEQ_MASK
        _, _, _, actual = unpack_status(word)
        if actual != expected:
            wseq_ok = False
            break
    f_ok = wseq_ok
    print(f"  (f) WSEQ ARITHMETIC: every word[i] has wseq==((i+1)//{WINDOW_BATCHES})&0xF -> "
          f"{'OK' if f_ok else 'FAIL'}")
    ok = ok and f_ok

    # (g) WELL-FORMEDNESS: over all streams.
    all_words = words_a + words_b + words_c + words_d + words_e
    well_ok = True
    bad = []
    for word in all_words:
        fwd, rev, verdict, wseq = unpack_status(word)
        if fwd > 1023:
            bad.append(f"fwd={fwd}"); well_ok = False
        if rev > 1023:
            bad.append(f"rev={rev}"); well_ok = False
        if verdict not in (0, 1, 2):
            bad.append(f"verdict={verdict}"); well_ok = False
        if wseq > 15:
            bad.append(f"wseq={wseq}"); well_ok = False
        if word >= (1 << 26):
            bad.append(f"word=0x{word:08x}"); well_ok = False
    g_ok = well_ok
    print(f"  (g) WELL-FORMEDNESS: {len(all_words)} total words; "
          f"fwd<=1023, rev<=1023, verdict in {{0,1,2}}, wseq<=15, word<2^26 -> "
          f"{'OK' if g_ok else 'FAIL: ' + '; '.join(bad[:5])}")
    ok = ok and g_ok

    print()
    print("VALIDATION:", "PASS -- fade-world arrow exact; time-mirror swap exact; "
          "polarity-mirror swap exact; Loschmidt invariance exact; "
          "chatter cancellation proven; wseq arithmetic exact; word fields well-formed"
          if ok else "FAIL")
    return ok


def load_csv(path):
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name.strip(): i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x   = np.array([int(row[idx["x"]])   for row in rows], dtype=np.int64)
    y   = np.array([int(row[idx["y"]])   for row in rows], dtype=np.int64)
    pol = np.array([int(row[idx["pol"]]) for row in rows], dtype=np.int64)
    return x, y, pol


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: fade-world arrow, time-mirror, "
                         "polarity-mirror, Loschmidt invariance, chatter cancellation, "
                         "wseq arithmetic, well-formedness")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the entropy PNG here")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} real chip status words from {args.from_actsim}")
    elif args.csv:
        x, y, pol = load_csv(args.csv)
        print(f"loaded {len(x)} events from {args.csv}; computing entropy words in "
              f"Python (bit-faithful mirror of firmware).")
        words, total_fwd, total_rev = python_entropy_words(x, y, pol)
        if words:
            last_fwd, last_rev, last_verdict, last_wseq = unpack_status(words[-1])
            verdict_str = {1: "FORWARD", 2: "BACKWARD", 0: "UNDECIDED"}.get(
                last_verdict, "?")
            print(f"total_fwd={total_fwd}, total_rev={total_rev}, "
                  f"last window fwd={last_fwd} rev={last_rev} "
                  f"D={last_fwd - last_rev} verdict={verdict_str} "
                  f"({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_entropy(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
