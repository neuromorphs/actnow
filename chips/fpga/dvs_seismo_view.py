#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_seismo/main.c
("Ballroom Seismology" -- the camera stares at a fixed high-contrast vertical
edge; sub-pixel motion of that edge reveals slow oscillation (a building
swaying, a table vibrating).  Over a thin vertical-edge ROI, the signed
polarity sum per time-bin (ON=+1, OFF=-1) approximates the derivative of edge
displacement; integrate it into a displacement proxy D with a slow leak
(D -= D>>LEAK_K); estimate the oscillation frequency by counting sign
zero-crossings of D over a window and mapping the count to a frequency label
via a small LUT; a resonance meter = leaky sum of |D| (conditional-negate +
add).  All shift/add/sub/compare/LUT, NO multiply.

python_seismo_words() below is a bit-faithful port of the firmware's integer
logic (same roi_sum accumulator, same integrator, same zero-crossing counter,
same FREQ_LUT, same word packing) so what we emit is provably what the chip
would emit given the same event stream.

Word layout: bits[7:0]=disp_q (signed 8-bit, two's complement),
bits[12:8]=freqbin (0..31), bits[22:13]=resonance_q (0..1023),
bits[26:23]=seq (4-bit window sequence counter, wraps mod 16),
bits[31:27]=0.

disp_q is stored as unsigned uint8_t two's-complement; host sign-extends:
  disp_signed = int8_t(disp_q)  -- or in Python: (disp_q ^ 0x80) - 0x80

------------------------------------------------------------------------------
Usage:
  dvs_seismo_view.py --validate                   # synthetic self-test
  dvs_seismo_view.py --from-actsim results.mem    # render real chip status words
  dvs_seismo_view.py events.csv                   # render (host-computed) from a CSV
  dvs_seismo_view.py events.csv --headless --save seismo.png
"""
import argparse
import numpy as np

# --- must match software/dvs_seismo/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
X_ROI_LO = 55
X_ROI_HI = 70
LEAK_K = 6
RES_LEAK = 5
RES_CAP = 0xFFFFFF
MIN_RES_VALID = 64
DISP_SHIFT = 3
RES_SHIFT = 0
WINDOW_BATCHES = 256
SEQ_MASK = 0xF
DISP_CLAMP = 16384

# Frequency LUT: FREQ_LUT[i] = freqbin for i = min(zc_count >> 1, 15).
# Monotone non-decreasing; 0 = no oscillation detected.
# Must match software/dvs_seismo/main.c's FREQ_LUT[] exactly.
FREQ_LUT = [0, 4, 7, 9, 11, 13, 15, 16, 17, 19, 21, 23, 25, 27, 29, 31]


def _clamp(v, lo, hi):
    """Saturate v to [lo, hi]."""
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _as_int8(v):
    """Clamp int to [-128, 127] (mirrors C cast chain (int8_t)(int32_t))."""
    return _clamp(v, -128, 127)


def _pack_disp_u8(dq):
    """Two's-complement 8-bit packing: same as C's (uint8_t)(int8_t)dq."""
    return dq & 0xFF


def python_seismo_words(x, y, pol):
    """Bit-faithful port of software/dvs_seismo/main.c's ISR.

    x, y, pol are per-event integer arrays (ts is not used by this algorithm;
    y is decoded per ABI but ignored).  Processes only complete batches
    (n - n%BATCH events).

    State cold-start all zeros:
      disp=0; resonance=0; prev_sign=1 (>=0 branch); zc_count=0;
      batch_in_window=0; seq=0;
      lat_disp_u8=0; lat_freqbin=0; lat_resonance_q=0

    Per batch of BATCH events:
      roi_sum = sum(+1 if pol and X_ROI_LO<=x<=X_ROI_HI else
                   -1 if not pol and X_ROI_LO<=x<=X_ROI_HI else 0)
      disp += roi_sum
      disp -= disp >> LEAK_K
      disp = clamp(disp, -16384, 16384)
      abs_disp = abs(disp)
      resonance += abs_disp; resonance -= resonance >> RES_LEAK
      resonance = clamp(resonance, 0, RES_CAP)
      cur_sign = 1 if disp >= 0 else 0
      if cur_sign != prev_sign: zc_count += 1
      prev_sign = cur_sign

    After each batch (latch BEFORE emit):
      batch_in_window += 1
      if batch_in_window >= WINDOW_BATCHES:
        latch, clear zc_count, increment seq

    Emit one word per batch:
      word = (seq<<23) | (resonance_q<<13) | (freqbin<<8) | disp_u8

    Returns (words, latches) where latches is a list of
    (disp_q_signed, freqbin, resonance_q, seq_at_latch) tuples.
    """
    # Cold start -- note: prev_sign=1 (disp=0 is >= 0)
    disp = 0
    resonance = 0
    prev_sign = 1          # disp>=0 branch
    zc_count = 0
    batch_in_window = 0
    seq = 0
    lat_disp_u8 = 0
    lat_freqbin = 0
    lat_resonance_q = 0
    words = []
    latches = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Accumulate signed polarity sum over ROI
        roi_sum = 0
        for i in range(b, b + BATCH):
            xi = int(x[i])
            pi = int(pol[i])
            if X_ROI_LO <= xi <= X_ROI_HI:
                roi_sum += 1 if pi != 0 else -1

        # Integrate and leak
        disp += roi_sum
        disp -= disp >> LEAK_K          # arithmetic right shift (Python int)
        disp = _clamp(disp, -DISP_CLAMP, DISP_CLAMP)

        # Resonance: leaky |disp|
        abs_disp = disp if disp >= 0 else -disp
        resonance += abs_disp
        resonance -= resonance >> RES_LEAK
        resonance = _clamp(resonance, 0, RES_CAP)

        # Zero-crossing
        cur_sign = 1 if disp >= 0 else 0
        if cur_sign != prev_sign:
            zc_count += 1
        prev_sign = cur_sign

        # Window boundary (latch BEFORE emit)
        batch_in_window += 1
        if batch_in_window >= WINDOW_BATCHES:
            batch_in_window = 0

            dq = disp >> DISP_SHIFT
            dq = _as_int8(dq)
            lat_disp_u8 = _pack_disp_u8(dq)

            lut_idx = zc_count >> 1
            if lut_idx > 15:
                lut_idx = 15
            if resonance >= MIN_RES_VALID:
                lat_freqbin = FREQ_LUT[lut_idx]
            else:
                lat_freqbin = 0

            rq = resonance >> RES_SHIFT
            if rq > 1023:
                rq = 1023
            lat_resonance_q = rq

            latches.append((dq, lat_freqbin, lat_resonance_q, seq + 1))
            zc_count = 0
            seq = (seq + 1) & SEQ_MASK

        # Emit one word per batch (latched fields)
        word = (seq << 23) | (lat_resonance_q << 13) | (lat_freqbin << 8) | lat_disp_u8
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one seismo status word.

    bits[7:0]=disp_u8 (uint8 two's-comp; sign-extend to get signed displacement)
    bits[12:8]=freqbin (0..31)
    bits[22:13]=resonance_q (0..1023)
    bits[26:23]=seq (0..15)
    bits[31:27]=0
    """
    disp_u8      =  word        & 0xFF
    freqbin      = (word >>  8) & 0x1F
    resonance_q  = (word >> 13) & 0x3FF
    seq          = (word >> 23) & 0xF
    # Sign-extend disp_u8
    disp_signed  = (disp_u8 ^ 0x80) - 0x80
    return disp_signed, freqbin, resonance_q, seq


# ---------------------------------------------------------------------------
# Synthetic stream builder
# ---------------------------------------------------------------------------

def build_sine_edge_stream(n_events, freq_hz, event_rate_hz,
                           roi_lo=X_ROI_LO, roi_hi=X_ROI_HI,
                           amplitude=4, center_x=None, seed=42):
    """Build a synthetic event stream simulating a sinusoidally oscillating edge.

    The edge is centred at center_x (default: midpoint of ROI).  At each event
    the edge position is:
      edge_x(t) = center_x + amplitude * sin(2*pi*freq_hz * t / event_rate_hz)
    Events near the edge fire with polarity determined by whether the edge moved
    right (leading pixels -> ON) or left (trailing pixels -> OFF).  Events
    outside the ROI are ignored by the firmware anyway; we put all events inside
    for maximum SNR in the test.

    Returns (x, y, pol) integer numpy arrays of length n_events.
    n_events is rounded down to the nearest BATCH.
    """
    rng = np.random.default_rng(seed)
    n_events = (n_events // BATCH) * BATCH
    t = np.arange(n_events, dtype=np.float64)
    phase = 2 * np.pi * freq_hz * t / event_rate_hz
    # Edge x position (float)
    edge = (center_x if center_x is not None else (roi_lo + roi_hi) / 2.0) + \
           amplitude * np.sin(phase)
    # Edge velocity (derivative of position, sign = polarity signal)
    vel = amplitude * 2 * np.pi * freq_hz / event_rate_hz * np.cos(phase)

    # Place events at the edge pixel (rounded); polarity follows velocity sign
    x_arr = np.clip(np.round(edge).astype(np.int64), roi_lo, roi_hi)
    pol_arr = (vel >= 0).astype(np.int64)  # moving right -> ON; left -> OFF

    # y is irrelevant; place in middle of sensor
    y_arr = np.full(n_events, SY // 2, dtype=np.int64)

    return x_arr, y_arr, pol_arr


def build_static_edge_stream(n_events, center_x=None, roi_lo=X_ROI_LO,
                              roi_hi=X_ROI_HI, seed=7):
    """Build a synthetic event stream for a static edge (balanced ON/OFF, no net motion).

    Each event pair alternates ON/OFF at the edge centre so roi_sum per batch
    is near 0.  n_events rounded to nearest BATCH.
    """
    n_events = (n_events // BATCH) * BATCH
    cx = center_x if center_x is not None else (roi_lo + roi_hi) // 2
    x_arr   = np.full(n_events, cx, dtype=np.int64)
    y_arr   = np.full(n_events, SY // 2, dtype=np.int64)
    pol_arr = np.array([i % 2 for i in range(n_events)], dtype=np.int64)
    return x_arr, y_arr, pol_arr


def build_outside_roi_stream(n_events, seed=13):
    """Build a stream where all events are outside the ROI column band."""
    n_events = (n_events // BATCH) * BATCH
    x_arr   = np.zeros(n_events, dtype=np.int64)   # column 0, outside ROI
    y_arr   = np.full(n_events, SY // 2, dtype=np.int64)
    pol_arr = np.ones(n_events, dtype=np.int64)
    return x_arr, y_arr, pol_arr


# ---------------------------------------------------------------------------
# Renderer: seismograph strip chart + resonance bar.
# ---------------------------------------------------------------------------

def render_seismo(words, save=None, headless=False):
    """Compose one figure: seismograph strip chart (disp) + resonance bar
    + freqbin indicator."""
    if not words:
        print("no words to render")
        return

    disp_trace = []
    freq_trace = []
    res_trace  = []
    prev_seq   = None
    for word in words:
        d, fb, rq, sq = unpack_status(word)
        if sq != prev_seq:
            disp_trace.append(d)
            freq_trace.append(fb)
            res_trace.append(rq)
            prev_seq = sq

    last_d, last_fb, last_rq, last_sq = unpack_status(words[-1])

    BG     = "#0b0d12"
    TEXT   = "#d8e0ec"
    TEAL   = "#4fc4c4"
    AMBER  = "#e8a83a"
    RED    = "#e05050"
    STEEL  = "#7a8494"
    DIM    = "#2a2e38"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last: disp_q={last_d}, freqbin={last_fb}, resonance_q={last_rq}, seq={last_sq}")
        return

    fig = plt.figure(figsize=(12, 7))
    fig.patch.set_facecolor(BG)
    fig.suptitle('"Ballroom Seismology"  dvs_seismo', color=TEXT, fontsize=13,
                 fontweight="bold", y=0.97)

    gs = gridspec.GridSpec(3, 1, figure=fig, hspace=0.52,
                           top=0.89, bottom=0.09, left=0.10, right=0.96)
    ax_disp  = fig.add_subplot(gs[0])
    ax_freq  = fig.add_subplot(gs[1])
    ax_res   = fig.add_subplot(gs[2])

    for ax in (ax_disp, ax_freq, ax_res):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor(DIM)
        ax.tick_params(colors=STEEL, labelsize=8)
        for spine in ax.spines.values():
            spine.set_linewidth(0.6)

    xs = list(range(len(disp_trace)))

    # --- Strip chart: displacement proxy ---
    ax_disp.axhline(0, color=DIM, linewidth=0.8)
    if disp_trace:
        ax_disp.plot(xs, disp_trace, color=TEAL, linewidth=1.0)
        ax_disp.fill_between(xs, disp_trace, 0,
                             color=TEAL, alpha=0.18)
    ax_disp.set_xlim(0, max(1, len(disp_trace) - 1))
    ax_disp.set_ylim(-140, 140)
    ax_disp.set_ylabel("disp_q (LSB)", color=STEEL, fontsize=8)
    ax_disp.set_title("edge displacement proxy (seismograph)", color=TEXT, fontsize=9, pad=4)
    ax_disp.set_xlabel("window index", color=STEEL, fontsize=7)

    # --- Frequency indicator ---
    if freq_trace:
        fb_colors = [AMBER if fb > 0 else DIM for fb in freq_trace]
        ax_freq.bar(xs, freq_trace, color=fb_colors, width=0.7)
    ax_freq.set_xlim(-0.5, max(0.5, len(freq_trace) - 0.5))
    ax_freq.set_ylim(0, 33)
    ax_freq.axhline(0, color=DIM, linewidth=0.5)
    ax_freq.set_ylabel("freqbin (0=none)", color=STEEL, fontsize=8)
    ax_freq.set_title("oscillation frequency bin (0=no detection)", color=TEXT,
                      fontsize=9, pad=4)
    ax_freq.set_xlabel("window index", color=STEEL, fontsize=7)

    # --- Resonance bar ---
    if res_trace:
        r_colors = [RED if rq > 0 else DIM for rq in res_trace]
        ax_res.bar(xs, res_trace, color=r_colors, width=0.7, alpha=0.85)
    ax_res.set_xlim(-0.5, max(0.5, len(res_trace) - 0.5))
    ax_res.set_ylim(0, 1060)
    ax_res.axhline(MIN_RES_VALID >> RES_SHIFT, color=STEEL, linewidth=0.8,
                   linestyle="--", alpha=0.6,
                   label=f"MIN_RES_VALID>>{RES_SHIFT}={MIN_RES_VALID >> RES_SHIFT}")
    ax_res.legend(fontsize=7, framealpha=0.2, labelcolor=TEXT,
                  facecolor=BG, edgecolor=DIM)
    ax_res.set_ylabel("resonance_q", color=STEEL, fontsize=8)
    ax_res.set_title("resonance (scaled |D| energy)", color=TEXT, fontsize=9, pad=4)
    ax_res.set_xlabel("window index", color=STEEL, fontsize=7)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: lettered exact-integer checks, zero tolerance.
# ---------------------------------------------------------------------------

def validate():
    """Run lettered validation checks.

    All expected values are pre-computed from the algorithm; if the mirror
    disagrees with any of them the mirror is wrong -- never adjust expectations.
    """
    ok = True

    # ------------------------------------------------------------------
    # Helper: run the mirror and collect a window's worth of words
    # ------------------------------------------------------------------
    N_WINDOWS = 2
    N_EVENTS  = BATCH * WINDOW_BATCHES * N_WINDOWS   # 2048 events, 512 words

    # ------------------------------------------------------------------
    # (a) OSCILLATING EDGE at a known frequency
    # Inject a sinusoidal edge at freq_hz=8, event_rate_hz=N_EVENTS/N_WINDOWS
    # (so we get 8 zero-crossings per window at full bandwidth).
    # Expected: resonance > 0 in window 1; freqbin > 0; D oscillates (sign
    # alternates at least 4 times in window 1); disp_q well-formed.
    # ------------------------------------------------------------------
    event_rate = N_EVENTS / N_WINDOWS   # = BATCH * WINDOW_BATCHES = 1024 events/window
    freq_test  = 4.0   # Hz relative to event_rate; yields ~8 zero-crossings/window
    x_a, y_a, pol_a = build_sine_edge_stream(N_EVENTS, freq_hz=freq_test,
                                              event_rate_hz=event_rate,
                                              amplitude=6, seed=1)
    words_a, latches_a = python_seismo_words(x_a, y_a, pol_a)

    n_words_a = len(words_a)
    n_lat_a   = len(latches_a)
    # Window 1 latch (index 0): (disp_q_signed, freqbin, resonance_q, seq_at_latch)
    lat1_a = latches_a[0] if latches_a else None
    a_res_nonzero  = lat1_a is not None and lat1_a[2] > 0
    a_freq_nonzero = lat1_a is not None and lat1_a[1] > 0
    # D oscillates: re-run the integrator for window 1 and count sign changes
    # (the latched disp_q is constant per window; we need the running values)
    _disp = 0; _res = 0; _ps = 1; _zc = 0; _sign_seq = []
    for b in range(0, BATCH * WINDOW_BATCHES, BATCH):
        _roi = 0
        for i in range(b, b + BATCH):
            xi = int(x_a[i]); pi = int(pol_a[i])
            if X_ROI_LO <= xi <= X_ROI_HI:
                _roi += 1 if pi else -1
        _disp += _roi
        _disp -= _disp >> LEAK_K
        _disp = _clamp(_disp, -DISP_CLAMP, DISP_CLAMP)
        _cs = 1 if _disp >= 0 else 0
        _sign_seq.append(_cs)
        _ps = _cs
    sign_changes = sum(1 for i in range(1, len(_sign_seq))
                       if _sign_seq[i] != _sign_seq[i - 1])
    a_oscillates = sign_changes >= 4

    a_ok = (n_words_a == N_EVENTS // BATCH
            and n_lat_a == N_WINDOWS
            and a_res_nonzero
            and a_freq_nonzero
            and a_oscillates)
    print(f"  (a) OSCILLATING EDGE: words={n_words_a} (want {N_EVENTS//BATCH}), "
          f"latches={n_lat_a} (want {N_WINDOWS}), "
          f"latch1={lat1_a}, resonance_q>0={a_res_nonzero}, "
          f"freqbin>0={a_freq_nonzero}, sign_changes={sign_changes}>=4={a_oscillates} -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) STATIC EDGE: balanced ON/OFF -> D stays near 0; resonance low;
    # freqbin == 0 in both windows.
    # ------------------------------------------------------------------
    x_b, y_b, pol_b = build_static_edge_stream(N_EVENTS)
    words_b, latches_b = python_seismo_words(x_b, y_b, pol_b)

    n_lat_b = len(latches_b)
    b_freqbin_zero = all(lat[1] == 0 for lat in latches_b)
    # For balanced stream, |disp| should be very small (< 32 => resonance_q~0)
    b_res_low = all(lat[2] < 2 for lat in latches_b)

    b_ok = n_lat_b == N_WINDOWS and b_freqbin_zero and b_res_low
    print(f"  (b) STATIC EDGE: latches={n_lat_b} (want {N_WINDOWS}), "
          f"all freqbin=0={b_freqbin_zero}, resonance_q<2={b_res_low} -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) OUTSIDE ROI: no events in ROI -> roi_sum=0 always -> D=0;
    # freqbin=0; resonance=0 for all words.
    # ------------------------------------------------------------------
    x_c, y_c, pol_c = build_outside_roi_stream(N_EVENTS)
    words_c, latches_c = python_seismo_words(x_c, y_c, pol_c)

    n_lat_c = len(latches_c)
    # Seq increments each window so words are not all-zero after the first window;
    # check that all DATA fields (disp_q, freqbin, resonance_q) are zero.
    # word & 0x7FF_FF (bits[22:0]) covers disp_q[7:0] + freqbin[12:8] + resonance_q[22:13].
    DATA_MASK = 0x7FFFFF   # bits[22:0] contain disp_u8, freqbin, resonance_q
    all_data_zero_c = all((w & DATA_MASK) == 0 for w in words_c)
    c_ok = n_lat_c == N_WINDOWS and all_data_zero_c
    print(f"  (c) OUTSIDE ROI: latches={n_lat_c} (want {N_WINDOWS}), "
          f"all data fields (disp,freqbin,res) zero={all_data_zero_c} -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) WELL-FORMEDNESS: every word from (a)+(b)+(c) is well-formed.
    # disp_q in [-128,127], freqbin in [0,31], resonance_q in [0,1023],
    # seq in [0,15], bits[31:27]=0, word < 2^32.
    # ------------------------------------------------------------------
    all_words_d = words_a + words_b + words_c
    bad_d = []
    for word in all_words_d:
        d, fb, rq, sq = unpack_status(word)
        if not (-128 <= d <= 127):
            bad_d.append(f"disp_q={d}")
        if not (0 <= fb <= 31):
            bad_d.append(f"freqbin={fb}")
        if not (0 <= rq <= 1023):
            bad_d.append(f"resonance_q={rq}")
        if not (0 <= sq <= 15):
            bad_d.append(f"seq={sq}")
        if (word >> 27) != 0:
            bad_d.append(f"upper bits set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_d.append(f"word>=2^32: 0x{word:08x}")
        if bad_d:
            break
    d_ok = len(bad_d) == 0
    print(f"  (d) WELL-FORMEDNESS: {len(all_words_d)} total words; "
          f"all fields in-range, upper 5 bits=0, word<2^32 -> "
          f"{'OK' if d_ok else 'FAIL: ' + '; '.join(bad_d[:5])}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) WSEQ ARITHMETIC: word index i carries seq==((i+1)//WINDOW_BATCHES)&SEQ_MASK.
    # Use stream (c) (all zeros, deterministic).
    # ------------------------------------------------------------------
    bad_e = []
    for i, word in enumerate(words_c):
        expected = ((i + 1) // WINDOW_BATCHES) & SEQ_MASK
        _, _, _, actual = unpack_status(word)
        if actual != expected:
            bad_e.append(f"i={i} got={actual} want={expected}")
            if len(bad_e) >= 3:
                break
    e_ok = len(bad_e) == 0
    print(f"  (e) WSEQ ARITHMETIC: {len(words_c)} words; "
          f"every word[i] has seq==((i+1)//{WINDOW_BATCHES})&0x{SEQ_MASK:x} -> "
          f"{'OK' if e_ok else 'FAIL: ' + '; '.join(bad_e[:3])}")
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) FREQ_LUT MONOTONE: FREQ_LUT is non-decreasing, values 0..31.
    # ------------------------------------------------------------------
    lut_range_ok = all(0 <= v <= 31 for v in FREQ_LUT)
    lut_mono_ok  = all(FREQ_LUT[i] <= FREQ_LUT[i + 1] for i in range(len(FREQ_LUT) - 1))
    f_ok = lut_range_ok and lut_mono_ok and len(FREQ_LUT) == 16
    print(f"  (f) FREQ_LUT MONOTONE: len={len(FREQ_LUT)} (want 16), "
          f"range 0..31={lut_range_ok}, monotone={lut_mono_ok} -> "
          f"{'OK' if f_ok else 'FAIL'}")
    ok = ok and f_ok

    # ------------------------------------------------------------------
    # (g) NOISE GUARD: perfectly balanced noise (alternating ON/OFF within
    # ROI, 2 of each per batch) gives roi_sum=0 every batch -> D stays 0 ->
    # resonance=0 -> freqbin=0.  Verify that D stays exactly 0 for all words.
    # ------------------------------------------------------------------
    # Build: BATCH=4 events per batch, alternating ON/OFF/ON/OFF, all in ROI.
    cx_g  = (X_ROI_LO + X_ROI_HI) // 2
    x_g   = np.array([cx_g] * N_EVENTS, dtype=np.int64)
    y_g   = np.full(N_EVENTS, SY // 2, dtype=np.int64)
    pol_g = np.array([i % 2 for i in range(N_EVENTS)], dtype=np.int64)
    words_g, latches_g = python_seismo_words(x_g, y_g, pol_g)
    # Each batch: events (ON, OFF, ON, OFF) -> roi_sum = +1-1+1-1 = 0 -> D never moves
    g_all_zero_data = all((w & 0x7FFFFF) == 0 for w in words_g)
    g_latches_zero  = all(lat[1] == 0 and lat[2] == 0 for lat in latches_g)
    g_ok = g_all_zero_data and g_latches_zero
    print(f"  (g) NOISE GUARD: balanced alternating noise (roi_sum=0 per batch); "
          f"all data fields zero={g_all_zero_data}, latches (freqbin,res)=(0,0)={g_latches_zero} -> "
          f"{'OK' if g_ok else 'FAIL'}")
    ok = ok and g_ok

    print()
    print("VALIDATION:",
          "PASS -- oscillating edge detected; static edge silent; outside-ROI zero; "
          "well-formedness; wseq arithmetic; FREQ_LUT monotone; noise guard"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# CSV loader (from dvs_vital_view.py pattern)
# ---------------------------------------------------------------------------

def load_csv(path, ts_col="le"):
    """Load event CSV with columns x, y, pol (ts_col ignored by algorithm
    but loaded for compatibility).

    Returns (x, y, ts, pol) integer numpy arrays.
    """
    import csv
    TS_MASK = 0xFFFF
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
                    help="synthetic self-test: oscillating edge, static edge, "
                         "outside-ROI, well-formedness, wseq arithmetic, "
                         "FREQ_LUT monotone, noise guard")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le; ts unused by "
                         "this algorithm but loaded for ABI compatibility)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the seismo PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing seismo words in Python "
              f"(bit-faithful mirror of firmware).")
        words, _ = python_seismo_words(x, y, pol)
        if words:
            d, fb, rq, sq = unpack_status(words[-1])
            print(f"final: disp_q={d}, freqbin={fb}, resonance_q={rq}, seq={sq} "
                  f"({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_seismo(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
