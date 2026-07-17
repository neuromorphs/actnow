# Weird demo ideas for the actnow event-driven RV32I MCU

_Brainstormed by GPT-5.6 (codex, gpt-5.6-sol), 2026-07-15. Complements the grounded ranked list in demo_ideas.md — these are the out-there / artsy / uncanny ones. All designed to be multiply-free-friendly in ~32 KB, exploiting the async-sleep + microsecond-timing + event-sparsity + darkness/HDR nature._

1. THE MACHINE THAT BLINKS FIRST

It watches a visitor’s eye and fires an LED or tiny servo in the first instant an eyelid begins moving—before the blink is consciously felt. Core trick: events in two fixed eye-shaped regions, ON/OFF edge order, timestamp differences, and a tiny state machine; no frames, identity model, multiply, or division. The visitor feels an uncanny reversal of agency: “I blinked because it flashed… didn’t I?”

2. GHOST MAINS CHOIR

Point it around the booth and let invisible LED PWM, fluorescent ballast flicker, screens, chargers, and mains-powered lamps become separate rhythmic voices. Core trick: per-region timestamp-difference histograms with power-of-two bins, shift-based leaky counters, and lookup-table pitches; sleep between photons betraying their power supplies. The audience hears the room’s electrical ecosystem singing while apparently steady lights pulse on the host display like haunted machinery.

3. STILLNESS WORSHIP ENGINE

The device accumulates “silence” only while absolutely nothing changes, gradually raising a servo-carried tiny flag or lighting a sacred LED; the smallest twitch instantly collapses the ritual. Core trick: reset a shift-based quietness counter on every event and advance it from a sparse timer wakeup. A crowd becomes absurdly invested in standing perfectly still for a computer whose highest achievement is going back to sleep.

4. SHADOW THEREMIN FOR NONEXISTENT LIMBS

A person conducts sound using only the moving boundaries of their shadow, even in harsh sun or near-darkness; their body itself need not be visible as an image. Core trick: divide the sensor into strips, maintain saturating ON/OFF event counts, and map centroid-free threshold crossings to table-driven notes. The audience hears a disembodied silhouette playing music, with crisp microsecond attacks that make it feel more like touching electricity than waving at a camera.

5. PRE-ECHO

A solenoid click, LED flash, or buzzer chirp appears to anticipate a repetitive gesture—finger tapping, pendulum swinging, hand clapping—by triggering slightly before its next occurrence. Core trick: store the last few inter-event burst intervals, select a median using compares, then schedule the next output with a fixed subtraction; no division required. After three repetitions, the machine begins interrupting causality.

6. THE ARGUMENT BETWEEN ON AND OFF

Two tiny characters—perhaps red and blue LEDs or opposed servos—interpret brightening events as optimism and darkening events as doom, constantly arguing over what the world “really” did. Core trick: polarity-specific saturating accumulators decay by right shifts and trigger canned FIFO text or table-driven sound fragments. A waving hand produces a rapid philosophical dispute because every leading edge creates hope and every trailing edge creates despair.

7. LASER GNAT CIRCUS

A laser dot becomes a nearly invisible high-speed insect that the chip “hunts” with four GPIO traps, reacting to motion faster than human vision can follow. Core trick: sparse event adjacency, timestamp windows, bit-packed occupancy tiles, and compare-only direction changes. LEDs snap around the booth as if chasing something supernatural; slow-motion host rendering reveals the chase was happening in the gaps between perception.

8. OBJECTS HAVE SECRET HEARTBEATS

Aim the camera at “dead” appliances, toys, cables, vents, or tables and sonify microscopic vibration and periodic brightness changes as heartbeats. Core trick: XOR-fold coordinates into a few spatial buckets, keep power-of-two interval bins, and promote stable timestamp deltas into pulse trains. A laptop charger wheezes, a ventilation grille growls, and a supposedly inert monitor has a frantic pulse.

9. THE DARKNESS APPLAUSE METER

Instead of measuring loudness or brightness, it measures how violently darkness changes: wave black cloth, extinguish a light, sweep a shadow, or move in a nearly black room. Core trick: count polarity bursts across coarse bitmask regions and use shifts for logarithmic intensity levels. Four LEDs or a buzzer reward the most dramatic act of subtraction, making “less light” into a physical performance.

10. TEMPORAL GRAFFITI

