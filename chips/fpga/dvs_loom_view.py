#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_loom/main.c
("The Finish-Line Loom" -- an event-driven slit-scan camera that weaves a
textile metaphor: three fixed 4-px vertical slits act as warp threads; every
event landing in a slit becomes a thread sample; a wrapping WEFT counter
(advancing on a pure event-count timebase -- every WEFT_BATCHES batches, no
real-time microseconds) drives the loom axis. The host weaves three cloth
strips -- warp = y (0..111), weft = time column (0..127) -- colouring threads
warm gold (ON events) or indigo (OFF events), faint if the sample failed the
noise floor (flag=0).

python_loom_words() below is a bit-faithful port of the firmware's integer
logic (same slit attribution, same per-ybin hit counter, same WEFT_BATCHES
advance, same candidate-promotion rule) so what we weave is provably what the
chip would emit given the same event stream.

------------------------------------------------------------------------------
Usage:
  dvs_loom_view.py --validate                  # synthetic self-test (numbers)
  dvs_loom_view.py --from-actsim results.mem   # render real chip status words
  dvs_loom_view.py events.csv                  # render (host-computed) from a CSV
  dvs_loom_view.py ... --headless --save loom.png
"""
import argparse
import numpy as np

# --- must match software/dvs_loom/main.c exactly ---
SX, SY = 126, 112
BATCH = 4

# Slit bands: a pixel lands in a slit when (x >> SLIT_XQ_SHIFT) equals the
# slit's xq value.  Three slits at xq = 5, 15, 25 -> x bands 20..23, 60..63,
# 100..103.
SLIT_XQ_SHIFT = 2
SLIT_XQS = (5, 15, 25)           # xq values for slit 0, 1, 2
SLIT_CENTRE_X = (21, 61, 101)    # display labels

# y binning: ybin = y >> YBIN_SHIFT (0..27, 28 bins).  Hit counter index in
# the 96-entry table: (slit << 5) | ybin.  The table is uint8 saturating at
# 255 and cleared each weft step.
YBIN_SHIFT = 2
WEFT_BATCHES = 16      # batches per weft step (event-count timebase)
MIN_HITS = 4           # hits needed for a sample to be flagged (confirmed)
WEFT_MASK = 0x7F       # weft wraps 0..127
WEFT_COLS = 128        # columns in the woven cloth


def python_loom_words(x, y, pol):
    """Bit-faithful port of software/dvs_loom/main.c's ISR: one word per
    BATCH=4 events.  x, y, pol are per-event arrays.  Returns a list of
    packed words (the same integers the chip writes to FIFO_OUT).

    Packing: word = (flag<<17)|(weft<<10)|(pol<<9)|(y<<2)|slit   [candidate]
             word = (weft<<10)|3                                   [sentinel]

    Per batch, in event order:
      - compute slit from xq = xi >> SLIT_XQ_SHIFT
      - if in a slit: update hits[idx], compute flag, apply candidate rule
    After the batch: emit one word, then advance batch_count / weft.
    """
    hits = [0] * 96   # (slit<<5)|ybin -> uint8 hit count; cleared each weft step
    batch_count = 0
    weft = 0
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        candidate_slit = -1
        candidate_y = 0
        candidate_pol = 0
        candidate_flag = -1   # -1 = no candidate yet

        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            pi = int(pol[i]) & 1
            xq = xi >> SLIT_XQ_SHIFT

            # Determine slit (0/1/2) or skip.
            if xq == SLIT_XQS[0]:
                slit = 0
            elif xq == SLIT_XQS[1]:
                slit = 1
            elif xq == SLIT_XQS[2]:
                slit = 2
            else:
                continue   # not in any slit

            idx = (slit << 5) | (yi >> YBIN_SHIFT)
            if hits[idx] < 255:
                hits[idx] += 1
            f = 1 if hits[idx] >= MIN_HITS else 0

            # Candidate promotion rule:
            #   flagged always replaces (f >= candidate_flag covers both
            #   0->1 and 1->1 last-wins); unflagged replaces only if no
            #   candidate yet (candidate_flag == -1) or only an unflagged
            #   candidate exists (candidate_flag == 0) -- last-wins within
            #   the same flag class; unflagged never replaces flagged.
            if f >= candidate_flag:
                candidate_slit = slit
                candidate_y = yi
                candidate_pol = pi
                candidate_flag = f

        # Emit one word for this batch.
        if candidate_flag >= 0:   # at least one candidate
            word = (candidate_flag << 17) | (weft << 10) | (candidate_pol << 9) | (candidate_y << 2) | candidate_slit
        else:
            word = (weft << 10) | 3   # sentinel: slit=3, y/pol/flag=0

        words.append(word)

        # Advance weft on event-count timebase.
        batch_count += 1
        if batch_count >= WEFT_BATCHES:
            batch_count = 0
            weft = (weft + 1) & WEFT_MASK
            hits[:] = [0] * 96

    return words


def unpack_status(word):
    """Mirror of the firmware's FIFO_OUT packing.
    bits[1:0]=slit, bits[8:2]=y, bit9=pol, bits[16:10]=weft, bit17=flag."""
    slit = word & 3
    y = (word >> 2) & 0x7F
    pol = (word >> 9) & 1
    weft = (word >> 10) & 0x7F
    flag = (word >> 17) & 1
    return slit, y, pol, weft, flag


# ---------------------------------------------------------------------------
# Cloth weaver and renderer: three (SY x WEFT_COLS) strips, dark backdrop,
# warm gold for ON threads, indigo for OFF threads, faint (0.35 weight) for
# noise-floor failures (flag=0).
# ---------------------------------------------------------------------------

def weave_cloth(words):
    """Build cloth_on and cloth_off float arrays, shape (3, SY, WEFT_COLS).
    ON threads go into cloth_on, OFF threads into cloth_off.
    Deposit 1.0 for flagged (flag=1) or 0.35 for faint (flag=0), using max
    so multiple hits in the same cell stay bounded.  Returns (cloth_on,
    cloth_off, last_weft) where last_weft is the final weft column seen."""
    cloth_on  = np.zeros((3, SY, WEFT_COLS), dtype=np.float32)
    cloth_off = np.zeros((3, SY, WEFT_COLS), dtype=np.float32)
    last_weft = 0
    for word in words:
        slit, y, pol, weft, flag = unpack_status(word)
        if slit == 3:
            last_weft = weft
            continue
        last_weft = weft
        weight = 1.0 if flag else 0.35
        if pol:
            cloth_on[slit, y, weft]  = max(cloth_on[slit, y, weft],  weight)
        else:
            cloth_off[slit, y, weft] = max(cloth_off[slit, y, weft], weight)
    return cloth_on, cloth_off, last_weft


def render_loom(words, save=None, headless=False):
    """Compose one RGB figure with three cloth strips stacked vertically."""
    cloth_on, cloth_off, last_weft = weave_cloth(words)

    # Build the RGB image for each strip: dark loom backdrop, ON=warm gold,
    # OFF=indigo.  Threads already carry their faint/full weight.
    #   Warm gold:  ~(1.0, 0.78, 0.25)
    #   Indigo:     ~(0.35, 0.40, 0.95)
    ON_COLOR  = np.array([1.00, 0.78, 0.25], dtype=np.float32)
    OFF_COLOR = np.array([0.35, 0.40, 0.95], dtype=np.float32)
    BACKDROP  = np.array([0.05, 0.04, 0.07], dtype=np.float32)

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        print("Cloth intensities (max ON per strip per weft column):")
        np.set_printoptions(precision=2, suppress=True, linewidth=200)
        for s in range(3):
            print(f"  slit x={SLIT_CENTRE_X[s]}: max ON col = {cloth_on[s].max(axis=0)}")
        return

    fig, axes = plt.subplots(3, 1, figsize=(10, 6))
    fig.patch.set_facecolor("#0d0b10")

    for s in range(3):
        ax = axes[s]
        # Compose the RGB strip channel by channel: dark backdrop, then deposit
        # whichever thread (ON gold / OFF indigo) is brighter per pixel.
        rgb = np.full((SY, WEFT_COLS, 3), BACKDROP, dtype=np.float32)
        for ci in range(3):
            on_layer  = cloth_on[s,  :, :] * ON_COLOR[ci]
            off_layer = cloth_off[s, :, :] * OFF_COLOR[ci]
            # Blend: deposit whichever thread is brighter per pixel.
            rgb[:, :, ci] = np.maximum(rgb[:, :, ci],
                                        np.maximum(on_layer, off_layer))

        ax.imshow(np.clip(rgb, 0, 1), origin="upper", aspect="auto",
                  interpolation="nearest")
        ax.set_ylabel(f"slit x={SLIT_CENTRE_X[s]}", color="#c8b89a", fontsize=9,
                      rotation=0, labelpad=60, va="center")
        ax.set_yticks([])
        ax.set_xticks([])
        ax.spines[:].set_edgecolor("#332d40")
        # Cursor line at the current weft column.
        ax.axvline(x=last_weft, color="#e0d060", linewidth=0.8, alpha=0.6)

    axes[-1].set_xlabel("weft (event-count time →)", color="#c8b89a", fontsize=8)
    fig.suptitle('"The Finish-Line Loom"', color="#e8dfc8", fontsize=12,
                 fontweight="bold", y=0.99)
    fig.tight_layout(rect=[0, 0, 1, 0.97])

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: deterministic self-tests covering slit attribution,
# off-slit silence, weft arithmetic, noise vs object discrimination, and
# word well-formedness.
# ---------------------------------------------------------------------------

def validate():
    ok = True
    rng = np.random.default_rng(42)

    # ------------------------------------------------------------------
    # (a) SLIT ATTRIBUTION: dense bar sweeping x=30..90 (crosses ONLY
    # slit 1 band 60..63; slits 0 and 2 are outside the sweep).
    # ------------------------------------------------------------------
    xs_a = []
    ys_a = []
    ps_a = []
    for xi in range(30, 91):
        n_here = 40
        for _ in range(n_here):
            ys_a.append(int(rng.integers(20, 91)))
            xs_a.append(xi)
            ps_a.append(int(rng.integers(0, 2)))
    xa = np.array(xs_a, dtype=np.int64)
    ya = np.array(ys_a, dtype=np.int64)
    pa = np.array(ps_a, dtype=np.int64)
    words_a = python_loom_words(xa, ya, pa)

    slit_samples_a = [unpack_status(w) for w in words_a if (w & 3) != 3]
    all_slit1 = all(s[0] == 1 for s in slit_samples_a)
    any_flagged = any(s[4] == 1 for s in slit_samples_a)
    no_slit02 = not any(s[0] in (0, 2) for s in slit_samples_a)

    a_ok = all_slit1 and any_flagged and no_slit02
    print(f"  (a) SLIT ATTRIBUTION: {len(xa)} events, {len(words_a)} words, "
          f"{len(slit_samples_a)} slit samples; "
          f"all slit==1={all_slit1}, any flag==1={any_flagged}, "
          f"no slit 0 or 2={no_slit02} -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) OFF-SLIT SILENCE: activity at x=36..52 (no slit in that range).
    #     xq=36>>2=9, 52>>2=13; SLIT_XQS are 5,15,25 -- none in 9..13.
    # ------------------------------------------------------------------
    xs_b = []
    ys_b = []
    ps_b = []
    for xi in range(36, 53):
        for _ in range(40):
            ys_b.append(int(rng.integers(0, SY)))
            xs_b.append(xi)
            ps_b.append(int(rng.integers(0, 2)))
    xb = np.array(xs_b, dtype=np.int64)
    yb = np.array(ys_b, dtype=np.int64)
    pb = np.array(ps_b, dtype=np.int64)
    words_b = python_loom_words(xb, yb, pb)

    all_sentinel = all((w & 3) == 3 for w in words_b)
    b_ok = all_sentinel
    print(f"  (b) OFF-SLIT SILENCE: {len(xb)} events, {len(words_b)} words; "
          f"all slit==3={all_sentinel} -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) WEFT ARITHMETIC: for every word index i in stream (a), the weft
    # field must equal (i // WEFT_BATCHES) & WEFT_MASK exactly.
    # ------------------------------------------------------------------
    weft_ok = True
    for i, word in enumerate(words_a):
        expected_weft = (i // WEFT_BATCHES) & WEFT_MASK
        _, _, _, actual_weft, _ = unpack_status(word)
        if actual_weft != expected_weft:
            weft_ok = False
            break
    c_ok = weft_ok
    print(f"  (c) WEFT ARITHMETIC: every word[i] has weft==(i//{WEFT_BATCHES})&0x7F -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) NOISE vs OBJECT: sparkle stream at slit 1 (x=61) cycling y over
    # 28 distinct 4-px-separated bins so no cell ever collects MIN_HITS=4
    # hits in a single weft step (64 events / 28 bins -> max 3 per cell).
    # CHECK: every slit sample is flag==0.  Contrast with stream (a).
    # ------------------------------------------------------------------
    # Build sparkle: 64 events per weft step (= WEFT_BATCHES*BATCH), cycling
    # y over bins 0,4,8,...,108 (28 bins).  Use enough steps to be sure.
    events_per_step = WEFT_BATCHES * BATCH  # 64
    n_steps = 20
    ybins = list(range(0, SY, 4))  # [0, 4, 8, ..., 108] -> 28 values
    xs_d = []
    ys_d = []
    ps_d = []
    for step in range(n_steps):
        for j in range(events_per_step):
            xs_d.append(61)
            ys_d.append(ybins[j % len(ybins)])
            ps_d.append(j % 2)
    xd = np.array(xs_d, dtype=np.int64)
    yd = np.array(ys_d, dtype=np.int64)
    pd = np.array(ps_d, dtype=np.int64)
    words_d = python_loom_words(xd, yd, pd)

    slit_samples_d = [unpack_status(w) for w in words_d if (w & 3) != 3]
    all_unflagged = all(s[4] == 0 for s in slit_samples_d)
    d_ok = all_unflagged
    # Restate that stream (a) produced at least one flag==1.
    print(f"  (d) NOISE vs OBJECT: sparkle stream -> {len(slit_samples_d)} slit samples, "
          f"all flag==0={all_unflagged}; "
          f"real object stream (a) had any flag==1={any_flagged} -> "
          f"{'OK (noise silent, object flagged)' if d_ok and any_flagged else 'FAIL'}")
    ok = ok and d_ok and any_flagged

    # ------------------------------------------------------------------
    # (e) WELL-FORMEDNESS: over all streams.
    # ------------------------------------------------------------------
    all_words = words_a + words_b + words_d
    well_ok = True
    bad = []
    for word in all_words:
        slit, y, pol, weft, flag = unpack_status(word)
        if slit not in (0, 1, 2, 3):
            bad.append(f"slit={slit}"); well_ok = False
        if y > 111:
            bad.append(f"y={y}"); well_ok = False
        if weft > 127:
            bad.append(f"weft={weft}"); well_ok = False
        if flag not in (0, 1):
            bad.append(f"flag={flag}"); well_ok = False
        if slit == 3:
            if y != 0 or pol != 0 or flag != 0:
                bad.append(f"sentinel corrupt y={y} pol={pol} flag={flag}")
                well_ok = False
    e_ok = well_ok
    print(f"  (e) WELL-FORMEDNESS: {len(all_words)} total words; "
          f"slit in {{0,1,2,3}}, y<=111, weft<=127, flag in {{0,1}}, "
          f"sentinel fields zeroed -> "
          f"{'OK' if e_ok else 'FAIL: ' + '; '.join(bad[:5])}")
    ok = ok and e_ok

    print()
    print("VALIDATION:", "PASS -- slit attribution exact; off-slit all-sentinel; "
          "weft arithmetic exact; noise-guard proven (sparkle=all-unflagged, "
          "object=flagged); word fields well-formed"
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
                    help="synthetic self-test: slit attribution, weft arithmetic, "
                         "noise vs object, well-formedness")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the loom PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing loom words in "
              f"Python (bit-faithful mirror of firmware).")
        words = python_loom_words(x, y, pol)
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_loom(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
