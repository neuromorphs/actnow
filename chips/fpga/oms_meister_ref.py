#!/usr/bin/env python3
"""
oms_meister_ref.py -- bit-identical INTEGER twin of software/dvs_oms_meister/main.c.

This is NOT the float oms_pipeline.py. It reimplements the *exact* fixed-point
arithmetic of the RV32I firmware (shift-leaks, tanh LUT, summed-area table box
pooling, reciprocal-LUT divisive inhibition, integer LIF) so we can validate the
firmware's OMS behaviour off-target:

  - near-SILENT on `global_only` and `object_coherent`
  - FIRES (localized) on `object_independent`

It feeds the firmware the same 126x112 event stream the actnow sensor produces:
the oms-meister dataset is 240x180, so x/y are rescaled to 126x112 here (the
firmware sees post-rescale coordinates on real hardware). Events are streamed in
timestamp order, popped BATCH at a time, exactly as the ISR does.

Usage:
    python3 chips/fpga/oms_meister_ref.py                 # all 3 npz clips
    python3 chips/fpga/oms_meister_ref.py --csv FILE.csv  # an actnow capture CSV

Every constant and every operation below matches main.c line-for-line.
"""
import argparse
import os
import sys
import numpy as np

# ---- constants, identical to main.c ---------------------------------------
BATCH = 4
SX, SY = 126, 112
# Input event ABI (evt_pack.v / dvs_track): x=(w>>24)&0x7F, y=(w>>17)&0x7F,
# ts=(w>>1)&0xFFFF, pol=w&1. Matches software/dvs_oms_meister/main.c.
X_SHIFT, Y_SHIFT = 24, 17
BLK_SHIFT = 3
GW, GH, GW_SHIFT = 16, 16, 4
NCELL = GW * GH
K_FAST, K_SLOW = 2, 5
INP_GAIN = 64
THETA_RECT = 6
RECIP_N, RECIP_Q = 512, 15
SIGMA_FLOOR = 4
EGAIN = 3
THETA_G = 2
K_GTHR = 4
K_LIF = 2
LIF_VTH = 64
LIF_REFRAC = 3
THRESHOLD = 40
RC_CTR, RC_INN, RC_OUT = 3, 2, 5

INT16_MIN, INT16_MAX = -32768, 32767


def _wrap16(v):
    """Emulate C int16 wraparound (matches (int16_t) casts in the firmware)."""
    return np.int16(np.int32(v))


# ---- ROM tables (built exactly as main.c builds them) ---------------------
def build_tanh_lut():
    # main.c hardcodes tanh(i/64)*256 clamped; regenerate the identical values.
    lut = np.zeros(256, dtype=np.int32)
    for i in range(256):
        lut[i] = int(round(np.tanh(i / 64.0) * 256.0))
    # main.c's table saturates around 323 (limited by int rounding); clamp to it
    # is unnecessary -- tanh(255/64)*256 ~= 256, but small-x region dominates.
    return lut


def build_recip():
    # bitwise long division, round-to-nearest -- identical to main() in the fw.
    recip = np.zeros(RECIP_N, dtype=np.int64)
    for i in range(1, RECIP_N):
        num = 1 << RECIP_Q
        q, rem = 0, 0
        for b in range(RECIP_Q, -1, -1):
            rem = (rem << 1) | ((num >> b) & 1)
            if rem >= i:
                rem -= i
                q |= (1 << b)
        if (rem << 1) >= i:
            q += 1
        recip[i] = q
    return recip


TANH = build_tanh_lut()
RECIP = build_recip()


