#!/usr/bin/env python3
"""Raw AER viewer sender via the GPIO last_event path (no DMA).

The DMA event path on this board is corrupted by a block-design width mismatch
(the 32-bit S2MM DMA strides its writes into the 128-bit PS HP port, mangling the
data -- see actnow_devmem_send.py). Until that's fixed in the BD, this streams the
raw camera the way kr260_aer_interface's original kr260_aer_send.py did: poll the
`last_event` AXI-GPIO register, forward each new value over UDP. This is the
KNOWN-GOOD path -- clean spatially, just poll-rate limited (bursts undersample).

    raw camera -> :3333  (aer_udp_viewer.py --port 3333)

Run (on the KR260, after `sudo fpgautil -b actnow_fixed.bit`):
    sudo python3 actnow_gpio_send.py --host <HOST_IP>
"""
import argparse, socket, struct, time
import actnow_mmio as A

HDR = struct.Struct("<4sIH")
MAGIC = b"AER1"
GPIO_S2 = 0x80050000        # ch2 @0x8 = last_event {pol, y[6:0], x[6:0]}
LAST_EVENT = 0x8


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--batch", type=int, default=64, help="events per UDP datagram")
    ap.add_argument("--stats-s", type=float, default=2.0)
    args = ap.parse_args()

    mem = A.Mem()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = (args.host, args.port)

    print(f"streaming raw last_event -> {args.host}:{args.port} (poll mode)")
    seq = 0
    sent = 0
    last = -1
    body = bytearray()
    cnt = 0
    t_stat = time.time()
    while True:
        w = mem.rd(GPIO_S2, LAST_EVENT)
        if w != last:                       # a new event latched
            last = w
            x = w & 0x7F
            y = (w >> 7) & 0x7F
            pol = (w >> 14) & 0x1
            body += struct.pack("<BBB", x, y, pol)
            cnt += 1
            if cnt >= args.batch:
                sock.sendto(HDR.pack(MAGIC, seq, cnt) + bytes(body), dst)
                seq += 1
                sent += cnt
                body = bytearray()
                cnt = 0
        if time.time() - t_stat >= args.stats_s:
            t_stat = time.time()
            print(f"sent {sent} events in {seq} packets  last=0x{last & 0xffffffff:08x}")


if __name__ == "__main__":
    main()
