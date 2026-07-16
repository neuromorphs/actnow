# actnow event-camera demo firmware suite

Host-output DVS demos for the async **RV32I** actnow core (no multiply/divide, 32 KB SRAM,
event-driven wake-on-interrupt). Each app pops a batch of events in its ISR, does multiply-free
integer work, and writes status word(s) to the output FIFO for a host to render.

## Event ABI (current hardware — `harness/static/evt_pack.v`)
```
[31] pad   [30:24] x[6:0]   [23:17] y[6:0]   [16:1] timestep[15:0]   [0] polarity
=> x=(w>>24)&0x7F, y=(w>>17)&0x7F, ts=(w>>1)&0xFFFF, pol=w&1   (X_SHIFT=24, Y_SHIFT=17)
```
Timestamps are live/monotonic on hardware. NOTE: several upstream apps (dvs_motion, dvs_rotate,
dvs_denoise, dvs_timesurface) still use the stale low-bit layout; this suite matches `evt_pack.v`
+ `dvs_track`. The recorded `chips/fpga/*.csv` captures carry a *wrapped coarse counter*, not
real µs ts, so timing-based apps validate on synthetic / oms-meister timestamped data.

The SciDVS is 126×112 and very noisy: apps that need it apply a multiply-free 3×3
spatio-temporal correlation gate (keep an event only if >= CORR_MIN of its 8 neighbours fired
within CORR_WINDOW events — rejects hot pixels + isolated background activity), à la `dvs_track`.

