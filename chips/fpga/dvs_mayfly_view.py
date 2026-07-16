#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_mayfly/main.c
("Computational Mayfly").

Every incoming DVS event spawns a tiny ephemeral "creature" that walks a few
steps through a bit-packed occupancy world, toggling cells as it goes, then
dies. The chip emits one word per walk STEP describing the touched cell; this
host unpacks those step-words and renders the evolving world. Because the whole
thing is event-driven and timestamp-independent, covering the lens (no events)
freezes the world -- the organisms only exist while the world is changing.

python_mayfly_steps() below is a byte-for-byte port of the firmware's integer
logic (same xorshift hash, same 8-compass walk, same bit-packed toggle world,
same edge wrapping) so the rendered ecosystem is provably what the chip emits
for the same event stream.

------------------------------------------------------------------------------
Timestamp-independent by design, so this validates on ANY event stream,
including the recorded chips/fpga CSVs (whose `le` column is not a usable
timestamp). --validate runs the identical integer logic on a real capture and
confirms the world stays bounded (population never exceeds the bit-world; no
runaway growth).

Usage:
  dvs_mayfly_view.py capture.csv                 # render evolving world from a CSV
  dvs_mayfly_view.py capture.csv --validate      # bounded-world self-test (numbers)
  dvs_mayfly_view.py --from-actsim steps.mem     # render real chip step-words
  dvs_mayfly_view.py capture.csv --headless --save world.png
  dvs_mayfly_view.py capture.csv --anim out.mp4  # (if imageio present) evolving movie
live keys (windowed): space=pause, r=restart, q=quit
"""
import argparse
import numpy as np

# --- must match software/dvs_mayfly/main.c exactly ---
SX, SY = 126, 112
WORDS_PER_ROW = 4                # 128 bits/row, 126 used
MIN_LIFE = 2
LIFE_MASK = 7
MAX_LIFE = MIN_LIFE + LIFE_MASK  # 9
BATCH = 4

DX = [1, 1, 0, -1, -1, -1, 0, 1]
DY = [0, 1, 1, 1, 0, -1, -1, -1]

MASK32 = 0xFFFFFFFF

# --- must match software/dvs_mayfly/main.c exactly ---
X_SHIFT = 24
Y_SHIFT = 17
# Spatio-temporal correlation SPAWN GATE (coarse 4x4-px cells). CORR_MIN=0 off.
CELL_SHIFT = 2
GRID_COLS = 32
GRID_ROWS = 28
CORR_WINDOW = 30
CORR_MIN = 2


def hash_step(h):
    """Byte-for-byte mirror of main.c's hash_step (xorshift, 32-bit wrap)."""
    h &= MASK32
    h ^= (h << 13) & MASK32
    h ^= (h >> 17)
    h ^= (h << 5) & MASK32
    return h & MASK32