Visitors draw gestures too fast to see, and the host renders them as ribbons whose color encodes microsecond event age rather than spatial brightness. Core trick: retain only compact event packets or per-tile last-timestamp low bits, using wraparound subtraction and lookup palettes. A whip-crack hand motion becomes luminous calligraphy painted in time, then vanishes because the camera refuses to remember stillness.

11. THE POLTERGEIST KNOCKER

Choose a mundane object—a cup, sign, or cardboard box—and have a hidden servo knock back only when the object experiences a very particular micro-vibration signature. Core trick: a fixed region, alternating-polarity run lengths, and compare-based matching against a tiny hand-authored temporal template. Visitors tap experimentally; sometimes the object answers immediately, sometimes it pointedly ignores them.

12. MOIRÉ ORACLE

Show the camera two displays, PWM lamps, or spinning slotted objects; it emits prophecies whenever their invisible rhythms briefly align or drift through a beat frequency. Core trick: edge timestamps feed two phase accumulators, with phase represented by wraparound counters and coincidence detected by subtraction thresholds. The audience sees steady objects, but the oracle flashes, twitches, or prints cryptic messages at moments produced by their hidden temporal interference.

13. FOUR-PIN MICRO-RAVE FOR ONE FALLING COIN

Drop or spin a coin and let its glints trigger a complete audiovisual composition lasting only as long as the metal is changing. Core trick: ON/OFF burst density, event-address hashes, timestamp-gap lookup tables, and four GPIO voices; every instruction burst ends when the glint ends. The coin creates an impossibly detailed strobe percussion solo, then the entire rave—and processor—goes dead asleep the instant it settles.

14. THE BOREDOM DETECTOR

The device ignores large theatrical motion but becomes fascinated by involuntary micro-movements: fingertip tremor, fabric flutter, pulse-adjacent skin shifts, restless feet. Core trick: reject large spatial bursts, retain sparse alternating events inside tiny regions, and score persistence with saturating counters plus shift decay. Its LED turns on when someone is trying hardest to appear motionless, publicly accusing them of concealed nervous energy.

15. COMPUTATIONAL MAYFLY

Every isolated event wakes a tiny generative creature whose entire life is perhaps 20–100 instructions: it chooses a direction from coordinate bits, modifies one cell in a 126×112 bit-packed world, emits a chirp or pixel, and dies back into wait state. Core trick: coordinate XORs as randomness, cellular rules made from shifts and masks, and a world fitting in a few kilobytes. The host shows a strange ecosystem whose organisms literally exist only when something in the physical world changes—cover the lens, and time effectively stops.

---

## Host-output-only variants (no extra hardware — MCU → output FIFO → computer)
_Rui's constraint: emit words to the host, which does all the rendering/sound. Requested builds: Objects Have Secret Heartbeats + Computational Mayfly._

- **Objects Have Secret Heartbeats** (building) — per-region dominant flicker/vibration PERIOD from event Δt (pow-2 period bins, shift/compare) → host sonifies each region as a heartbeat/tone.
- **Computational Mayfly** (building) — each event spawns a ~few-step creature in a bit-packed world (coord-hash direction, shift/mask rules) → host renders the ecosystem; cover the lens and time stops.
- **Flicker Fingerprint** — classify each light by temporal signature (mains 50/60 Hz vs PWM vs steady) → host labels it.
- **Frequency Rainbow** — per-region dominant flicker frequency → host repaints the scene colored by frequency (fans/screens/bulbs each a hue).
- **The Eavesdropper** — detect a surface's periodic vibration (speaker cone / string) → emit frequency → host shows the sound you can see but not hear.
- **Event Rain (ASMR)** — forward subsampled events; host sonifies pitch=y, pan=x, ON/OFF=timbre.
- **Temporal Graffiti** — age-coded event packets → host renders fast gestures as time-colored ribbons.
- **The Boredom Detector** — score involuntary micro-motion (reject big bursts) → host meter calls out concealed nervous energy.
- **Pre-Echo** — predict next tap/clap from recent intervals (median via compares − fixed offset) → host clicks before you do.
- **Moiré Oracle** — two flicker sources' phase beat → host prints "prophecies" at alignment moments.
- **The Sleep Talker (meta)** — each wake emits {events-processed, idle-since-last} → host visualizes the chip's own duty-cycle / breathing.
