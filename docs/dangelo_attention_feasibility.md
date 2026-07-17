# Feasibility Memo: Giulia D'Angelo's Event-Based Visual Attention Pipeline on an Async RV32I MCU

**Date:** 2026-07-15
**Question:** Can Giulia D'Angelo's neuromorphic proto-object / object-motion saliency pipeline run on a tiny async RV32I core (no HW multiply/divide, 32 KB SRAM, DVS event input, 126x112 sensor, ~115k ev/s)?
**Short answer:** The *full* pipeline (multi-orientation von Mises border-ownership + grouping pyramids, or the spiking-CNN OMS front end) is **not feasible** on this MCU. A **reduced, single-feature saliency subset** — event accumulation + one center-surround difference-of-Gaussians (OMS-style) + winner-take-all driving GPIO — **is feasible** in 32 KB with no hardware multiply, using integer/LUT approximations.

---

## 1. What her pipeline computes, and its stages

Giulia D'Angelo (formerly IIT Genoa, Event-Driven Perception for Robotics; now MSCA fellow at CTU Prague) has published a lineage of **bottom-up, learning-free, bio-inspired saliency / selective-attention** models for event cameras. The pipeline traces to the frame-based **Russell/Niebur proto-object saliency model** (Russell et al. 2014; Molin/Etienne-Cummings FPGA version, arXiv:2002.11898) and was ported to events.

### Key publications / artifacts
- **Iacono, D'Angelo, Glover, Tikhanoff, Niebur, Bartolozzi — "Proto-object based saliency for event-driven cameras"** — IROS 2019 (IEEE Xplore 8967943). The base event-driven port.
- **D'Angelo, Iacono, Glover, Niebur, Bartolozzi et al. — "Event-driven proto-object based saliency in 3D space to attract a robot's attention"** — *Scientific Reports* 12, 2022 (Nature s41598-022-11723-6; PMC9090933). Adds stereo/disparity depth saliency. Notably **omits the Gabor + center-surround filtering of the original frame-based model**, exploiting the event camera as a built-in edge extractor, and keeps the **von Mises border-ownership + grouping** stages.
- **D'Angelo et al. — "Wandering around: a bioinspired approach to visual attention through object motion sensitivity"** — *Neuromorphic Computing and Engineering*, 2025 (IOP 10.1088/2634-4386/addc90). A **spiking CNN (sCNN)** with an **Object Motion Sensitivity (OMS)** center-surround front end feeding the von Mises attention module; runs on **Speck** neuromorphic hardware + DVS on a pan-tilt unit. Produces a saliency map ~every 100 ms; ~0.04 s detection latency.
- **Reference code (ground truth for parameters below):** https://github.com/GiuliaDAngelo/Speckegomotion — PyTorch/sinabs. Key files: `functions/attention_helpers.py` (von Mises border-ownership + grouping pyramid), `functions/OMS_helpers.py` (DoG center-surround), `functions/createVMkernel.py` (VM kernel), `MainSpeckOMSAttention.py` (config with all numeric params).

### Concrete algorithm stages (from the repo)

**Stage A — Event accumulation.** DVS events (x, y, pol, t) accumulate into a 2-channel (pos/neg) 128x128 event map/histogram over a short window (`UPDATE_INTERVAL = 1 ms`). No multiply — just per-event increment of a grid cell.

**Stage B — Object Motion Sensitivity (OMS), center-surround DoG.** Two 2-D Gaussian convolutions (a tight "center" and a wide "surround"), subtracted, then normalized and thresholded to segment moving objects from ego-motion background. From `MainSpeckOMSAttention.py`:
- `size_krn_center = 8`, `sigma_center = 1`
- `size_krn_surround = 8`, `sigma_surround = 4`
- `threshold = 0.96`, LIF `tau_mem = 0.1`
- Op: `events = center - surround`; min-max normalize; `OMS = 255 where (1-norm) >= threshold`.
- Each is a `Conv2d(1,1,8x8)` — i.e. 64 multiply-accumulates per output pixel, x2 kernels.

**Stage C — Von Mises border-ownership.** The proto-object core. A bank of curved **von Mises filters** (emulating border-ownership cells) convolved with the event/edge map. From `attention_helpers.py` (`AttentionModule` defaults + config):
- `num_ori = 4` orientations (0, 45, 90, 135 deg), each with an **opposite** pair -> 8 border filters.
- `VM_radius = 8`; border kernel size `= 2*R = 17` (rounded odd). So each border conv is `Conv2d(1, 8, 17x17)` = **289 MACs/pixel x 8 orientations**.
- VM kernel value: `exp(r0 * w * cos(theta - theta0)) / I0(w2*(r - r0))` — **exponential + modified Bessel I0** per kernel tap (precomputed once, so this cost is offline).
- Border ownership combines pos/neg responses with inhibition `b_inh = 3` and a LIF nonlinearity.

