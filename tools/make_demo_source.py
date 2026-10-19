#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一段纯合成的「航拍城市」测试源，用来演示效果。

不需要任何外部素材：整段画面由 numpy 画出来——街区、道路、河流、公园，
再加上沿马路跑的车灯和缓慢的镜头平移，保证有足够运动和色彩来检验效果。

    python tools/make_demo_source.py demo_source.mp4

输出 960x540 / 30fps / 3 秒 / 无音轨。
"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw

W, H = 960, 540
BIG = (1560, 1160)          # 先画一张大图，再在窗口里平移
DUR = 3.0
FPS = 30
SEED = 20251019


def make_scene(seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    bw, bh = BIG
    img = Image.new("RGB", (bw, bh), (96, 97, 92))
    d = ImageDraw.Draw(img)

    # 地面：低饱和的暗色底 + 大块斑驳
    base = np.asarray(img).astype(np.float32)
    yy, xx = np.mgrid[0:bh, 0:bw].astype(np.float32)
    blotch = (np.sin(xx / 130.0) * np.cos(yy / 97.0)
              + 0.6 * np.sin((xx + yy) / 61.0))
    base += blotch[..., None] * 9.0
    base += rng.normal(0, 2.2, base.shape)
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)

    # 河流：一条缓慢弯曲的带子
    pts = [(x, int(bh * 0.62 + 120 * np.sin(x / 240.0))) for x in range(-60, bw + 60, 20)]
    d.line(pts, fill=(52, 84, 112), width=64, joint="curve")
    d.line(pts, fill=(70, 112, 146), width=44, joint="curve")

    # 街区：网格 + 随机色块
    palette = [(150, 144, 134), (176, 158, 136), (136, 134, 144), (188, 170, 150),
               (118, 120, 128), (198, 178, 150), (150, 166, 158)]
    step_x, step_y = 122, 104
    for gy, y in enumerate(range(-40, bh + 40, step_y)):
        for gx, x in enumerate(range(-40, bw + 40, step_x)):
            if rng.random() < 0.14:      # 留空做广场/公园
                continue
            w = step_x - rng.integers(28, 46)
            h = step_y - rng.integers(24, 40)
            x0 = x + rng.integers(10, 22)
            y0 = y + rng.integers(8, 18)
            col = palette[rng.integers(0, len(palette))]
            col = tuple(int(c * (0.84 + 0.34 * rng.random())) for c in col)
            d.rectangle([x0, y0, x0 + w, y0 + h], fill=col)
            # 屋顶高光
            d.rectangle([x0 + 4, y0 + 4, x0 + w - 4, y0 + h - 4],
                        fill=tuple(min(255, int(c * 1.18)) for c in col))

    # 公园
    for _ in range(26):
        px, py = rng.integers(0, bw), rng.integers(0, bh)
        r = rng.integers(26, 54)
        g = int(112 + 52 * rng.random())
        d.ellipse([px - r, py - r, px + r, py + r], fill=(58, g, 62))

    # 主干道
    for y in range(0, bh, step_y):
        d.line([(0, y), (bw, y)], fill=(186, 184, 176), width=9)
        d.line([(0, y + 1), (bw, y + 1)], fill=(226, 224, 214), width=2)
    for x in range(0, bw, step_x):
        d.line([(x, 0), (x, bh)], fill=(186, 184, 176), width=9)
        d.line([(x + 1, 0), (x + 1, bh)], fill=(226, 224, 214), width=2)
    return img


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "demo_source.mp4"
    scene = make_scene(SEED)
    bw, bh = scene.size
    rng = np.random.default_rng(SEED + 1)

    # 车灯：沿横竖干道跑
    cars = []
    for _ in range(46):
        horiz = rng.random() < 0.55
        lane = int(rng.integers(0, (bh if horiz else bw) // 100) * 100 + 4)
        speed = rng.uniform(70, 190) * (1 if rng.random() < 0.5 else -1)
        pos = rng.uniform(0, bw if horiz else bh)
        col = (255, 214, 120) if rng.random() < 0.6 else (255, 250, 235)
        cars.append((horiz, lane, speed, pos, col))

    frames = int(DUR * FPS)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "16",
         "-pix_fmt", "yuv420p", out],
        stdin=subprocess.PIPE)

    for i in range(frames):
        t = i / FPS
        img = scene.copy()
        d = ImageDraw.Draw(img)
        for horiz, lane, speed, pos, col in cars:
            p = (pos + speed * t) % (bw if horiz else bh)
            if horiz:
                d.rectangle([p, lane, p + 10, lane + 3], fill=col)
            else:
                d.rectangle([lane, p, lane + 3, p + 10], fill=col)
        # 缓慢平移 + 轻微呼吸，制造运动
        ox = int(300 + 260 * t / DUR)
        oy = int(280 + 120 * t / DUR + 10 * np.sin(t * 2.1))
        frame = img.crop((ox, oy, ox + W, oy + H))
        proc.stdin.write(frame.tobytes())

    proc.stdin.close()
    proc.wait()
    print(f"已生成 {out}  {W}x{H}  {FPS}fps  {DUR}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
