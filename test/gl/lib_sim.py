#!/usr/bin/env python3
"""Write a simulation copy of the PDK cell library with edge paths made plain.

iverilog cannot match SDF "IOPATH S Z" to a `specify` path like
`(posedge S => (Z : ...))`, so that arc silently keeps the library's 1 ns default.
Only `specify` text changes; the script fails if anything else would.

    lib_sim.py <pdk_cells.v> <out.v>
"""
import re
import sys

EDGE_PATH = r"\((?:posedge|negedge)\s+(\w+)\s*=>\s*\(\s*(\w+)\s*:\s*\w+\s*\)\s*\)"


def strip_specify(text: str) -> str:
    return re.sub(r"\bspecify\b.*?\bendspecify\b", "", text, flags=re.S)


src, dst = sys.argv[1:3]
original = open(src).read()
rewritten, count = re.subn(EDGE_PATH, r"(\1 => \2)", original)
if strip_specify(original) != strip_specify(rewritten):
    sys.exit("lib_sim: functional logic changed")
open(dst, "w").write(rewritten)
print(f"lib_sim: {count} edge paths rewritten, functional logic identical")