## Apps
| app | what it does | output word | host viewer | state | validated |
|-----|--------------|-------------|-------------|-------|-----------|
| **dvs_stabilize** | global background-motion direction (scene stabilization) via time-surface normal-flow voting + halve-decay; CORR noise gate | `{sign\|dx, sign\|dy, octant, magnitude}`/batch | `chips/fpga/dvs_stabilize_view.py` | ~18 KB | synthetic pans (R→E,L→W,U→N,D→S) |
| **dvs_mayfly** | each event spawns a short-lived "creature" (xorshift-hash walk) in a 126×112 bit-world; timestamp-independent | `{cx,cy,new_state,step0}`/step | `chips/fpga/dvs_mayfly_view.py` | 1.8 KB | bounded ≤44% on real capture |
| **dvs_heartbeats** | per-region dominant flicker/vibration **period** (pow-2 Δt bins, leaky) — host sonifies as heartbeats | `{conf<<10\|bin<<6\|region}`/batch | `chips/fpga/dvs_heartbeats_view.py` | ~2 KB | synthetic 5/13/50 Hz |
| **dvs_oms_meister** | canonical Meister rate-based OMS (LNLN: DoE EMA bandpass, rectify-before-pool, SAT center/annulus, reciprocal-LUT divisive inhibition, adaptive threshold, LIF) | `{oms<<14\|val<<6\|row<<3\|col}`/batch | `chips/fpga/oms_meister_ref.py` | 6.6 KB | silent on global/coherent, fires on independent (1.4–1.8×) |
| **dvs_oms_dirconsensus** | best-from-benchmark OMS: per-tile 8-bin motion-**direction** histograms, flag events disagreeing with the tile's background consensus; also emits the global background direction | `{flag<<14\|z<<9\|row<<6\|col<<3\|gdir}`/batch | `chips/fpga/dvs_oms_dirconsensus_ref.py` | 9 KB | oms-meister ts recordings (bg~1%, obj~6×) |
| **dvs_apophenia** | "Apophenia Engine": coarse 32×14 decaying activity grid (halve-decay every N batches), emit argmax cell over threshold; host mirrors it 4-fold into a live Rorschach inkblot | `{xq[4:0]\|yq[8:5]\|val[16:9]\|flag[17]}` | `chips/fpga/dvs_apophenia_view.py` | ~1 KB | grid bounded, 4-fold mirror-symmetric |
| **dvs_sonar** | "Radial Motion Oracle": each event's position vs. frame center → compass octant (sign/compare) + Chebyshev radius (shift); leaky 8-wedge histogram picks the dominant wedge/batch; host animates expanding sonar ripples | `{octant[2:0]\|radius[7:3]\|pol[8]\|strength[13:9]\|flag[14]}`/batch | `chips/fpga/dvs_sonar_view.py` | 1.8 KB | synthetic circular sweep visits 6/8 octants |
| **dvs_caustics** | "Event-Caustic Refractor": refracts each event through a fake wavy water surface — a multiply-free quarter-sine LUT warp of (x,y) driven by ts phase (travelling wave); host paints shimmering underwater light-caustics | `{xr[6:0]\|yr[13:7]\|pol[14]\|strength[19:15]\|flag[20]}`/batch | `chips/fpga/dvs_caustics_view.py` | 1.4 KB | offsets bounded ≤AMP, warp displaces samples |
| **dvs_blackhole** | "Micro-Event Black Holes": per-region fast/slow leaky EMAs; `collapse=slow−fast` opens where motion was busy then abruptly stops (inverse of an activity map); host carves dark gravity wells + lensing rings | `{xq[3:0]\|yq[7:4]\|strength[12:8]\|flag[13]}`/batch | `chips/fpga/dvs_blackhole_view.py` | 1.1 KB | collapse fires only on stop; steady-active & steady-empty silent |
| **dvs_flinch** | "The Flinch": LGMD locust looming detector — active-cell **area** trend per window (translation-invariant); growing area → leaky accumulator → flinch; host is a giant eye that recoils only on a *lunge* | `{flinch[0]\|level[6:1]\|cx[13:7]\|cy[20:14]}`/window | `chips/fpga/dvs_flinch_view.py` | 1.2 KB | loom fires; pan & recede silent |
| **dvs_loom** | "The Finish-Line Loom": event-driven slit-scan (photo-finish) — three fixed 4-px vertical slits; a wrapping event-count **weft** counter is the time axis (no µs); per-(slit,4-px-y-bin) hit floor flags real threads vs sparkle; host weaves 3 cloth strips (gold=ON/indigo=OFF, faint below floor) | `{slit[1:0]\|y[8:2]\|pol[9]\|weft[16:10]\|flag[17]}`/batch (slit=3 sentinel) | `chips/fpga/dvs_loom_view.py` | 0.1 KB | slit attribution + weft arithmetic exact; sparkle all-unflagged, object flagged |
| **dvs_entropy** | "Entropy's Bloodhound": **arrow-of-time** detector — per-pixel last-polarity memory counts same-pixel ON→OFF ("decay") vs OFF→ON ("kindle") transitions per 1024-event window; D=fwd−rev outside a MARGIN dead-band is the thermodynamic verdict (reversing the stream provably swaps the counters; alternating hot-pixel chatter cancels, ≤1 per pixel); host draws a verdict needle + count meters + D history | `{fwd[9:0]\|rev[19:10]\|verdict[21:20]\|wseq[25:22]}`/batch | `chips/fpga/dvs_entropy_view.py` | 16 KB | fade-world arrow, time-mirror swap, polarity-mirror swap, Loschmidt invariance, chatter bound — all exact |
| **dvs_widdershins** | "The Widdershins Engine": communal **winding-number** counter — a ±1-step median tracker follows the activity locus; its compass octant around frame centre is sampled every 32 events and circular octant differences (odd 8-entry LUT, half-turn→0) accumulate into a signed `wind` register (eighth-turns; >0 deosil/CW, <0 widdershins/CCW, `wind>>3`=whole turns); noise/stillness freeze it via a Chebyshev RMIN dead-zone that breaks the winding chain; host draws a brass compass + wind history | `{oct[2:0]\|valid[3]\|wind[15:4]\|turns[23:16]\|wseq[27:24]\|radq[31:28]}`/batch (wind/turns two's-complement) | `chips/fpga/dvs_widdershins_view.py` | <0.1 KB | deosil laps wind==8K−1, reversed laps ==−(8K−1) (exact antisymmetry), stillness & half-turn drop pin wind==0, turns floor + wseq arithmetic — all exact |
| **dvs_vital** | "The Vitalometer": **alive-or-mechanism** séance gauge from burst TIMING only (position-invariant, x/y/pol unused) — inter-event gaps ≥GAP_MIN open a burst, bursts confirm at 4 events, inter-burst intervals fill a 32-bin half-octave log histogram (log2 via shift loop) per 1024-event window; spread = #bins above peak>>3 → tight=MECHANISM, wide=ALIVE, too-few-IBIs=DORMANT, between=LIMINAL; dense sparkle never pauses (no bursts) and singletons never confirm, so noise reads DORMANT | `{pbin[4:0]\|spread[10:5]\|total[18:11]\|verdict[20:19]\|wseq[24:21]}`/batch | `chips/fpga/dvs_vital_view.py` | 0.1 KB | metronome P=1000→bin19 spread=1 MECHANISM; 6-period jitter cycle→6 bins spread=6 ALIVE; sparkle+singleton guards DORMANT; position-invariance; log-bin exhaustive — all exact |
| **dvs_quartz** | "The Human Quartz": grades finger-**tap timing** like a crystal oscillator (position-invariant, timing only) — the dvs_vital burst detector confirms each tap; 16 consecutive in-tempo inter-tap intervals (512..32768 ticks, out-of-range **resets** the collection) yield mean tempo (`sum>>4`) + MAD jitter (`Σ\|ITI−mean\|>>4`, exactly-16 samples make both divides shifts); grade: jit≤16 QUARTZ, ≤64 METRONOME, ≤256 MORTAL HAND, else JELLY; sparkle never forms taps, singletons never confirm, bounce/walk-away reset — noise can't fake a grade | `{prog[3:0]\|meanq[14:4]\|jit[24:15]\|grade[26:25]\|sseq[30:27]}`/batch | `chips/fpga/dvs_quartz_view.py` | 0.1 KB | metronome P=2000→(3,0,62) ×3 sessions w/ exact word indices; grade ladder jit=100/16/50/1000→grades 1/3/2/0 exact; sparkle+singleton+tempo-gate all-zero; position-invariance — all exact |

All firmware: `-march=rv32i`, **zero mul/div** (verified in the linked image), fit 32 KB.

## Build & run
Firmware (on a host with the `riscv32-unknown-elf-` toolchain):
```
make -C software PROG=<app>          # -> software/build/rom.mem
```
Local compile-check (this repo uses clang here; no gcc):
```
clang --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 -O3 -ffreestanding -nostdlib \
  -fno-builtin -fuse-ld=lld -Wl,--gc-sections -T software/common/application.lds \
  -o /tmp/<app>.elf software/common/crt0.S software/<app>/main.c
```
Deploy + view via the actnow PC tool:
```
python3 harness/host/actnow_client.py --listen-host <ip> --xsa <x.xsa> --firmware software/build/rom.mem
```
Or self-test a viewer offline: `python3 chips/fpga/<app>_view.py --validate` /
`--from-actsim <results.mem>` / `<capture.csv>`.

See also: `demo_ideas.md`, `weird_demo_ideas.md`, `dangelo_attention_feasibility.md`.
