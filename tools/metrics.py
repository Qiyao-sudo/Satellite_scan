#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一帧画面的「扫描感」指标，用来对照参考帧或比较不同参数。

    python tools/metrics.py a.png b.png c.png

输出：
    p1 / p5 / p25 / p50 / p75 / p95 / p99   亮度分位
    RGB                                      三通道均值（看色偏）
    sat                                      平均饱和度
    dx/dy                                    横/纵细节比（越小越"横向拖影"）
    raster                                   扫描线调制幅度（相对亮度）
"""

from __future__ import annotations

import sys

import numpy as np
from PIL import Image


def metrics(path: str, label: str = "") -> dict:
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(np.float32)
    H, W, _ = a.shape
    g = a.mean(2)
    mx, mn = a.max(2), a.min(2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    p = [np.percentile(g, q) for q in (1, 5, 25, 50, 75, 95, 99)]

    # 先按 2x2 平均掉扫描线和像素噪声，再量各向异性
    g2 = g[: H // 2 * 2, : W // 2 * 2]
    b = (g2[0::2, :] + g2[1::2, :]) / 2
    dx = np.abs(np.diff(b, axis=1)).mean()
    c = (b[:, 0::2] + b[:, 1::2]) / 2
    dy = np.abs(np.diff(c, axis=0)).mean()

    # 扫描线：中间窄条的纵向高通
    rp = g[:, int(W * 0.45):int(W * 0.55)].mean(1)
    hp = rp - np.convolve(rp, np.ones(9) / 9, mode="same")

    return dict(label=label or path.split("/")[-1].split("\\")[-1],
                size=f"{W}x{H}", p=p, rgb=a.reshape(-1, 3).mean(0),
                sat=sat.mean(), dxdy=dx / max(dy, 1e-6),
                raster=hp.std() / max(rp.mean(), 1e-6))


def show(m: dict) -> None:
    r, gg, b = m["rgb"]
    p = m["p"]
    print(f"{m['label']:<26s} {m['size']:>10s}  "
          + "  ".join(f"p{q} {v:5.1f}" for q, v in zip((1, 5, 25, 50, 75, 95, 99), p))
          + f"  | RGB {r:5.1f}/{gg:5.1f}/{b:5.1f}  sat {m['sat']:.3f}"
            f"  dx/dy {m['dxdy']:.3f}  raster {m['raster']:.3f}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for p in sys.argv[1:]:
        show(metrics(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
