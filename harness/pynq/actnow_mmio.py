#!/usr/bin/env python3
"""ActNow DVS harness bring-up over raw /dev/mem (no PYNQ/XRT needed).

This board's Ubuntu image has no XRT-registered PL device, so pynq.Overlay
fails ("No Devices Found") -- exactly what kr260_aer_interface hit. Bitstream is
loaded with `fpgautil -b actnow.bit`; here we drive the AXI-GPIO / AXI-BRAM-Ctrl
slaves directly via mmap on /dev/mem, the same way that project pokes its GPIO.

Address map (from actnow_aer_kr260.hwh):
  gpio_ctrl 0x80020000  ch1@0x0 = reset(out), ch2@0x8 = decim(out)
  gpio_s0   0x80030000  ch1@0x0 = req_count,   ch2@0x8 = evt_count
  gpio_s1   0x80040000  ch1@0x0 = core_drop,   ch2@0x8 = results
  gpio_s2   0x80050000  ch1@0x0 = fetch_count, ch2@0x8 = last_event
  bram_ctrl 0x82000000  firmware BRAM (word i at +i*4)
"""
import argparse, mmap, os, struct, time

GPIO_CTRL = 0x80020000
GPIO_S0   = 0x80030000
GPIO_S1   = 0x80040000
GPIO_S2   = 0x80050000
BRAM      = 0x82000000
MAP_SZ    = 0x10000
CH1, CH2  = 0x0, 0x8


class Mem:
    def __init__(self):
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self._maps = {}

    def _map(self, base):
        if base not in self._maps:
            self._maps[base] = mmap.mmap(self.fd, MAP_SZ, mmap.MAP_SHARED,
                                         mmap.PROT_READ | mmap.PROT_WRITE,
                                         offset=base)
        return self._maps[base]

    def rd(self, base, off):
        m = self._map(base)
        return struct.unpack("<I", m[off:off+4])[0]

    def wr(self, base, off, val):
        m = self._map(base)
        m[off:off+4] = struct.pack("<I", val & 0xFFFFFFFF)


def load_firmware(mem, path):
    with open(path) as f:
        words = [int(l.strip(), 2) for l in f if l.strip()]
    for i, w in enumerate(words):
        mem.wr(BRAM, i * 4, w)
    back = mem.rd(BRAM, 0)
    if back != words[0]:
        raise RuntimeError(f"BRAM readback mismatch: wrote {words[0]:#010x} read {back:#010x}")
    return len(words), words[0]


def reset_core(mem):
    mem.wr(GPIO_CTRL, CH1, 0x0)
    time.sleep(0.01)
    mem.wr(GPIO_CTRL, CH1, 0x1)
    time.sleep(0.01)
    mem.wr(GPIO_CTRL, CH1, 0x0)


def set_decim(mem, core_n, raw_n=0):
    mem.wr(GPIO_CTRL, CH2, ((raw_n & 0xFFFF) << 16) | (core_n & 0xFFFF))


def counters(mem):
    return dict(
        req=mem.rd(GPIO_S0, CH1), evt=mem.rd(GPIO_S0, CH2),
        core_drop=mem.rd(GPIO_S1, CH1), results=mem.rd(GPIO_S1, CH2),
        fetch=mem.rd(GPIO_S2, CH1), last=mem.rd(GPIO_S2, CH2),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--firmware", default="rom.mem")
    ap.add_argument("--decim", type=int, default=8)
    ap.add_argument("--watch", type=float, default=0.0, help="poll counters every N s (0=one shot)")
    ap.add_argument("--no-reset", action="store_true", help="just read counters")
    args = ap.parse_args()
    mem = Mem()

    if not args.no_reset:
        n, w0 = load_firmware(mem, args.firmware)
        print(f"firmware: {n} words to BRAM, word0=0x{w0:08x} (readback OK)")
        set_decim(mem, args.decim)
        reset_core(mem)
        print(f"core reset pulsed; decim=every {args.decim}th event")
        time.sleep(0.2)

    c = counters(mem)
    print("fetch={fetch}  req={req} evt={evt}  results={results} "
          "core_drop={core_drop}  last=0x{last:08x}".format(**c))
    if c["fetch"] == 0 and not args.no_reset:
        print("WARNING: fetch=0 -> core never booted (reset didn't reach reset_ext, "
              "or BRAM not answering). HW_BRINGUP step 3.")
    elif not args.no_reset:
        print(f"core booted: {c['fetch']} ROM fetches")

    if args.watch > 0:
        try:
            while True:
                time.sleep(args.watch)
                print("  " + "fetch={fetch} req={req} evt={evt} results={results} "
                      "core_drop={core_drop} last=0x{last:08x}".format(**counters(mem)))
        except KeyboardInterrupt:
            print("done")


if __name__ == "__main__":
    main()