def clampi(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class OMS:
    """Stateful firmware twin. Call step(batch_of_words) -> status word."""

    def __init__(self):
        self.s_fast_on = np.zeros(NCELL, np.int16)
        self.s_slow_on = np.zeros(NCELL, np.int16)
        self.s_fast_off = np.zeros(NCELL, np.int16)
        self.s_slow_off = np.zeros(NCELL, np.int16)
        self.lif_v = np.zeros(NCELL, np.uint8)
        self.lif_refrac = np.zeros(NCELL, np.uint8)
        self.rc = np.zeros(NCELL, np.int32)
        self.sat = np.zeros((GH + 1, GW + 1), np.int64)
        self.s_surr_prev = np.zeros(NCELL, np.int64)
        self.gthr = 0

        # --- precompute clamped SAT corner index tables for the vectorized
        #     path (geometry-only; identical arithmetic to box_sum). ---
        rr, ccc = np.meshgrid(np.arange(GH), np.arange(GW), indexing="ij")
        self._geom = {}
        for tag, hw in (("ctr", RC_CTR), ("inn", RC_INN), ("out", RC_OUT)):
            r0 = np.clip(rr - hw, 0, GH - 1); r1 = np.clip(rr + hw, 0, GH - 1)
            c0 = np.clip(ccc - hw, 0, GW - 1); c1 = np.clip(ccc + hw, 0, GW - 1)
            area = (r1 - r0 + 1) * (c1 - c0 + 1)
            self._geom[tag] = (r0, c0, r1, c1, area)

    def _box_sum_vec(self, tag):
        r0, c0, r1, c1, _ = self._geom[tag]
        s = self.sat
        return (s[r1 + 1, c1 + 1] - s[r0, c1 + 1] - s[r1 + 1, c0] + s[r0, c0])

    def box_sum(self, r0, c0, r1, c1):
        r0 = clampi(r0, 0, GH - 1); r1 = clampi(r1, 0, GH - 1)
        c0 = clampi(c0, 0, GW - 1); c1 = clampi(c1, 0, GW - 1)
        s = self.sat
        return int(s[r1 + 1][c1 + 1] - s[r0][c1 + 1] - s[r1 + 1][c0] + s[r0][c0])

    def step_vec(self, words):
        """Vectorized (numpy) twin of step(); bit-identical integer results.

        Cell stage (pooling+divisive+LIF) runs over all GHxGW cells at once."""
        # --- 1. sparse input field ---
        inp_on = np.zeros(NCELL, np.int32); inp_off = np.zeros(NCELL, np.int32)
        for w in words:
            x = (w >> X_SHIFT) & 0x7F; y = (w >> Y_SHIFT) & 0x7F; pol = w & 1
            col = x >> BLK_SHIFT; row = y >> BLK_SHIFT
            if col >= GW: col = GW - 1
            if row >= GH: row = GH - 1
            cell = (row << GW_SHIFT) | col
            if pol: inp_on[cell] = _wrap16(inp_on[cell] + INP_GAIN)
            else:   inp_off[cell] = _wrap16(inp_off[cell] + INP_GAIN)
        # --- 2. EMA update (DC-matched DoE) ---
        for arr, inp, k in ((self.s_fast_on, inp_on, K_FAST),
                            (self.s_slow_on, inp_on, K_SLOW),
                            (self.s_fast_off, inp_off, K_FAST),
                            (self.s_slow_off, inp_off, K_SLOW)):
            s = arr.astype(np.int32)
            arr[:] = _wrap16(s + ((inp - s) >> k))
        # --- 3. bandpass/rectify/tanh ---
        b_on = self.s_fast_on.astype(np.int32) - self.s_slow_on.astype(np.int32)
        b_off = self.s_fast_off.astype(np.int32) - self.s_slow_off.astype(np.int32)
        r_on = np.clip(b_on - THETA_RECT, 0, 255)
        r_off = np.clip(b_off - THETA_RECT, 0, 255)
        self.rc = TANH[r_on] + TANH[r_off]
        # --- 4. SAT (vectorized cumulative sums, same values as the C loop) ---
        rcg = self.rc.reshape(GH, GW).astype(np.int64)
        sat = self.sat
        sat[0, :] = 0; sat[:, 0] = 0
        sat[1:, 1:] = rcg.cumsum(0).cumsum(1)
        # --- 5. pooling + divisive + LIF, all cells at once ---
        E_sum = self._box_sum_vec("ctr")
        E = (E_sum * RECIP[self._geom["ctr"][4].clip(0, RECIP_N - 1)]) >> RECIP_Q
        out_sum = self._box_sum_vec("out"); inn_sum = self._box_sum_vec("inn")
        ann_sum = out_sum - inn_sum
        aarea = (self._geom["out"][4] - self._geom["inn"][4]).clip(1, RECIP_N - 1)
        S = (ann_sum * RECIP[aarea]) >> RECIP_Q
        S = np.where((self._geom["out"][4] - self._geom["inn"][4]) <= 0, 0, S)

        S_del = self.s_surr_prev.reshape(GH, GW)
        denom = np.clip(SIGMA_FLOOR + S_del, 1, RECIP_N - 1)
        z = (E * RECIP[denom]) >> (RECIP_Q - EGAIN)
        z = np.clip(z - THETA_G, 0, None)
        self.s_surr_prev = np.clip(S, 0, 65535).reshape(-1)

        # slow adaptive global threshold (delay-matched): subtract t = gthr*5/4.
        batch_peak = int(z.max())
        t = self.gthr + (self.gthr >> 2)
        z2 = np.clip(z - t, 0, None)
        self.gthr = self.gthr + ((batch_peak - self.gthr) >> K_GTHR)

        # LIF (vectorized): refractory cells decrement; others integrate z2.
        zf = z2.reshape(-1)
        v = self.lif_v.astype(np.int32)
        refr = self.lif_refrac.astype(np.int32)
        active = refr <= 0
        newv = v - (v >> K_LIF) + zf
        fired = active & (newv >= LIF_VTH)
        v = np.where(active, newv, v)
        v = np.where(fired, 0, v)
        v = np.clip(v, 0, 255)
        refr = np.where(fired, LIF_REFRAC, np.where(active, refr, refr - 1))
        self.lif_v = v.astype(np.uint8)
        self.lif_refrac = refr.astype(np.uint8)
        any_fire = int(fired.any())

        best = int(np.argmax(zf))
        best_val = int(zf[best])
        best_row = best >> GW_SHIFT; best_col = best & (GW - 1)
        val = 255 if best_val > 255 else best_val
        oms = 1 if (val >= THRESHOLD or any_fire) else 0
        word = (oms << 14) | (val << 6) | ((best_row >> 1) << 3) | (best_col >> 1)
        return word, oms, val, best_row, best_col, any_fire

    def step(self, words):
        # --- 1. build sparse per-batch input field inp = count*INP_GAIN ---
        inp_on = np.zeros(NCELL, np.int32)
        inp_off = np.zeros(NCELL, np.int32)
        for w in words:
            x = (w >> X_SHIFT) & 0x7F; y = (w >> Y_SHIFT) & 0x7F; pol = w & 1
            col = x >> BLK_SHIFT; row = y >> BLK_SHIFT
            if col >= GW: col = GW - 1
            if row >= GH: row = GH - 1
            cell = (row << GW_SHIFT) | col
            if pol: inp_on[cell] = _wrap16(inp_on[cell] + INP_GAIN)
            else:   inp_off[cell] = _wrap16(inp_off[cell] + INP_GAIN)

        # --- 2. EMA-update both poles toward inp (DC-matched DoE) ---
        for arr, inp, k in ((self.s_fast_on, inp_on, K_FAST),
                            (self.s_slow_on, inp_on, K_SLOW),
                            (self.s_fast_off, inp_off, K_FAST),
                            (self.s_slow_off, inp_off, K_SLOW)):
            s = arr.astype(np.int32)
            arr[:] = _wrap16(s + ((inp - s) >> k))

        # --- 3. bandpass, rectify-before-pool, tanh compress, sum ON+OFF ---
        b_on = self.s_fast_on.astype(np.int32) - self.s_slow_on.astype(np.int32)
        b_off = self.s_fast_off.astype(np.int32) - self.s_slow_off.astype(np.int32)
        r_on = np.clip(b_on - THETA_RECT, 0, 255)
        r_off = np.clip(b_off - THETA_RECT, 0, 255)
        self.rc = TANH[r_on] + TANH[r_off]

        # --- 4. summed-area table ---
        rcg = self.rc.reshape(GH, GW).astype(np.int64)
        sat = self.sat
        sat[0, :] = 0
        for r in range(GH):
            rowsum = 0
            sat[r + 1, 0] = 0
            for cc in range(GW):
                rowsum += int(rcg[r, cc])
                sat[r + 1, cc + 1] = sat[r, cc + 1] + rowsum

        # --- 5. pooling + divisive inhibition + LIF ---
        best_cell, best_val, any_fire = 0, 0, 0
        batch_peak = 0
        t = self.gthr + (self.gthr >> 2)   # delay-matched adaptive threshold
        for r in range(GH):
            for cc in range(GW):
                cell = (r << GW_SHIFT) | cc

                e_sum = self.box_sum(r - RC_CTR, cc - RC_CTR, r + RC_CTR, cc + RC_CTR)
                E = self._norm_area(e_sum, r - RC_CTR, cc - RC_CTR, r + RC_CTR, cc + RC_CTR)

                out_sum = self.box_sum(r - RC_OUT, cc - RC_OUT, r + RC_OUT, cc + RC_OUT)
                inn_sum = self.box_sum(r - RC_INN, cc - RC_INN, r + RC_INN, cc + RC_INN)
                ann_sum = out_sum - inn_sum
                or0 = clampi(r - RC_OUT, 0, GH - 1); or1 = clampi(r + RC_OUT, 0, GH - 1)
                oc0 = clampi(cc - RC_OUT, 0, GW - 1); oc1 = clampi(cc + RC_OUT, 0, GW - 1)
                ir0 = clampi(r - RC_INN, 0, GH - 1); ir1 = clampi(r + RC_INN, 0, GH - 1)
                ic0 = clampi(cc - RC_INN, 0, GW - 1); ic1 = clampi(cc + RC_INN, 0, GW - 1)
                oarea = (or1 - or0 + 1) * (oc1 - oc0 + 1)
                iarea = (ir1 - ir0 + 1) * (ic1 - ic0 + 1)
                aarea = oarea - iarea
                if aarea <= 0:
                    S = 0
                else:
                    if aarea >= RECIP_N: aarea = RECIP_N - 1
                    S = (ann_sum * int(RECIP[aarea])) >> RECIP_Q

                S_del = int(self.s_surr_prev[cell])
                self.s_surr_prev[cell] = clampi(S, 0, 65535)

                denom = SIGMA_FLOOR + S_del
                if denom < 1: denom = 1
                if denom >= RECIP_N: denom = RECIP_N - 1
                z = (E * int(RECIP[denom])) >> (RECIP_Q - EGAIN)
                z = z - THETA_G
                if z < 0: z = 0

                if z > batch_peak: batch_peak = z
                z2 = z - t
                if z2 < 0: z2 = 0

                # LIF (driven by adaptive-thresholded z2)
                if self.lif_refrac[cell] > 0:
                    self.lif_refrac[cell] -= 1
                else:
                    vv = int(self.lif_v[cell])
                    vv = vv - (vv >> K_LIF) + z2
                    if vv >= LIF_VTH:
                        vv = 0
                        self.lif_refrac[cell] = LIF_REFRAC
                        any_fire = 1
                    if vv > 255: vv = 255
                    self.lif_v[cell] = vv

                if z2 > best_val:
                    best_val = z2
                    best_cell = cell

        self.gthr = self.gthr + ((batch_peak - self.gthr) >> K_GTHR)
        best_row = best_cell >> GW_SHIFT
        best_col = best_cell & (GW - 1)
        val = 255 if best_val > 255 else best_val
        oms = 1 if (val >= THRESHOLD or any_fire) else 0
        word = (oms << 14) | (val << 6) | ((best_row >> 1) << 3) | (best_col >> 1)
        return word, oms, val, best_row, best_col, any_fire

    def _norm_area(self, s, r0, c0, r1, c1):
        rc0 = clampi(r0, 0, GH - 1); rc1 = clampi(r1, 0, GH - 1)
        cc0 = clampi(c0, 0, GW - 1); cc1 = clampi(c1, 0, GW - 1)
        a = (rc1 - rc0 + 1) * (cc1 - cc0 + 1)
        if a <= 0: return 0
        if a >= RECIP_N: a = RECIP_N - 1
        return (s * int(RECIP[a])) >> RECIP_Q


# ---- event loaders --------------------------------------------------------
def pack_word(x, y, pol, ts=0):
    # evt_pack.v / dvs_track ABI: x=(w>>24)&0x7F, y=(w>>17)&0x7F, ts=(w>>1)&0xFFFF,
    # pol=w&1.  Must match X_SHIFT/Y_SHIFT decode above.
    return ((x & 0x7F) << X_SHIFT) | ((y & 0x7F) << Y_SHIFT) | ((ts & 0xFFFF) << 1) | (pol & 1)


def load_npz(path):
    """Load an oms-meister clip, rescale 240x180 -> 126x112, sort by t, pack."""
    d = np.load(path)
    x = d["x"].astype(np.int64)
    y = d["y"].astype(np.int64)
    t = d["t"].astype(np.float64)
    p = d["p"].astype(np.int64)
    W0, H0 = 240, 180
    # rescale to sensor grid (integer, clamp) -- what the actnow pixel produces.
    xs = np.clip((x * SX) // W0, 0, SX - 1)
    ys = np.clip((y * SY) // H0, 0, SY - 1)
    pol = (p > 0).astype(np.int64)   # oms-meister uses +/-1
    order = np.argsort(t, kind="stable")
    xs, ys, pol, ts = xs[order], ys[order], pol[order], (t[order] * 1000).astype(np.int64)
    # evt_pack.v / dvs_track ABI (matches X_SHIFT/Y_SHIFT decode).
    words = (((xs & 0x7F) << X_SHIFT) | ((ys & 0x7F) << Y_SHIFT)
             | ((ts & 0xFFFF) << 1) | pol).astype(np.uint32)
    return words, order.size


def load_csv(path):
    """actnow capture CSV (columns le,x,y,pol). No timestamps needed -- stream order."""
    words = []
    with open(path) as f:
        header = f.readline().strip().split(",")
        ix = {name: i for i, name in enumerate(header)}
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < len(header):
                continue
            x = int(parts[ix["x"]]); y = int(parts[ix["y"]]); pol = int(parts[ix["pol"]])
            words.append(pack_word(x, y, pol))
    return np.array(words, np.uint32), len(words)


def run_clip(words, n, label, max_events=None):
    if max_events:
        words = words[:max_events]; n = len(words)
    oms = OMS()
    n_batches = n // BATCH
    spikes = 0          # LIF fires (any_fire)
    detect_batches = 0  # batches whose status word set the oms flag
    heat = np.zeros(NCELL, np.int64)
    for b in range(n_batches):
        chunk = words[b * BATCH:(b + 1) * BATCH]
        word, oflag, val, br, bc, fired = oms.step_vec([int(w) for w in chunk])
        if fired:
            spikes += 1
        if oflag:
            detect_batches += 1
            heat[(br << GW_SHIFT) | bc] += val
    rate = spikes / max(n_batches, 1)
    det_rate = detect_batches / max(n_batches, 1)
    hot = int(np.argmax(heat)) if heat.max() > 0 else -1
    return dict(label=label, n=n, n_batches=n_batches, spikes=spikes,
                spike_frac=rate, detect_batches=detect_batches,
                detect_frac=det_rate, hottest_cell=hot,
                hottest_rc=(hot >> GW_SHIFT, hot & (GW - 1)) if hot >= 0 else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="actnow capture CSV instead of the npz clips")
    ap.add_argument("--data", default=os.path.expanduser("~/git/oms-meister/data"))
    ap.add_argument("--max-events", type=int, default=None,
                    help="cap events per clip (speed); default all")
    args = ap.parse_args()

    print(f"# OMS fixed-point twin (RV32I firmware mirror), grid {GW}x{GH}, BATCH={BATCH}")
    print(f"# rescale 240x180 -> {SX}x{SY}; near-silent on global/coherent, fires on independent\n")

    if args.csv:
        words, n = load_csv(args.csv)
        r = run_clip(words, n, os.path.basename(args.csv), args.max_events)
        _report(r)
        return

    results = {}
    for sc in ["global_only", "object_coherent", "object_independent"]:
        path = os.path.join(args.data, f"events_{sc}.npz")
        if not os.path.exists(path):
            print(f"  (missing {path}, skipping)")
            continue
        words, n = load_npz(path)
        r = run_clip(words, n, sc, args.max_events)
        results[sc] = r
        _report(r)

    # summary contrast
    if {"global_only", "object_independent"} <= results.keys():
        g = results["global_only"]["spike_frac"] + 1e-12
        i = results["object_independent"]["spike_frac"]
        print(f"\n# CONTRAST  independent / global_only spike-fraction ratio: "
              f"{i / g:.1f}x  (higher = better silence-vs-detection separation)")
        if "object_coherent" in results:
            c = results["object_coherent"]["spike_frac"] + 1e-12
            print(f"# CONTRAST  independent / object_coherent spike-fraction ratio: "
                  f"{i / c:.1f}x")


def _report(r):
    print(f"[{r['label']:>20}] events={r['n']:>8}  batches={r['n_batches']:>7}  "
          f"LIF-spikes={r['spikes']:>6} ({100*r['spike_frac']:6.3f}% of batches)  "
          f"detect-flag={100*r['detect_frac']:6.3f}%  hottest(r,c)={r['hottest_rc']}",
          flush=True)


if __name__ == "__main__":
    main()