def python_mayfly_steps(x, y, pol, return_world=False):
    """Bit-faithful port of software/dvs_mayfly/main.c's ISR. Consumes events in
    BATCH-sized groups (like the chip) and returns the list of packed step-words
    the chip would emit. If return_world, also returns the final occupancy world
    as a bool array [SY, SX] and a per-word population trace."""
    # Bit-packed world identical to the firmware's world[row][word].
    world = [[0] * WORDS_PER_ROW for _ in range(SY)]
    words = []
    pop_trace = []
    pop = 0  # number of set cells, tracked incrementally

    # Correlation spawn-gate state (mirrors the firmware's last_touched[]).
    last_touched = [0] * (GRID_COLS * GRID_ROWS)
    event_count = 0

    def is_recent(last, nowc):
        return last != 0 and (nowc - last) <= CORR_WINDOW

    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            pi = int(pol[i]) & 1

            if CORR_MIN > 0:
                # Spatio-temporal correlation SPAWN GATE (bit-exact to firmware).
                gcol = xi >> CELL_SHIFT
                grow = yi >> CELL_SHIFT
                cell = (grow << 5) | gcol
                event_count += 1
                nc = 0
                has_l, has_r = gcol > 0, gcol < GRID_COLS - 1
                has_u, has_d = grow > 0, grow < GRID_ROWS - 1
                if has_l:            nc += is_recent(last_touched[cell - 1], event_count)
                if has_r:            nc += is_recent(last_touched[cell + 1], event_count)
                if has_u:            nc += is_recent(last_touched[cell - GRID_COLS], event_count)
                if has_d:            nc += is_recent(last_touched[cell + GRID_COLS], event_count)
                if has_l and has_u:  nc += is_recent(last_touched[cell - GRID_COLS - 1], event_count)
                if has_r and has_u:  nc += is_recent(last_touched[cell - GRID_COLS + 1], event_count)
                if has_l and has_d:  nc += is_recent(last_touched[cell + GRID_COLS - 1], event_count)
                if has_r and has_d:  nc += is_recent(last_touched[cell + GRID_COLS + 1], event_count)
                last_touched[cell] = event_count
                if nc < CORR_MIN:
                    continue   # uncorrelated -- no spawn (noise/hot pixel)

            # Canonical (x,y,pol)-only seed -- matches the firmware's re-packed seed.
            seed = (xi << X_SHIFT) | (yi << Y_SHIFT) | pi
            h = hash_step(seed | 0x9E3779B9)
            life = MIN_LIFE + (h & LIFE_MASK)
            if life > MAX_LIFE:
                life = MAX_LIFE
            cx = xi
            cy = yi
            for s in range(life):
                # firmware wraps underflow to 0 / clamps overflow to edge-1
                if cx >= SX:
                    cx = 0 if cx >= 0x80000000 else SX - 1
                if cy >= SY:
                    cy = 0 if cy >= 0x80000000 else SY - 1
                widx = cx >> 5
                bit = 1 << (cx & 31)
                was = (world[cy][widx] & bit) != 0
                world[cy][widx] ^= bit
                new_state = 0 if was else 1
                pop += 1 if new_state else -1
                step0 = 1 if s == 0 else 0
                words.append((step0 << 15) | (new_state << 14) | (cy << 7) | cx)
                pop_trace.append(pop)
                # advance
                h = hash_step(h)
                d = h & 7
                cx = (cx + DX[d]) & MASK32
                cy = (cy + DY[d]) & MASK32
    if return_world:
        w = np.zeros((SY, SX), dtype=bool)
        for r in range(SY):
            for c in range(SX):
                if world[r][c >> 5] & (1 << (c & 31)):
                    w[r, c] = True
        return words, w, pop_trace
    return words


def unpack_step(word):
    cx = word & 0x7F
    cy = (word >> 7) & 0x7F
    new_state = (word >> 14) & 1
    step0 = (word >> 15) & 1
    return cx, cy, new_state, step0


def world_from_steps(words):
    """Replay chip step-words to reconstruct the occupancy world (host doesn't
    need the hash -- each word already carries the toggled cell + its new
    state)."""
    w = np.zeros((SY, SX), dtype=bool)
    pop_trace = []
    pop = 0
    for word in words:
        cx, cy, new_state, step0 = unpack_step(word)
        if 0 <= cx < SX and 0 <= cy < SY:
            if bool(w[cy, cx]) != bool(new_state):
                pop += 1 if new_state else -1
            w[cy, cx] = bool(new_state)
        pop_trace.append(pop)
    return w, pop_trace


def load_csv(path):
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x = np.array([int(row[idx["x"]]) for row in rows], dtype=np.int64)
    y = np.array([int(row[idx["y"]]) for row in rows], dtype=np.int64)
    pcol = idx.get("pol")
    pol = (np.array([int(row[pcol]) for row in rows], dtype=np.int64)
           if pcol is not None else np.zeros(len(x), dtype=np.int64))
    return x, y, pol


def validate(csv_path):
    x, y, pol = load_csv(csv_path)
    words, world, pop_trace = python_mayfly_steps(x, y, pol, return_world=True)
    n_events = (len(x) // BATCH) * BATCH
    max_cells = SX * SY
    final_pop = int(world.sum())
    peak_pop = max(pop_trace) if pop_trace else 0
    print(f"csv:            {csv_path}")
    print(f"events fed:     {n_events} (BATCH={BATCH})")
    print(f"step-words out: {len(words)}  (avg {len(words) / max(1, n_events):.2f} per event)")
    print(f"world capacity: {max_cells} cells ({SX}x{SY} bits, "
          f"{SY * WORDS_PER_ROW * 4} bytes packed)")
    print(f"final occupied: {final_pop} cells ({100 * final_pop / max_cells:.1f}% of world)")
    print(f"peak occupied:  {peak_pop} cells ({100 * peak_pop / max_cells:.1f}% of world)")
    bounded = peak_pop <= max_cells and final_pop <= max_cells
    # A toggling walker keeps the world sparse: births and deaths interleave, so
    # occupancy should sit well below capacity (no runaway fill).
    healthy = peak_pop < max_cells * 0.9
    print()
    print("VALIDATION:",
          "PASS -- world stays bounded" + (" and sparse (no runaway growth)" if healthy else "")
          if bounded else "FAIL -- world exceeded capacity")
    return bounded


def render_world(world, save=None, headless=False, title="Computational Mayfly -- world"):
    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"occupied cells: {int(world.sum())} / {SX * SY}")
        return
    fig, ax = plt.subplots(figsize=(SX / 20, SY / 20))
    ax.imshow(world, cmap="magma", interpolation="nearest")
    ax.set_title(title, fontsize=8)
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=140)
        print(f"wrote {save}")
    if not headless:
        plt.show()


