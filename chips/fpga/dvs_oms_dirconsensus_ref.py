#!/usr/bin/env python3
"""
Integer reference mirror for software/dvs_oms_dirconsensus/main.c.

Recomputes, in Python, the EXACT integer logic the firmware runs on the actnow
async RV32I core: a per-pixel/per-polarity 1-byte quantized last-timestamp
surface (downsampled /2), an 8-band-weighted direction coincidence vector per
event, a leaky per-tile 8-bin direction histogram (decayed by a periodic
HALVING, not exp), a confidence gate by cross-multiply, and a per-event
independent-motion agreement score z (all shifts/adds/compares, no mul/div).

Two entry points:

  * detector_scores(events, W, H)  -> (scores[N], dbg dict)
      per-event integer z (higher = more independent motion), matching the C.
      Used to score against the oms-meister GT the same way eval_harness does
      (BGL sweep / keep-rate), so we can report detection-vs-background numbers.

  * batch_words(events, W, H)      -> list[int]
      the exact per-BATCH output words the firmware's isr_handler emits
      (flag/zval/row/col/global-dir), so a bit-exact e2e test could assert them.

Constants are transcribed VERBATIM from main.c; keep them in sync.

Validation CLI:
    python3 dvs_oms_dirconsensus_ref.py <oms-meister rec dir> [max_events]
        -> loads events.npy + ground_truth.npz (origin labels), runs the integer
           detector, prints BGL-vs-recall (keep@1%BGL) per object, background
           suppression, and the global-direction stability. Confirms it silences
           coherent global motion and flags independent motion.
    python3 dvs_oms_dirconsensus_ref.py --csv <chips/fpga capture.csv>
        -> runs on an actnow recording (le,x,y,pol) and prints score stats +
           a few batch words (no GT -> qualitative).
"""
import sys
import numpy as np

# ------------------------------------------------------------------ constants
# (transcribed from software/dvs_oms_dirconsensus/main.c)
SX, SY = 126, 112
TS_SHIFT = 1
TSW = (SX + (1 << TS_SHIFT) - 1) >> TS_SHIFT          # 63
TSH = (SY + (1 << TS_SHIFT) - 1) >> TS_SHIFT          # 56
TSW_LOG2 = 6
TSW_P2 = 1 << TSW_LOG2                                 # 64
POL_STRIDE = TSW_P2 * TSH                              # 3584

TILE_SHIFT = 4
TW = (SX + (1 << TILE_SHIFT) - 1) >> TILE_SHIFT        # 8
TH = (SY + (1 << TILE_SHIFT) - 1) >> TILE_SHIFT        # 7
TW_LOG2 = 3
TW_P2 = 1 << TW_LOG2                                   # 8
NT = TW_P2 * TH                                        # 56

TS_TICK_SHIFT = 8
BAND_LO, BAND_MID, BAND_HI, BAND_FAR = 4, 12, 31, 78
W_NEAR, W_MID, W_FAR = 4, 2, 1
CONF_NUM, CONF_DEN = 2, 5
DECAY_BATCHES = 64
HIST_CAP = 4000
ZVAL_MAX = 31
THRESHOLD = 6
BATCH = 4

ODX = np.array([1, 1, 0, -1, -1, -1, 0, 1], np.int32)
ODY = np.array([0, -1, -1, -1, 0, 1, 1, 1], np.int32)
OPP = np.array([4, 5, 6, 7, 0, 1, 2, 3], np.int32)