**Stage D — Grouping pyramid.** A second, larger von Mises bank groups border responses into proto-objects:
- `VM_radius_group = 15`; group kernel size `= int(15*2.5) = 37` (odd) -> `Conv2d(16,16,37x37, groups=16)` = **1369 MACs/pixel x 16 channels**.
- Winner-take-all across orientations (`temp_pyramid == max`), then LIF gating and grouping inhibition `g_inh = 1.0`.

**Stage E — Multi-scale pyramid + saliency integration.** The whole B–D stack runs at **3 pyramid levels** (scale factor `0.7071^l`), results are bilinearly rescaled to a common size, **summed**, and normalized `saliency_map /= saliency_map.max()`.

**Stage F — Selective attention / WTA.** Global max of the saliency map = focus of attention -> drives a saccade / pan-tilt (or here, a GPIO pointer). Inhibition-of-return is the natural next step.

### Compute primitives used
2-D convolutions (17x17 and 37x37 kernels, 8–16 channels, 3 scales), exponentials + Bessel I0 (filter build, offline), per-pixel multiplies for min-max normalization and division-by-max, LIF neuron state updates, and repeated bilinear resampling. This is **dense, multiply-heavy, floating-point, frame-based convolution** — the opposite of what an RV32I-no-mul core does cheaply.

---

## 2. Per-stage mapping to the target MCU

Target reminder: RV32I, **no HW mul/div**; 32 KB SRAM for data (code XIP from ROM); sensor 126x112 (14,112 px); up to ~115k ev/s; 4 GPIO out. Cheap ops = shift/add/sub/compare. Every `*` or `/` is a software routine or a LUT.

A full 126x112 uint8 map = **13.8 KB**. That is the hard budget constraint: you can afford roughly **one or two full-resolution maps**, not the ~40+ intermediate tensors (8 border channels x 3 scales x pos/neg, plus 16 group channels) the reference pipeline holds.

| Stage | Memory | Compute (per frame, 126x112 ~= 14k px) | Mul/div? Conv? exp? | Verdict on this MCU |
|---|---|---|---|---|
| **A. Event accumulation** | 2 x 13.8 KB (pos/neg), or 1 x 13.8 KB single-channel | ~1 add per event; ~115k adds/s | None. Pure integer increment. | **Fits.** This is exactly the existing "decaying activity grid" app pattern. |
| **B. OMS center-surround DoG** (2x 8x8 Gaussian conv, subtract, normalize, threshold) | +2 map buffers (~28 KB) if done naively; separable + in-place brings it down | 8x8 conv = 64 MAC/px x 2 x 14k ~= **1.8M mul/frame** naive; separable Gaussian -> 2x8 = 16 MAC/px -> ~450k mul/frame | **Multiply-heavy + convolution.** Workaround: **separable integer Gaussian with power-of-two/shift-add taps** (approximate sigma=1 and sigma=4 by binomial kernels whose taps are sums of powers of two -> pure shift+add). Normalization -> replace min-max+divide with a **fixed integer threshold on (center - surround)**, no divide. **Feasible reduced form.** |
| **C. Von Mises border-ownership** (8 filters, 17x17, exp/Bessel kernel) | 8 channels x 13.8 KB = **110 KB** | 289 MAC/px x 8 x 14k ~= **32M mul/frame** | **BLOCKER at full spec.** 17x17 dense non-separable curved kernels x 8 orientations x 3 scales. Memory alone (110 KB x scales) blows 32 KB; MAC count is far beyond an event-rate no-mul budget. Kernel `exp`/`I0` are offline (OK), but the *convolution* is the killer. Workaround only via drastic reduction: 1–2 orientations, tiny (<=7x7) binary/ternary (shift-add) VM approximations, single scale — which largely degenerates into an oriented-edge center-surround. |
| **D. Grouping pyramid** (16ch, 37x37 conv, WTA) | 16 x 13.8 KB = **220 KB** | 1369 MAC/px x 16 x 14k ~= **300M mul/frame** | **HARD BLOCKER.** 37x37 kernels and 16 channels are impossible in 32 KB and orders of magnitude over budget. Drop entirely. |
| **E. 3-scale pyramid + integration** | Extra downsampled buffers (small) + repeated bilinear resample | Bilinear resample = 4 mul/px x several passes | **Multiply + resample.** Workaround: use **power-of-two downsampling (2x2 box average = adds + shift)** instead of 0.7071 bilinear; sum maps with adds. Feasible if kept to 1–2 scales. |
| **F. WTA / attention pick** | O(1) running max (x,y) | 14k compares/frame | **None.** Pure compare. **Fits trivially.** Drives GPIO (quadrant / thresholded x-y). |

