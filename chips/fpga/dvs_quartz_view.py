#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_quartz/main.c
("The Human Quartz" -- grades finger-TAP timing regularity like a crystal
oscillator, from burst timing only.  Every NTAPS inter-tap intervals it
computes mean ITI and MAD jitter, then awards a grade: QUARTZ (jit<=16),
METRONOME (jit<=64), MORTAL HAND (jit<=256), JELLY (worse).
Position-invariant: x/y/pol accepted to mirror the ABI but ignored.

Word layout: bits[3:0]=prog (live ITI count toward next grade, 0..15),
bits[14:4]=meanq (latched mean ITI>>5 clamp 2047),
bits[24:15]=jit (latched MAD jitter, clamp 1023),
bits[26:25]=grade (0=JELLY,1=MORTAL HAND,2=METRONOME,3=QUARTZ),
bits[30:27]=sseq (session counter, wraps; 0=no measurement yet),
bit[31]=0.

------------------------------------------------------------------------------
Usage:
  dvs_quartz_view.py --validate                  # synthetic self-test (numbers)
  dvs_quartz_view.py --from-actsim results.mem   # render real chip status words
  dvs_quartz_view.py events.csv --ts-col le      # render (host-computed) from a CSV
  dvs_quartz_view.py ... --headless --save quartz.png
"""
import argparse
import numpy as np

# --- must match software/dvs_quartz/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
TS_MASK = 0xFFFF
GAP_MIN = 48
BURST_MIN_LEN = 4
ITI_MIN = 512
ITI_MAX = 32768
NTAPS = 16
J_QUARTZ = 16
J_STEADY = 64
J_MORTAL = 256
MEANQ_MAX = 2047
JIT_MAX = 1023
SSEQ_MASK = 0xF
GRADE_NAMES = ["JELLY", "MORTAL HAND", "METRONOME", "QUARTZ"]


def python_quartz_words(x, y, ts, pol):
    """Bit-faithful port of software/dvs_quartz/main.c's ISR.

    x, y, pol are per-event arrays (x/y/pol accepted to mirror the ABI but
    ignored by the algorithm -- position-invariant).  ts is the per-event
    timestamp array.  Processes only complete batches (n - n%BATCH events).

    State cold-start all zeros:
      last_ts=0; cur_onset_ts=0; cur_len=0; prev_onset_ts=0; have_prev=0;
      iti=[0]*NTAPS; iti_count=0; lat_meanq=lat_jit=lat_grade=0; sseq=0.

    Per event in order:
      t = int(ts[i]) & TS_MASK
      dt = (t - last_ts) & TS_MASK; last_ts = t
      if dt >= GAP_MIN: cur_onset_ts = t; cur_len = 1
      else:
        if cur_len < 255: cur_len += 1
        if cur_len == BURST_MIN_LEN:            # tap confirmed exactly once
          if have_prev:
            iv = (cur_onset_ts - prev_onset_ts) & TS_MASK
            if ITI_MIN <= iv <= ITI_MAX:
              iti[iti_count] = iv; iti_count += 1
              if iti_count == NTAPS:
                s = sum(iti); mean = s >> 4
                madsum = sum(v - mean if v > mean else mean - v for v in iti)
                jit = madsum >> 4
                grade = (3 if jit <= J_QUARTZ else
                         2 if jit <= J_STEADY else
                         1 if jit <= J_MORTAL else 0)
                lat_meanq = min(mean >> 5, MEANQ_MAX)
                lat_jit = min(jit, JIT_MAX)
                lat_grade = grade
                sseq = (sseq + 1) & SSEQ_MASK
                iti_count = 0
                latches.append((grade, lat_jit, lat_meanq))
            else:
              iti_count = 0                     # tempo gate: reject + reset collection
          prev_onset_ts = cur_onset_ts; have_prev = 1

    After each batch of 4 events emit ONE word:
      word = (sseq << 27) | (lat_grade << 25) | (lat_jit << 15) | (lat_meanq << 4) | iti_count

    Returns (words, latches) where latches is a list of
    (grade, jit, meanq) tuples appended at every NTAPS-tap latch.
    """
    last_ts = 0
    cur_onset_ts = 0
    cur_len = 0
    prev_onset_ts = 0
    have_prev = 0
    iti = [0] * NTAPS
    iti_count = 0
    lat_meanq = 0
    lat_jit = 0
    lat_grade = 0
    sseq = 0
    words = []
    latches = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Process BATCH events: update burst tracker
        for i in range(b, b + BATCH):
            t = int(ts[i]) & TS_MASK
            dt = (t - last_ts) & TS_MASK
            last_ts = t
            if dt >= GAP_MIN:
                cur_onset_ts = t
                cur_len = 1
            else:
                if cur_len < 255:
                    cur_len += 1
                if cur_len == BURST_MIN_LEN:
                    if have_prev:
                        iv = (cur_onset_ts - prev_onset_ts) & TS_MASK
                        if ITI_MIN <= iv <= ITI_MAX:
                            iti[iti_count] = iv
                            iti_count += 1
                            if iti_count == NTAPS:
                                s = sum(iti)
                                mean = s >> 4
                                madsum = sum(
                                    v - mean if v > mean else mean - v
                                    for v in iti
                                )
                                jit = madsum >> 4
                                grade = (3 if jit <= J_QUARTZ else
                                         2 if jit <= J_STEADY else
                                         1 if jit <= J_MORTAL else 0)
                                lat_meanq = min(mean >> 5, MEANQ_MAX)
                                lat_jit = min(jit, JIT_MAX)
                                lat_grade = grade
                                sseq = (sseq + 1) & SSEQ_MASK
                                iti_count = 0
                                latches.append((grade, lat_jit, lat_meanq))
                        else:
                            iti_count = 0
                    prev_onset_ts = cur_onset_ts
                    have_prev = 1

        # Emit one word per batch
        word = ((sseq << 27) | (lat_grade << 25) | (lat_jit << 15)
                | (lat_meanq << 4) | iti_count)
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one quartz status word.

    bits[3:0]=prog, bits[14:4]=meanq, bits[24:15]=jit,
    bits[26:25]=grade, bits[30:27]=sseq, bit[31]=0.
    """
    prog  =  word        & 0xF
    meanq = (word >>  4) & 0x7FF
    jit   = (word >> 15) & 0x3FF
    grade = (word >> 25) & 0x3
    sseq  = (word >> 27) & 0xF
    return prog, meanq, jit, grade, sseq


# ---------------------------------------------------------------------------
# Synthetic stream builder
# ---------------------------------------------------------------------------

def build_tap_stream(periods, n_taps, burst_len=8, intra_dt=2, t0=1000):
    """Build a synthetic tap stream with known inter-tap intervals.

    Onset times: onset[0]=t0; onset[k]=onset[k-1]+periods[(k-1)%len(periods)].
    Each tap k contributes burst_len events at ts onset[k] + j*intra_dt
    for j in 0..burst_len-1 (full-precision ints; the mirror applies TS_MASK).
    x=63, y=56, pol=j%2 throughout (irrelevant to the algorithm).

    Returns int64 numpy arrays (x, y, ts, pol).
    """
    t_onset = [t0]
    for k in range(1, n_taps):
        t_onset.append(t_onset[k - 1] + periods[(k - 1) % len(periods)])
    xs = []
    ys = []
    tss = []
    pols = []
    for k in range(n_taps):
        for j in range(burst_len):
            xs.append(63)
            ys.append(56)
            tss.append(t_onset[k] + j * intra_dt)
            pols.append(j % 2)
    return (np.array(xs,   dtype=np.int64),
            np.array(ys,   dtype=np.int64),
            np.array(tss,  dtype=np.int64),
            np.array(pols, dtype=np.int64))


# ---------------------------------------------------------------------------
# Renderer: quartz-crystal aesthetic on dark backdrop.
# ---------------------------------------------------------------------------

BG     = "#0d0b10"
TEXT   = "#e8dfc8"
GOLD   = "#e8b84b"
INDIGO = "#5a5fd4"
GREEN  = "#5fd48a"
STEEL  = "#8a94a6"
DIM    = "#555566"

GRADE_COLORS = [DIM, STEEL, INDIGO, GOLD]


def render_quartz(words, save=None, headless=False):
    """Compose one figure: crystal glyph, jitter history, tempo history."""
    if not words:
        print("no words to render")
        return

    prog_last, meanq_last, jit_last, grade_last, sseq_last = unpack_status(words[-1])

    # Collect one sample per sseq change
    history_jit   = []
    history_meanq = []
    history_grade = []
    prev_sseq = None
    for word in words:
        prog_, meanq_, jit_, grade_, sseq_ = unpack_status(word)
        if sseq_ != prev_sseq:
            history_jit.append(jit_)
            history_meanq.append(meanq_)
            history_grade.append(grade_)
            prev_sseq = sseq_

    # Drop the sseq=0 "no measurement yet" entry (it holds latched zeros)
    # Only the first entry where sseq_=0 is the cold-start; subsequent
    # sseq changes are real measurements.  We'll annotate instead.

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.patches import Polygon
        import matplotlib.patheffects as pe
    except Exception as e:
        print("matplotlib unavailable:", e)
        grade_name = GRADE_NAMES[grade_last]
        tempo = meanq_last << 5
        print(f"last grade={grade_name} jitter={jit_last} ticks "
              f"tempo={tempo} ticks sseq={sseq_last} prog={prog_last}")
        if history_jit:
            print("per-session jitter history:", history_jit)
            print("per-session tempo history:", [m << 5 for m in history_meanq])
        return

    fig = plt.figure(figsize=(10, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.6], wspace=0.38,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_left  = fig.add_subplot(gs[0])
    ax_right = fig.add_subplot(gs[1])

    # Split right column: jitter history top, tempo history bottom
    gs_right = gs[1].subgridspec(2, 1, hspace=0.55)
    ax_jit   = fig.add_subplot(gs_right[0])
    ax_tempo = fig.add_subplot(gs_right[1])
    ax_right.remove()

    for ax in (ax_left, ax_jit, ax_tempo):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#332d40")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left panel: diamond/crystal glyph coloured by latched grade ---
    ax_left.set_xlim(-1.4, 1.4)
    ax_left.set_ylim(-1.8, 1.4)
    ax_left.set_aspect("equal")
    ax_left.set_xticks([])
    ax_left.set_yticks([])
    ax_left.set_title("crystal grade", color=TEXT, fontsize=9, pad=4)

    if sseq_last == 0:
        # No measurement yet — grey outline only
        diamond_color = BG
        outline_color = STEEL
        grade_text = "no measurement yet"
    else:
        diamond_color = GRADE_COLORS[grade_last]
        outline_color = TEXT
        grade_text = GRADE_NAMES[grade_last]

    # Diamond shape
    diamond_pts = np.array([[0, 0.95], [0.65, 0], [0, -0.75], [-0.65, 0]])
    diamond = Polygon(diamond_pts, closed=True, facecolor=diamond_color,
                      edgecolor=outline_color, linewidth=2.0, zorder=2)
    ax_left.add_patch(diamond)

    # Inner facet lines for crystal look
    if sseq_last != 0:
        facet_color = "#ffffff30"
        ax_left.plot([0, 0.65], [0.95, 0], color=facet_color, lw=0.8, zorder=3)
        ax_left.plot([0, -0.65], [0.95, 0], color=facet_color, lw=0.8, zorder=3)
        ax_left.plot([-0.65, 0.65], [0, 0], color=facet_color, lw=0.5, zorder=3)

    ax_left.text(0, -1.00, grade_text,
                 ha="center", va="center", color=TEXT,
                 fontsize=11, fontweight="bold")

    if sseq_last != 0:
        tempo_last = meanq_last << 5
        ax_left.text(0, -1.35,
                     f"jitter={jit_last} ticks,  tempo={tempo_last} ticks",
                     ha="center", va="center", color=TEXT, fontsize=8)

    # 16-pip progress row for live prog field
    pip_y = -1.65
    pip_xs = np.linspace(-0.90, 0.90, NTAPS)
    for pi, px in enumerate(pip_xs):
        c = GOLD if pi < prog_last else DIM
        ax_left.plot(px, pip_y, "o", color=c, markersize=5, zorder=3)
    ax_left.text(0, -1.82, f"progress: {prog_last}/{NTAPS} ITIs",
                 ha="center", va="center", color=STEEL, fontsize=7)

    # --- Right top: per-session jitter history (log-scaled y) ---
    ax_jit.set_yscale("log")
    ax_jit.axhline(J_QUARTZ, color=GOLD,   linewidth=0.8, linestyle="--", alpha=0.7,
                   label=f"J_QUARTZ={J_QUARTZ}")
    ax_jit.axhline(J_STEADY, color=INDIGO, linewidth=0.8, linestyle="--", alpha=0.7,
                   label=f"J_STEADY={J_STEADY}")
    ax_jit.axhline(J_MORTAL, color=STEEL,  linewidth=0.8, linestyle="--", alpha=0.7,
                   label=f"J_MORTAL={J_MORTAL}")

    # Only the real measurements (sseq>0 samples)
    real_jit   = history_jit[1:]   if len(history_jit)   > 1 else []
    real_grade = history_grade[1:] if len(history_grade) > 1 else []
    if real_jit:
        xs = list(range(1, len(real_jit) + 1))
        jit_colors = [GRADE_COLORS[g] for g in real_grade]
        ax_jit.scatter(xs, [max(j, 1) for j in real_jit],
                       color=jit_colors, s=20, zorder=3)
        ax_jit.step(xs, [max(j, 1) for j in real_jit],
                    where="post", color=TEXT, linewidth=0.8, alpha=0.6)

    ax_jit.set_xlabel("session index", color=TEXT, fontsize=8)
    ax_jit.set_ylabel("MAD jitter (ticks, log)", color=TEXT, fontsize=8)
    ax_jit.set_title("per-session jitter history", color=TEXT, fontsize=9, pad=4)
    ax_jit.legend(fontsize=7, framealpha=0.2,
                  labelcolor=TEXT, facecolor=BG, edgecolor="#332d40")

    # --- Right bottom: per-session tempo (meanq<<5) history ---
    real_tempo = [(m << 5) for m in history_meanq[1:]] if len(history_meanq) > 1 else []
    real_grade2 = history_grade[1:] if len(history_grade) > 1 else []
    if real_tempo:
        xs = list(range(1, len(real_tempo) + 1))
        tempo_colors = [GRADE_COLORS[g] for g in real_grade2]
        ax_tempo.scatter(xs, real_tempo, color=tempo_colors, s=20, zorder=3)
        ax_tempo.step(xs, real_tempo, where="post",
                      color=TEXT, linewidth=0.8, alpha=0.6)

    ax_tempo.set_xlabel("session index", color=TEXT, fontsize=8)
    ax_tempo.set_ylabel("mean ITI (ticks)", color=TEXT, fontsize=8)
    ax_tempo.set_title("per-session tempo history", color=TEXT, fontsize=9, pad=4)

    fig.suptitle('"The Human Quartz"', color=TEXT, fontsize=12,
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
    """Run lettered validation checks against pre-computed expected values.

    Expected numbers were hand-derived and independently review-verified.
    If the mirror disagrees with any of them the mirror is wrong -- never
    adjust the expectations.
    """
    ok = True

    # ------------------------------------------------------------------
    # (a) METRONOME EXACT
    # build_tap_stream([2000], 60) -> 480 events, 120 words, 3 latches,
    # every latch == (3, 0, 62)  [mean=2000, jit=0, grade=QUARTZ, meanq=62].
    # Word-index structure (0-based):
    #   word[31]: prog=15, sseq=0
    #   word[32]: prog=0, sseq=1, meanq=62, jit=0, grade=3
    #   word[64]: sseq=2;  word[96]: sseq=3;  word[119]: prog=11, sseq=3
    #   For j in 1..15: words 2j and 2j+1 both have prog=j and sseq=0.
    #   sseq blocks: words 0..31 sseq=0, 32..63 sseq=1,
    #                64..95 sseq=2, 96..119 sseq=3.
    # ------------------------------------------------------------------
    x_a, y_a, ts_a, pol_a = build_tap_stream([2000], 60)
    words_a, latches_a = python_quartz_words(x_a, y_a, ts_a, pol_a)

    n_ev_a  = len(x_a)
    n_wd_a  = len(words_a)
    n_lat_a = len(latches_a)

    all_latches_a = all(latch == (3, 0, 62) for latch in latches_a)

    # word[31]: prog=15, sseq=0
    p31, mq31, jt31, gr31, sq31 = unpack_status(words_a[31])
    w31_ok = (p31 == 15 and sq31 == 0)

    # word[32]: prog=0, sseq=1, meanq=62, jit=0, grade=3
    p32, mq32, jt32, gr32, sq32 = unpack_status(words_a[32])
    w32_ok = (p32 == 0 and sq32 == 1 and mq32 == 62 and jt32 == 0 and gr32 == 3)

    # word[64]: sseq=2
    sq64 = (words_a[64] >> 27) & 0xF
    w64_ok = (sq64 == 2)

    # word[96]: sseq=3
    sq96 = (words_a[96] >> 27) & 0xF
    w96_ok = (sq96 == 3)

    # word[119]: prog=11, sseq=3
    p119, _, _, _, sq119 = unpack_status(words_a[119])
    w119_ok = (p119 == 11 and sq119 == 3)

    # For j in 1..15: words 2j and 2j+1 have prog=j and sseq=0
    jblock_ok = all(
        (words_a[2*j] & 0xF) == j and ((words_a[2*j] >> 27) & 0xF) == 0 and
        (words_a[2*j+1] & 0xF) == j and ((words_a[2*j+1] >> 27) & 0xF) == 0
        for j in range(1, 16)
    )

    # sseq blocks: 0..31->sseq=0, 32..63->sseq=1, 64..95->sseq=2, 96..119->sseq=3
    sseq_blocks_a = (
        all((words_a[i] >> 27) & 0xF == 0 for i in range(0,  32)) and
        all((words_a[i] >> 27) & 0xF == 1 for i in range(32, 64)) and
        all((words_a[i] >> 27) & 0xF == 2 for i in range(64, 96)) and
        all((words_a[i] >> 27) & 0xF == 3 for i in range(96, 120))
    )

    a_ok = (
        n_ev_a == 480
        and n_wd_a == 120
        and n_lat_a == 3
        and all_latches_a
        and w31_ok
        and w32_ok
        and w64_ok
        and w96_ok
        and w119_ok
        and jblock_ok
        and sseq_blocks_a
    )
    print(f"  (a) METRONOME EXACT: events={n_ev_a} (want 480), "
          f"words={n_wd_a} (want 120), latches={n_lat_a} (want 3), "
          f"all_latches==(3,0,62)={all_latches_a}, "
          f"w31(prog=15,sseq=0)={w31_ok}, "
          f"w32(prog=0,sseq=1,mq=62,jit=0,g=3)={w32_ok}, "
          f"w64(sseq=2)={w64_ok}, w96(sseq=3)={w96_ok}, "
          f"w119(prog=11,sseq=3)={w119_ok}, "
          f"j-blocks={jblock_ok}, sseq-blocks={sseq_blocks_a} -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) GRADE LADDER EXACT
    # Four independent streams, each build_tap_stream([pA,pB], 17)
    # (17 taps -> exactly 16 ITIs, 8 of each period -> exactly 1 latch;
    # 136 events -> 34 words; latch lands in batch 32 -> word[32]):
    #   [1900,2100] -> latch (1, 100, 62)   MORTAL HAND
    #   [1984,2016] -> latch (3, 16, 62)    QUARTZ (boundary jit==J_QUARTZ)
    #   [1950,2050] -> latch (2, 50, 62)    METRONOME
    #   [1000,3000] -> latch (0, 1000, 62)  JELLY; jit=1000 NOT clamped
    # In each: words 32..33 carry sseq=1 and latch fields; words 0..31 sseq=0.
    # ------------------------------------------------------------------
    cases_b = [
        ([1900, 2100], (1, 100, 62),  "MORTAL HAND"),
        ([1984, 2016], (3,  16, 62),  "QUARTZ boundary"),
        ([1950, 2050], (2,  50, 62),  "METRONOME"),
        ([1000, 3000], (0, 1000, 62), "JELLY (jit=1000 not clamped)"),
    ]

    b_ok = True
    for periods_b, expected_latch, label in cases_b:
        xb, yb, tsb, polb = build_tap_stream(periods_b, 17)
        words_b, latches_b = python_quartz_words(xb, yb, tsb, polb)

        got_latch = latches_b[0] if latches_b else None
        n_latches_b = len(latches_b)
        n_words_b = len(words_b)

        # words 32..33 carry sseq=1
        sq32b = (words_b[32] >> 27) & 0xF if n_words_b > 32 else None
        sq33b = (words_b[33] >> 27) & 0xF if n_words_b > 33 else None
        sseq1_ok = (sq32b == 1 and sq33b == 1)

        # words 0..31 carry sseq=0
        sseq0_ok = all((words_b[i] >> 27) & 0xF == 0 for i in range(32))

        this_ok = (
            n_latches_b == 1
            and got_latch == expected_latch
            and n_words_b == 34
            and sseq1_ok
            and sseq0_ok
        )
        print(f"  (b) GRADE LADDER [{label}]: "
              f"words={n_words_b} (want 34), latches={n_latches_b} (want 1), "
              f"latch={got_latch} (want {expected_latch}), "
              f"sseq0_ok={sseq0_ok}, sseq1_ok={sseq1_ok} -> "
              f"{'OK' if this_ok else 'FAIL'}")
        b_ok = b_ok and this_ok
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) DENSE SPARKLE GUARD
    # 480 events, x=63,y=56,pol=0, ts=1000+8*i -> every word == 0.
    # (Cold-start phantom burst confirms once with have_prev=0, anchors,
    # and no gap ever recurs.)
    # ------------------------------------------------------------------
    x_c   = np.full(480, 63, dtype=np.int64)
    y_c   = np.full(480, 56, dtype=np.int64)
    ts_c  = np.array([1000 + 8 * i for i in range(480)], dtype=np.int64)
    pol_c = np.zeros(480, dtype=np.int64)
    words_c, latches_c = python_quartz_words(x_c, y_c, ts_c, pol_c)

    all_zero_c = all(w == 0 for w in words_c)
    c_ok = all_zero_c and latches_c == []
    print(f"  (c) DENSE SPARKLE GUARD: words={len(words_c)} (want 120), "
          f"all_zero={all_zero_c}, latches={latches_c} (want []) -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) SINGLETON GUARD
    # 480 events, ts=1000+5000*i -> every word == 0.
    # (Every event opens a fresh 1-event burst, never confirms.)
    # ------------------------------------------------------------------
    ts_d  = np.array([1000 + 5000 * i for i in range(480)], dtype=np.int64)
    words_d, latches_d = python_quartz_words(x_c, y_c, ts_d, pol_c)

    all_zero_d = all(w == 0 for w in words_d)
    d_ok = all_zero_d and latches_d == []
    print(f"  (d) SINGLETON GUARD: words={len(words_d)} (want 120), "
          f"all_zero={all_zero_d}, latches={latches_d} (want []) -> "
          f"{'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) TEMPO GATE
    # [100]   -> ITI=100  < ITI_MIN rejected, all words==0, zero latches.
    # [40000] -> masked ITI=40000 > ITI_MAX rejected, all words==0.
    # [65536] -> masked ITI=0 < ITI_MIN rejected, all words==0.
    # ------------------------------------------------------------------
    e_ok = True
    for periods_e, label_e in [([100], "ITI=100<ITI_MIN"),
                                ([40000], "ITI=40000>ITI_MAX"),
                                ([65536], "masked ITI=0<ITI_MIN")]:
        xe, ye, tse, pole = build_tap_stream(periods_e, 60)
        words_e, latches_e = python_quartz_words(xe, ye, tse, pole)
        all_zero_e = all(w == 0 for w in words_e)
        zero_lat_e = (latches_e == [])
        this_e = all_zero_e and zero_lat_e
        print(f"  (e) TEMPO GATE [{label_e}]: "
              f"all_zero={all_zero_e}, latches={latches_e} (want []) -> "
              f"{'OK' if this_e else 'FAIL'}")
        e_ok = e_ok and this_e
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) POSITION INVARIANCE
    # Rerun (a)'s ts with x2=(x*37+11)%126, y2=(y*23+5)%112, pol2=1-pol
    # -> words identical element-for-element to (a)'s.
    # ------------------------------------------------------------------
    x_f   = (x_a * 37 + 11) % 126
    y_f   = (y_a * 23 + 5)  % 112
    pol_f = 1 - pol_a
    words_f, _ = python_quartz_words(x_f, y_f, ts_a, pol_f)

    f_ok = list(words_f) == list(words_a)
    print(f"  (f) POSITION INVARIANCE: words identical element-for-element to (a) -> "
          f"{'OK' if f_ok else 'FAIL'}")
    ok = ok and f_ok

    # ------------------------------------------------------------------
    # (g) WELL-FORMEDNESS over ALL words from (a)-(e)
    # prog<=15, meanq<=2047, jit<=1023, grade<=3, sseq<=15,
    # (word>>31)==0, word < 2**32.
    # ------------------------------------------------------------------
    # Collect all words from (a)-(e)
    xe1, ye1, tse1, pole1 = build_tap_stream([100], 60)
    we1, _ = python_quartz_words(xe1, ye1, tse1, pole1)
    xe2, ye2, tse2, pole2 = build_tap_stream([40000], 60)
    we2, _ = python_quartz_words(xe2, ye2, tse2, pole2)
    xe3, ye3, tse3, pole3 = build_tap_stream([65536], 60)
    we3, _ = python_quartz_words(xe3, ye3, tse3, pole3)

    all_words_g = (list(words_a)
                   + [w for periods_b, _, _ in cases_b
                      for w in python_quartz_words(
                          *build_tap_stream(periods_b, 17))[0]]
                   + list(words_c)
                   + list(words_d)
                   + list(we1) + list(we2) + list(we3))

    bad_g = []
    for word in all_words_g:
        prog_, meanq_, jit_, grade_, sseq_ = unpack_status(word)
        if prog_ > 15:
            bad_g.append(f"prog={prog_}")
        if meanq_ > 2047:
            bad_g.append(f"meanq={meanq_}")
        if jit_ > 1023:
            bad_g.append(f"jit={jit_}")
        if grade_ > 3:
            bad_g.append(f"grade={grade_}")
        if sseq_ > 15:
            bad_g.append(f"sseq={sseq_}")
        if (word >> 31) != 0:
            bad_g.append(f"bit31 set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_g.append(f"word>=2^32: 0x{word:08x}")
        if bad_g:
            break
    g_ok = len(bad_g) == 0
    print(f"  (g) WELL-FORMEDNESS: {len(all_words_g)} total words; "
          f"prog<=15, meanq<=2047, jit<=1023, grade<=3, "
          f"sseq<=15, bit31=0, word<2^32 -> "
          f"{'OK' if g_ok else 'FAIL: ' + '; '.join(bad_g[:5])}")
    ok = ok and g_ok

    print()
    print("VALIDATION:", "PASS -- metronome exact; grade ladder exact; "
          "dense sparkle guard proven; singleton guard proven; "
          "tempo gate proven; position invariance proven; "
          "word fields well-formed"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def load_csv(path, ts_col):
    """Load event CSV with columns x, y, pol and optional timestamp column.

    ts_col: column name for the timestamp field (default 'le').
    NOTE: recorded captures carry a wrapped coarse counter (not real
    microseconds) so grades on captures are qualitative -- --validate
    builds its own synthetic timestamps and is the deterministic check.
    If ts_col is absent from the header, ts is zeroed.
    """
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x   = np.array([int(row[idx["x"]])   for row in rows], dtype=np.int64)
    y   = np.array([int(row[idx["y"]])   for row in rows], dtype=np.int64)
    pol = np.array([int(row[idx["pol"]]) for row in rows], dtype=np.int64)
    if ts_col in idx:
        ts = np.array([int(row[idx[ts_col]]) & TS_MASK for row in rows], dtype=np.int64)
    else:
        ts = np.zeros(len(x), dtype=np.int64)
    return x, y, ts, pol


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: metronome exact, grade ladder exact, "
                         "dense sparkle guard, singleton guard, tempo gate, "
                         "position invariance, well-formedness")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le -- NOTE: le is a "
                         "wrapped coarse counter, not real microseconds; grades on "
                         "recorded captures are qualitative; --validate builds its own "
                         "synthetic timestamps and is the deterministic check)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the quartz PNG here")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} real chip status words from {args.from_actsim}")
    elif args.csv:
        x, y, ts, pol = load_csv(args.csv, args.ts_col)
        print(f"loaded {len(x)} events from {args.csv}; computing quartz words in Python "
              f"(bit-faithful mirror of firmware).")
        words, _ = python_quartz_words(x, y, ts, pol)
        if words:
            prog_last, meanq_last, jit_last, grade_last, sseq_last = unpack_status(words[-1])
            tempo_last = meanq_last << 5
            print(f"final grade={GRADE_NAMES[grade_last]}, "
                  f"jitter={jit_last} ticks, tempo={tempo_last} ticks, "
                  f"sseq={sseq_last} ({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_quartz(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