def _run(x, y, pol, tq, do_words=False, W=SX, H=SY):
    """Core integer loop -- bit-faithful to isr_handler. Returns (scores, words)."""
    N = len(x)
    ts_surf = np.zeros(2 * POL_STRIDE, np.int32)     # quantized last-ts (0..255)
    seen = np.zeros(2 * POL_STRIDE, np.uint8)
    hist = np.zeros(NT * 8, np.int64)
    scores = np.zeros(N, np.int64)
    words = []
    batch_ctr = 0

    # spatial clamps (tiles are 16px full-res; super-px is /2)
    for j in range(N):
        # firmware decays ONCE per DECAY_BATCHES batches, at the top of the ISR,
        # i.e. every DECAY_BATCHES*BATCH events. Replicate that phase exactly.
        if j % BATCH == 0:
            batch_ctr += 1
            if batch_ctr >= DECAY_BATCHES:
                batch_ctr = 0
                hist >>= 1

        xj = int(x[j]); yj = int(y[j]); pj = int(pol[j]); tqj = int(tq[j])

        sx = xj >> TS_SHIFT
        sy = yj >> TS_SHIFT
        if sx >= TSW: sx = TSW - 1
        if sy >= TSH: sy = TSH - 1
        base = pj * POL_STRIDE
        sidx = base + (sy << TSW_LOG2) + sx

        tx = xj >> TILE_SHIFT
        ty = yj >> TILE_SHIFT
        if tx >= TW: tx = TW - 1
        if ty >= TH: ty = TH - 1
        tidx = (ty << TW_LOG2) + tx
        h0 = tidx << 3

        ce = [0] * 8
        ce_sum = 0
        for i in range(8):
            nx = sx + int(ODX[i]); ny = sy + int(ODY[i])
            if nx < 0 or nx >= TSW or ny < 0 or ny >= TSH:
                continue
            nidx = base + (ny << TSW_LOG2) + nx
            if not seen[nidx]:
                continue
            age = (tqj - int(ts_surf[nidx])) & 0xFF
            if age < BAND_LO:
                continue
            if age <= BAND_MID:   w = W_NEAR
            elif age <= BAND_HI:  w = W_MID
            elif age <= BAND_FAR: w = W_FAR
            else:                 continue
            ce[int(OPP[i])] += w
            ce_sum += w

        h_sum = 0; h_win = -1; d_cons = 0
        for d in range(8):
            hv = int(hist[h0 + d])
            h_sum += hv
            if hv > h_win:
                h_win = hv; d_cons = d

        z = 0
        if h_sum > 0 and ce_sum > 0:
            hw5 = (h_win << 2) + h_win
            hs2 = h_sum << 1
            if hw5 >= hs2:
                c_at = ce[d_cons]
                c_other = 0
                for d in range(8):
                    if d == d_cons: continue
                    if ce[d] > c_other: c_other = ce[d]
                z = (ce_sum - c_at) + (c_other >> 1)
        scores[j] = z

        # update AFTER scoring
        if ce_sum > 0:
            for d in range(8):
                if ce[d]:
                    nv = int(hist[h0 + d]) + ce[d]
                    hist[h0 + d] = HIST_CAP if nv > HIST_CAP else nv
        ts_surf[sidx] = tqj
        seen[sidx] = 1

        # emit a word at the END of each BATCH (mirrors isr writing FIFO_OUT once
        # per batch: winner over the batch's events, global dir over all tiles).
        if do_words and (j % BATCH == BATCH - 1):
            lo = j - (BATCH - 1)
            seg = scores[lo:j + 1]
            best_z = int(seg.max())
            # recover winning tile for the argmax event
            k = lo + int(np.argmax(seg))
            btx = min(int(x[k]) >> TILE_SHIFT, TW - 1)
            bty = min(int(y[k]) >> TILE_SHIFT, TH - 1)
            best_tile = (bty << TW_LOG2) + btx
            g = hist.reshape(NT, 8).sum(0)
            g_dir = int(np.argmax(g)) if g.max() > 0 else 0
            zc = 0 if best_z < 0 else min(best_z, ZVAL_MAX)
            flag = 1 if best_z >= THRESHOLD else 0
            row = (best_tile >> TW_LOG2) & 0x7
            col = best_tile & 0x7
            words.append((flag << 14) | (zc << 9) | (row << 6) | (col << 3) | (g_dir & 0x7))

    return scores, words


def _quantize_ts(t):
    """Raw ts (us) -> 8-bit byte-tick. Firmware gets ts=(word>>1)&0xFFFF (16-bit,
    evt_pack.v [16:1]) and does ts>>TS_TICK_SHIFT; (t>>8)&0xFF here selects the
    same ts bits [15:8], so it matches the firmware bit-for-bit.
    Timestamps are made relative to the first event to avoid huge-offset aliasing
    (a pure additive constant cancels in every age difference anyway)."""
    t = np.asarray(t, np.int64)
    t = t - t.min()
    return ((t >> TS_TICK_SHIFT) & 0xFF).astype(np.int32)


def detector_scores(events, W=SX, H=SY):
    x = np.asarray(events[:, 0], np.int32)
    y = np.asarray(events[:, 1], np.int32)
    pol = (np.asarray(events[:, 2]) > 0).astype(np.int32)
    tq = _quantize_ts(events[:, 3])
    scores, _ = _run(x, y, pol, tq, do_words=False)
    return scores


def batch_words(events, W=SX, H=SY):
    x = np.asarray(events[:, 0], np.int32)
    y = np.asarray(events[:, 1], np.int32)
    pol = (np.asarray(events[:, 2]) > 0).astype(np.int32)
    tq = _quantize_ts(events[:, 3])
    _, words = _run(x, y, pol, tq, do_words=True)
    return words


# ------------------------------------------------------------------ validation
def _keep_at_bgl(scores, is_bg, bgl=0.01):
    """threshold set to keep the top `bgl` fraction of BACKGROUND events; return
    thr. (mirrors eval_harness._bgl_threshold semantics.)"""
    bg = scores[is_bg]
    thr = np.quantile(bg, 1.0 - bgl)
    return thr


