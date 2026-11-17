#!/usr/bin/env python3
"""xref_scan.py — find adrp+add pairs in an AArch64 ELF that compute given addresses."""
import re
import struct
import subprocess
import sys

path = sys.argv[1]
targets = [int(a, 16) for a in sys.argv[2:]]

out = subprocess.run(["readelf", "-S", path], capture_output=True, text=True).stdout
secs = {}
for m in re.finditer(r"\[\s*\d+\]\s+(\S+)\s+\S+\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)", out):
    secs[m.group(1)] = (int(m.group(2), 16), int(m.group(3), 16), int(m.group(4), 16))
addr, off, size = secs[".text"]
data = open(path, "rb").read()
text = data[off:off + size]

adrp_regs = {}  # reg -> (pc, page)
hits = []
for i in range(0, len(text) - 4, 4):
    w = struct.unpack("<I", text[i:i + 4])[0]
    pc = addr + i
    # adrp: 1 immlo(2) 10000 immhi(19) rd(5)
    if (w & 0x9F000000) == 0x90000000:
        rd = w & 0x1F
        immlo = (w >> 29) & 3
        immhi = (w >> 5) & 0x7FFFF
        imm = (immhi << 2) | immlo
        if imm & (1 << 20):
            imm -= 1 << 21
        page = (pc & ~0xFFF) + (imm << 12)
        adrp_regs[rd] = (pc, page)
        continue
    # add imm 64-bit: sf=1 op=0 S=0 100010 shift=00 imm12 rn rd
    if (w & 0xFF800000) == 0x91000000:
        rd = w & 0x1F
        rn = (w >> 5) & 0x1F
        imm12 = (w >> 10) & 0xFFF
        shift = (w >> 22) & 3
        if rn in adrp_regs and adrp_regs[rn][0] >= pc - 0x40:  # near adrp
            a = adrp_regs[rn][1] + (imm12 << (12 if shift else 0))
            for t in targets:
                if t <= a < t + 16:
                    hits.append((pc, rd, a, t))

seen = set()
for pc, rd, a, t in hits:
    if pc in seen:
        continue
    seen.add(pc)
    print(f"pc {pc:#x} (x{rd}) -> {a:#x}  target {t:#x}")
