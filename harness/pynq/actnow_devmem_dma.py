#!/usr/bin/env python3
"""Drain the ActNow result stream (dma_res, Simple-mode S2MM) over /dev/mem,
using a contiguous CMA buffer allocated from the zocl DRM node (renderD128).

PYNQ/XRT can't enumerate the PL device on this image, so this bypasses both:
- zocl DRM ioctls allocate a physically-contiguous BO and report its paddr;
- the AXI-DMA S2MM channel is driven directly by MMIO register pokes.

If results was pinned at ~516 (the 512-deep result FIFO full, core backpressured),
draining it must make the fifo_in-fixed core resume and `results` climb past 516.
"""
import fcntl, mmap, os, struct, time
import actnow_mmio as A

# ---- zocl DRM ioctls (DRM_COMMAND_BASE=0x40, type 'd'=0x64) ----
def IOWR(nr, size): return 0xC0000000 | (size << 16) | (0x64 << 8) | nr
DRM_IOCTL_ZOCL_CREATE_BO = IOWR(0x40, 16)   # nr 0x0  {u64 size; u32 handle; u32 flags}
DRM_IOCTL_ZOCL_MAP_BO    = IOWR(0x43, 16)   # nr 0x3  {u32 handle; u32 pad; u64 offset}
DRM_IOCTL_ZOCL_INFO_BO   = IOWR(0x45, 24)   # nr 0x5  {u32 handle; u32 pad; u64 size; u64 paddr}
BO_FLAGS_CMA = 0x1 << 28

class ZoclBuf:
    def __init__(self, nbytes):
        self.n = nbytes
        self.fd = os.open("/dev/dri/renderD128", os.O_RDWR)
        b = struct.pack("QII", nbytes, 0, BO_FLAGS_CMA)
        b = fcntl.ioctl(self.fd, DRM_IOCTL_ZOCL_CREATE_BO, b)
        _sz, self.handle, _fl = struct.unpack("QII", b)
        b = struct.pack("IIQQ", self.handle, 0, 0, 0)
        b = fcntl.ioctl(self.fd, DRM_IOCTL_ZOCL_INFO_BO, b)
        _h, _p, _s, self.paddr = struct.unpack("IIQQ", b)
        b = struct.pack("IIQ", self.handle, 0, 0)
        b = fcntl.ioctl(self.fd, DRM_IOCTL_ZOCL_MAP_BO, b)
        _h, _p, offset = struct.unpack("IIQ", b)
        self.map = mmap.mmap(self.fd, nbytes, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE, offset=offset)

    def word(self, i):
        return struct.unpack("<I", self.map[i*4:i*4+4])[0]

# ---- AXI-DMA S2MM (Simple mode) registers, dma_res @ 0x80010000 ----
DMA_RES = 0x80010000
S2MM_DMACR, S2MM_DMASR, S2MM_DA, S2MM_DA_MSB, S2MM_LENGTH = 0x30, 0x34, 0x48, 0x4C, 0x58

def s2mm_oneshot(mem, paddr, nbytes):
    mem.wr(DMA_RES, S2MM_DMACR, 0x4)               # reset
    time.sleep(0.001)
    mem.wr(DMA_RES, S2MM_DMACR, 0x1)               # run (RS=1)
    mem.wr(DMA_RES, S2MM_DA, paddr & 0xFFFFFFFF)
    mem.wr(DMA_RES, S2MM_DA_MSB, (paddr >> 32) & 0xFFFFFFFF)
    mem.wr(DMA_RES, S2MM_LENGTH, nbytes)           # writing length starts it
    t0 = time.time()
    while time.time() - t0 < 1.0:
        sr = mem.rd(DMA_RES, S2MM_DMASR)
        if sr & 0x2:                               # Idle
            break
        time.sleep(0.001)
    transferred = mem.rd(DMA_RES, S2MM_LENGTH)     # bytes actually written
    return sr, transferred

def main():
    mem = A.Mem()
    buf = ZoclBuf(256 * 4 * 8)                      # room for several packets
    print("zocl BO: paddr=0x%x size=%d" % (buf.paddr, buf.n))
    assert buf.paddr < 2**32, "buffer above 4GB, dma is 32-bit"

    print("before: results=%d" % A.counters(mem)["results"])
    for k in range(6):
        sr, nb = s2mm_oneshot(mem, buf.paddr, 256 * 4)
        nwords = nb // 4
        sample = [buf.word(i) for i in range(min(nwords, 4))]
        c = A.counters(mem)
        print("drain %d: dmasr=0x%08x got %d words, first=%s ... results=%d evt=%d"
              % (k, sr, nwords, [hex(w) for w in sample], c["results"], c["evt"]))
        time.sleep(0.2)

if __name__ == "__main__":
    main()