def validate_rec(recdir, max_events=1_500_000, from_start=False):
    import os, json
    ev = np.load(os.path.join(recdir, "events.npy")).astype(np.int64)
    gt = np.load(os.path.join(recdir, "ground_truth.npz"))
    origin = gt["origin"].astype(np.int64)
    try:
        meta = json.load(open(os.path.join(recdir, "meta.json")))
        indep = {int(o["id"]): bool(o["independent"]) for o in meta["objects"]}
    except Exception:
        indep = {}
    if len(ev) > max_events:
        # Take a CONTIGUOUS FULL-RATE slice, NOT a strided decimation: the
        # detector is a TIMING detector (delay bands 1-20 ms), so decimating in
        # time stretches the effective inter-event spacing and pushes real
        # coincidences out of the delay bands. A contiguous window preserves the
        # native event rate and timing structure. Start partway in so the tile
        # histograms have real background flow to consense on.
        start = 0 if from_start else min(len(ev) - max_events, len(ev) // 4)
        ev = ev[start:start + max_events]
        origin = origin[start:start + max_events]
        dur = (ev[:, 3].max() - ev[:, 3].min()) / 1e6
        print(f"[slice] contiguous full-rate window: {len(ev):,} events, "
              f"{dur:.2f} s ({len(ev)/dur/1e3:.0f}k ev/s)  "
              f"(input ~{len(gt['origin']):,})")
    print(f"[rec] {recdir}  N={len(ev):,}  bg={int((origin==0).sum()):,}  "
          f"obj={int((origin>0).sum()):,}")

    sc = detector_scores(ev)

    is_bg = origin == 0
    thr = _keep_at_bgl(sc, is_bg, 0.01)   # ~1% BGL operating point
    bg_kept = float((sc[is_bg] >= thr).mean())
    print(f"  1%-BGL threshold z>={thr:.1f}   -> bg kept = {bg_kept*100:.2f}%  "
          f"(target ~1%; this is the background SUPPRESSION floor)")
    print(f"  bg score: mean={sc[is_bg].mean():.2f} med={np.median(sc[is_bg]):.1f} "
          f"q99={np.quantile(sc[is_bg],.99):.1f} max={sc[is_bg].max()}")

    for oid in sorted(set(origin[origin > 0]) - {0}):
        sel = origin == oid
        s = sc[sel]
        keep = float((s >= thr).mean())
        role = ("indep " if indep.get(int(oid) - 1, True) else "CONTROL")
        marker = ""
        if not indep.get(int(oid) - 1, True):
            marker = "  <- comoving control: SHOULD stay near bg (suppressed)"
        elif keep > bg_kept * 1.5:
            marker = "  <- independent motion FLAGGED above bg"
        print(f"  obj{int(oid)-1:>2} [{role}]: n={int(sel.sum()):>7d}  "
              f"keep@1%BGL={keep*100:6.2f}%  mean={s.mean():6.2f} "
              f"q90={np.quantile(s,.9):5.1f}{marker}")

    # global background direction stability (the stabilization bonus): the
    # dominant global histogram bin should be persistent for a coherently
    # shaking/panning background.
    words = batch_words(ev[:min(len(ev), 200_000)])
    if words:
        gdirs = np.array([w & 0x7 for w in words])
        vals, cnts = np.unique(gdirs, return_counts=True)
        dom = vals[np.argmax(cnts)]
        frac = cnts.max() / cnts.sum()
        print(f"  global bg dir (stabilization vector): dominant bin={dom} "
              f"({frac*100:.0f}% of batches)  [{dict(zip(vals.tolist(),cnts.tolist()))}]")


def validate_csv(csvpath):
    import csv
    rows = []
    with open(csvpath) as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append((int(row["le"]), int(row["x"]), int(row["y"]), int(row["pol"])))
    arr = np.array(rows, np.int64)
    le, x, y, pol = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    # actnow CSV order is le,x,y,pol; the detector wants [x,y,pol,t]. Sort by le
    # (time) so the causal loop sees events in time order.
    order = np.argsort(le, kind="stable")
    ev = np.stack([x[order], y[order], pol[order], le[order]], 1)
    print(f"[csv] {csvpath}  N={len(ev):,}  x[{x.min()},{x.max()}] "
          f"y[{y.min()},{y.max()}] le[{le.min()},{le.max()}]")
    sc = detector_scores(ev)
    nz = sc[sc > 0]
    print(f"  scores: nonzero={len(nz):,}/{len(sc):,} "
          f"({100*len(nz)/max(1,len(sc)):.1f}%)  max={sc.max()} "
          f"mean(nz)={nz.mean() if len(nz) else 0:.2f}")
    words = batch_words(ev[:min(len(ev), 40_000)])
    flagged = sum((w >> 14) & 1 for w in words)
    print(f"  batch words: {len(words)}  independent-motion flags={flagged}  "
          f"sample={[hex(w) for w in words[:6]]}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    if args[0] == "--csv":
        validate_csv(args[1])
    else:
        fs = "--from-start" in args
        args = [a for a in args if a != "--from-start"]
        me = int(args[1]) if len(args) > 1 else 1_500_000
        validate_rec(args[0], me, from_start=fs)