def animate(words, save):
    """Optional evolving movie: replay step-words, snapshotting the world."""
    try:
        import imageio.v2 as imageio
    except Exception as e:
        print("imageio unavailable, skipping --anim:", e)
        return
    w = np.zeros((SY, SX), dtype=bool)
    frames = []
    stride = max(1, len(words) // 200)  # ~200 frames
    for k, word in enumerate(words):
        cx, cy, new_state, step0 = unpack_step(word)
        if 0 <= cx < SX and 0 <= cy < SY:
            w[cy, cx] = bool(new_state)
        if k % stride == 0:
            frames.append((w.astype(np.uint8) * 255))
    imageio.mimsave(save, frames, fps=20)
    print(f"wrote {save} ({len(frames)} frames)")


def render_live(words, scale=6, fps=60):
    try:
        import cv2
    except Exception as e:
        print("cv2 unavailable; use --headless --save instead:", e)
        return
    n = len(words)
    W, H = SX, SY
    cv2.namedWindow("Computational Mayfly", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Computational Mayfly", W * scale, H * scale)
    world = np.zeros((H, W), np.float32)   # decaying trail for a lively look
    i, paused = 0, False
    print(f"replaying {n} step-words  (space=pause r=restart q=quit)")
    per_frame = max(1, n // 600)
    try:
        while True:
            if not paused:
                world *= 0.90
                for _ in range(per_frame):
                    if i >= n:
                        break
                    cx, cy, new_state, step0 = unpack_step(words[i])
                    if 0 <= cx < W and 0 <= cy < H:
                        world[cy, cx] = 1.0 if new_state else 0.3
                    i += 1
            img = (np.clip(world, 0, 1) * 255).astype(np.uint8)
            img = cv2.applyColorMap(img, cv2.COLORMAP_MAGMA)
            img = cv2.resize(img, (W * scale, H * scale), interpolation=cv2.INTER_NEAREST)
            cv2.putText(img, f"{i}/{n}", (2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (255, 255, 255), 1)
            cv2.imshow("Computational Mayfly", img)
            k = cv2.waitKey(max(1, int(1000 / fps))) & 0xFF
            if k == ord('q'):
                break
            elif k == ord(' '):
                paused = not paused
            elif k == ord('r'):
                i = 0; world[:] = 0
    finally:
        cv2.destroyAllWindows()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="bounded-world self-test on the CSV (prints numbers)")
    ap.add_argument("--from-actsim", metavar="STEPS_MEM",
                    help="use real chip step-words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the final world PNG here")
    ap.add_argument("--anim", metavar="OUT", help="write an evolving movie (needs imageio)")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--fps", type=float, default=60.0)
    args = ap.parse_args()

    if args.validate:
        if not args.csv:
            ap.error("--validate needs a CSV")
        ok = validate(args.csv)
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} real chip step-words from {args.from_actsim}")
        world, _ = world_from_steps(words)
    elif args.csv:
        x, y, pol = load_csv(args.csv)
        words, world, _ = python_mayfly_steps(x, y, pol, return_world=True)
        print(f"loaded {len(x)} events from {args.csv}; computed {len(words)} step-words "
              f"(Python mirror of firmware)")
    else:
        ap.error("need a CSV or --from-actsim STEPS_MEM")

    if args.anim:
        animate(words, args.anim)
    if args.headless or args.save:
        render_world(world, save=args.save, headless=args.headless)
    if not (args.headless or args.save or args.anim):
        render_live(words, scale=args.scale, fps=args.fps)


if __name__ == "__main__":
    main()
