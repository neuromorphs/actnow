#!/usr/bin/env python3
"""End-to-end ActNow DVS sender over raw /dev/mem + zocl DMA (no PYNQ/XRT).

The /dev/mem equivalent of actnow_dvs_send.py, for this board's broken PYNQ
stack. Boots the core out of the firmware BRAM, then runs one DMA-drain loop per
stream, forwarding each packet to the host as a UDP datagram in
kr260_aer_interface's existing format -- so aer_udp_viewer.py works unchanged:

    stream A (:3333)  raw camera events, straight off the AER receiver
    stream B (:3334)  what the ActNow core made of the same events (word+1)

Datagram (identical to kr260_aer_send.py / actnow_dvs_send.py):
    header "<4sIH" = magic b"AER1", seq (uint32), n (uint16), then n x "<BBB" = (x,y,pol)

Run (on the KR260, after `sudo fpgautil -b actnow_fixed.bit`):
    sudo python3 actnow_devmem_send.py --host <HOST_IP> --firmware rom.mem --decim 8
"""
import argparse, socket, struct, threading, time
import actnow_mmio as A
from actnow_devmem_dma import ZoclBuf, S2MM_DMACR, S2MM_DMASR, S2MM_DA, S2MM_DA_MSB, S2MM_LENGTH

HDR = struct.Struct("<4sIH")
MAGIC = b"AER1"
DMA_RAW = 0x80000000
DMA_RES = 0x80010000
PKT_WORDS = 256                      # == axis_pack_fifo PKT_WORDS: the PL never sends longer


def decode(w):                       # {ts[16:0], pol, y[6:0], x[6:0]}
    return w & 0x7F, (w >> 7) & 0x7F, (w >> 14) & 0x1


def s2mm_oneshot(mem, base, paddr, nbytes, timeout=0.5):
    """One Simple-mode S2MM transfer on the DMA at `base`; returns bytes written."""
    mem.wr(base, S2MM_DMACR, 0x4)               # reset
    time.sleep(0.0005)
    mem.wr(base, S2MM_DMACR, 0x1)               # run (RS=1)
    mem.wr(base, S2MM_DA, paddr & 0xFFFFFFFF)
    mem.wr(base, S2MM_DA_MSB, (paddr >> 32) & 0xFFFFFFFF)
    mem.wr(base, S2MM_LENGTH, nbytes)           # writing length starts the transfer
    t0 = time.time()
    while time.time() - t0 < timeout:
        if mem.rd(base, S2MM_DMASR) & 0x2:      # Idle -> transfer done (tlast or full)
            break
        time.sleep(0.0002)
    return mem.rd(base, S2MM_LENGTH)


def stream_loop(base, host, port, stop, name):
    """One DMA -> UDP pump. Own /dev/mem mapping + own DMA buffer, so the two
    streams never touch shared state."""
    mem = A.Mem()
    buf = ZoclBuf(PKT_WORDS * 4)
    assert buf.paddr < 2**32, "DMA buffer above 4GB (dma is 32-bit)"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = 0
    while not stop.is_set():
        n = s2mm_oneshot(mem, base, buf.paddr, PKT_WORDS * 4) // 4
        if n == 0:
            time.sleep(0.001)                   # quiet scene: nothing latched
            continue
        body = bytearray()
        for i in range(n):
            x, y, pol = decode(buf.word(i))
            body += struct.pack("<BBB", x, y, pol)
        sock.sendto(HDR.pack(MAGIC, seq, n) + bytes(body), (host, port))
        seq += 1
    print(f"[{name}] stopped after {seq} packets")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="host to send both streams to")
    ap.add_argument("--port-raw", type=int, default=3333)
    ap.add_argument("--port-core", type=int, default=3334)
    ap.add_argument("--firmware", default="rom.mem")
    ap.add_argument("--decim", type=int, default=8,
                    help="feed the core every Nth event (it is ~100x slower than the sensor)")
    ap.add_argument("--stats-s", type=float, default=2.0)
    args = ap.parse_args()

    mem = A.Mem()
    nwords, _w0 = A.load_firmware(mem, args.firmware)   # (count, word0); raises on readback mismatch
    A.set_decim(mem, core_n=args.decim)
    A.reset_core(mem)
    time.sleep(0.2)
    c = A.counters(mem)
    print(f"firmware written; core reset. fetch={c['fetch']} "
          f"({'booted' if c['fetch'] else 'NOT BOOTING -- see HW_BRINGUP step 3'})")

    stop = threading.Event()
    threads = [
        threading.Thread(target=stream_loop, args=(DMA_RAW, args.host, args.port_raw, stop, "raw"), daemon=True),
        threading.Thread(target=stream_loop, args=(DMA_RES, args.host, args.port_core, stop, "core"), daemon=True),
    ]
    for t in threads:
        t.start()
    print(f"streaming: raw -> {args.host}:{args.port_raw}, core -> {args.host}:{args.port_core}")
    try:
        while True:
            time.sleep(args.stats_s)
            c = A.counters(mem)
            print("evt={evt} results={results} core_drop={core_drop} "
                  "fetch={fetch} last=0x{last:08x}".format(**c))
    except KeyboardInterrupt:
        stop.set()
        time.sleep(0.5)
        print("stopped")


if __name__ == "__main__":
    main()