**Flagged hot spots:**
- **Multiply/divide:** every convolution MAC and every normalization/`/max`. Removable via separable power-of-two kernels (shift-add) and integer threshold comparisons instead of min-max normalization.
- **2-D conv / Gabor/VM banks:** Stages C and D are the defining feature of her proto-object model and are the **blockers** — both on memory (110–220 KB of channel maps vs 32 KB) and on op count (32M–300M MAC/frame).
- **exp / softmax / Bessel:** only in *kernel construction*, which is offline/precomputed into a ROM LUT; not a runtime blocker. There is no per-event exp in the hot path (the decay, if wanted, uses the existing shift-based decaying-grid trick).

---

## 3. Verdict and a realistic MCU-sized subset

**Full pipeline: NOT FEASIBLE.** The von Mises border-ownership bank (Stage C) and grouping pyramid (Stage D) — the parts that make it "proto-object based" — require ~110–220 KB of intermediate channel maps and tens-to-hundreds of millions of MACs per frame with 17x17/37x37 non-separable kernels across 3 scales. That exceeds 32 KB SRAM by ~7–20x and exceeds a no-multiply op budget by orders of magnitude. The 2025 sCNN/OMS version targets the **Speck** neuromorphic chip precisely because it needs a spiking convolutional fabric this MCU does not have.

**Reduced version: FEASIBLE and worthwhile.** Keep the *spirit* of the model — event-driven bottom-up saliency by center-surround + winner-take-all — and drop the proto-object von Mises machinery.

### Proposed MCU-sized subset ("OMS-lite saliency pointer")

1. **Event map (Stage A).** One 126x112 uint8 activity grid, decayed with the existing shift-based decay trick. **~13.8 KB.** Pure adds.
2. **Separable integer center-surround (Stage B, reduced).** Two separable binomial (Gaussian-approx) blurs with **power-of-two taps** (e.g. center = [1 2 1]/4 via shift; surround = wider [1 4 6 4 1]/16 via shift), computed **row then column, in place** using a small line-buffer (a few rows x 126 ~= <1 KB), not a second full channel. Saliency = `blur_center - blur_surround`, clamped. **No multiply, no divide** (all taps are shifts/adds; normalization replaced by a fixed integer threshold). Second working buffer ~13.8 KB.
   - Optionally add **one or two oriented differences** (horizontal vs vertical center-surround) as a crude single-scale orientation cue — still shift-add — if a single "salient region" is not enough. This is the maximum honest nod to "orientation/border" features within budget.
3. **Winner-take-all (Stage F).** Track running max saliency and its (x,y) in one pass — **pure compares**. Optional inhibition-of-return: subtract a small constant in a window around the last winner (adds).
4. **Output.** Map the winner (x,y) to the **4 GPIO** as a region/quadrant pointer (or 2 bits x + 2 bits y coarse position, or "salient / not + which half"). This reproduces the model's end behavior — "point attention at the most salient moving region" — which is what the pan-tilt saccade does in her system.

**Total SRAM:** ~2 full maps (event + saliency working) + line buffers ~= **28–30 KB**, inside 32 KB. **Compute:** a few adds/shifts per pixel per pass over ~14k pixels + a compare pass = well within an event-rate, no-multiply budget. Kernel taps and any decay/threshold constants live in ROM.

**Honest caveat.** This subset gives you a *saliency/attention pointer*, not proto-objects. It will highlight high-contrast moving edges and blobs and pick the strongest one; it will **not** do border-ownership figure-ground grouping or true object-level attention the way her full model does. If the application needs genuine proto-object grouping, that requires the convolution/channel budget of a neuromorphic conv fabric (Speck/Loihi) or an FPGA (as in the Molin/Etienne-Cummings FPGA proto-object implementation) — not an RV32I-no-mul MCU.

---

## Sources
- Iacono, D'Angelo et al., "Proto-object based saliency for event-driven cameras," IROS 2019 — https://ieeexplore.ieee.org/document/8967943/
- D'Angelo et al., "Event-driven proto-object based saliency in 3D space to attract a robot's attention," Sci. Rep. 2022 — https://www.nature.com/articles/s41598-022-11723-6 / https://pmc.ncbi.nlm.nih.gov/articles/PMC9090933/
- D'Angelo et al., "Wandering around: a bioinspired approach to visual attention through object motion sensitivity," Neuromorph. Comput. Eng. 2025 — https://iopscience.iop.org/article/10.1088/2634-4386/addc90
- Reference implementation — https://github.com/GiuliaDAngelo/Speckegomotion (files: `functions/attention_helpers.py`, `functions/OMS_helpers.py`, `functions/createVMkernel.py`, `MainSpeckOMSAttention.py`)
- Frame-based proto-object lineage / FPGA: Russell et al. 2014; Molin et al., "A Neuromorphic Proto-Object Based Dynamic Visual Saliency Model with an FPGA Implementation," arXiv:2002.11898
- D'Angelo profiles: https://giuliadangelo.github.io/ , https://open-neuromorphic.org/contributors/giulia-dangelo/
