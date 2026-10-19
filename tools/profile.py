#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐级加上滤镜跑一遍，看每个 stage 各吃掉多少速度（-f null，不编码）。

    python tools/profile.py input.mp4 --start 10 --frames 48

需要先跑一次主程序并保留纹理（--keep-textures --tex-dir tex），
脚本会直接读 tex/ 里的 PNG。
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

SCALE = "scale=1920:820:flags=lanczos,setsar=1,format=gbrp"
GRADE = ("eq=contrast=1.14:gamma=1.02:saturation=0.63,"
         "curves=m='0.000/0.075 0.180/0.244 0.500/0.468 0.820/0.800 1.000/0.760'")


def run(name: str, graph: str, src: str, start: float, frames: int,
        tex: Path, inputs: list[str]) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", "10", "-i", src]
    for p in inputs:
        cmd += ["-loop", "1", "-framerate", "30", "-i", str(tex / p)]
    cmd += ["-filter_complex", graph, "-map", "[v]",
            "-frames:v", str(frames), "-f", "null", "-"]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.time() - t0
    if p.returncode != 0:
        print(f"{name:<30s} FAILED  {p.stderr.strip()[:160]}")
        return
    print(f"{name:<30s} {dt:6.2f}s   {frames / dt:6.1f} fps")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--frames", type=int, default=48)
    ap.add_argument("--tex", default="tex")
    a = ap.parse_args()
    tex = Path(a.tex)
    if not (tex / "raster.png").exists():
        print(f"找不到 {tex}/raster.png —— 先跑一次：\n"
              f"  python satellite_scan.py {a.input} -o out.mp4 --start 1 "
              f"--duration 1 --keep-textures --tex-dir {tex}")
        return 2

    f = dict(start=a.start, frames=a.frames, tex=tex)

    run("00 只有 scale", f"[0:v]{SCALE}[v]", a.input, **f, inputs=[])
    run("01 + 调色", f"[0:v]{SCALE},{GRADE}[v]", a.input, **f, inputs=[])
    run("02 + 颗粒", f"[0:v]{SCALE},{GRADE},noise=alls=6:allf=t+u[v]",
        a.input, **f, inputs=[])
    run("03 + 横向拖影", f"[0:v]{SCALE},{GRADE},noise=alls=6:allf=t+u,"
                        f"avgblur=sizeX=7:sizeY=0[v]", a.input, **f, inputs=[])
    run("04 + 光栅", f"[0:v]{SCALE},{GRADE},noise=alls=6:allf=t+u,"
                     f"avgblur=sizeX=7:sizeY=0[b];[1:v]format=gbrp[r];"
                     f"[b][r]blend=all_mode=overlay[v]",
        a.input, **f, inputs=["raster.png"])
    run("05 + 柔光", f"[0:v]{SCALE},{GRADE},noise=alls=6:allf=t+u,"
                     f"avgblur=sizeX=7:sizeY=0[b];[1:v]format=gbrp[r];"
                     f"[b][r]blend=all_mode=overlay[t1];[2:v]format=gbrp[s];"
                     f"[t1][s]blend=all_mode=screen[v]",
        a.input, **f, inputs=["raster.png", "bloom.png"])
    run("06 + 白条", f"[0:v]{SCALE},{GRADE},noise=alls=6:allf=t+u,"
                     f"avgblur=sizeX=7:sizeY=0[b];[1:v]format=gbrp[r];"
                     f"[b][r]blend=all_mode=overlay[t1];[2:v]format=gbrp[s];"
                     f"[t1][s]blend=all_mode=screen[t2];"
                     f"[3:v]format=gbrp,scroll=vertical=-0.113:horizontal=0[br];"
                     f"[t2][br]blend=all_mode=screen[v]",
        a.input, **f, inputs=["raster.png", "bloom.png", "bars.png"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
