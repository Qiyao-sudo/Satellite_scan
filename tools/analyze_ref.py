#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一张「参考帧」的扫描特征：扫描线间距/幅度、白条位置、亮度分布、色偏。

    python tools/analyze_ref.py reference.png

程序里的默认参数就是照这几项标出来的。
"""

from __future__ import annotations

import sys

import numpy as np
from PIL import Image


def analyse(path: str) -> None:
    a = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    H, W, _ = a.shape
    g = a.mean(2)
    print(f"\n== {path}  {W}x{H}  aspect {W / H:.3f}")

    p = [np.percentile(g, q) for q in (1, 5, 25, 50, 75, 95, 99)]
    print("  亮度  p1 %.1f  p5 %.1f  p25 %.1f  p50 %.1f  p75 %.1f  p95 %.1f  p99 %.1f"
          % tuple(p))
    mx, mn = a.max(2), a.min(2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    print("  色偏  RGB %.1f / %.1f / %.1f   饱和度 %.3f"
          % (*a.reshape(-1, 3).mean(0), sat.mean()))

    # 扫描线间距：行均值的高通做 FFT
    rowp = g[:, int(W * 0.3):int(W * 0.7)].mean(1)
    hp = rowp - np.convolve(rowp, np.ones(9) / 9, mode="same")
    spec = np.abs(np.fft.rfft(hp * np.hanning(len(hp))))
    freq = np.fft.rfftfreq(len(hp))
    top = np.argsort(spec[1:])[::-1][:5] + 1
    print("  扫描线  间距 %.2f px（= %.3f 画面高）  相对幅度 %.3f"
          % (np.median(1 / freq[top]), np.median(1 / freq[top]) / H,
             hp.std() / max(rowp.mean(), 1e-6)))

    # 白条：大窗口平滑后找峰，排除扫描线本身
    base = np.convolve(rowp, np.ones(121) / 121, mode="same")
    res = rowp - base
    peaks, i = [], 10
    while i < H - 10:
        if res[i] > 10 and res[i] == res[i - 8:i + 9].max():
            lo = i
            while lo > 0 and res[lo] > res[i] * 0.5:
                lo -= 1
            hi = i
            while hi < H - 1 and res[hi] > res[i] * 0.5:
                hi += 1
            peaks.append((i, res[i], hi - lo))
            i = hi + 4
        else:
            i += 1
    print("  白条  %d 根" % len(peaks))
    for y, amp, w in peaks[:8]:
        print("        y=%4d (%.3fH)  高出周围 %5.1f  半宽 %3d px (%.3fH)"
              % (y, y / H, amp, w, w / H))
    if len(peaks) > 1:
        gaps = np.diff([q[0] for q in peaks])
        print("        间距 %s px" % ", ".join(str(int(v)) for v in gaps))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for p in sys.argv[1:]:
        analyse(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
