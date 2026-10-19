#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
satellite_scan.py —— 把普通视频转成「人造卫星光学扫描」质感的视频
====================================================================

Turn any video into a "satellite optical scan" feed: horizontal raster, a white
scan bar sweeping down the frame, and an optional CRT-surveillance grade.

灵感来自罗大佑《首都》MV 里「但是人造卫星看得更真更早」那组镜头：

    · 白条自上而下扫描：一屏 3 根，一条细白线 + 一圈更淡更粗的边框
    · 逐行扫描光栅：横向扫描线，带相位扭曲与逐行亮度抖动
    · 行内横向拖影：线扫传感器沿 x 方向积分，x 糊、y 保留细节
    · 颗粒噪声（也在 x 方向被拖成横丝）、轻微色差、四角压暗
    · 可选「监视器」调色：抬黑压白、去饱和、偏紫灰

用法
----
    # 整片转换（默认 1920x820 / 2.34:1 / 3 倍速）
    python satellite_scan.py input.mkv -o output.mkv

    # 原速；换风格；只转一段并加 HUD
    python satellite_scan.py in.mp4 -o out.mp4 --speed 1
    python satellite_scan.py in.mp4 -o out.mp4 --look natural
    python satellite_scan.py in.mkv -o clip.mkv --start 271.5 --duration 12 --hud full

    # 先出一张静帧看效果（调参时最快）
    python satellite_scan.py in.mp4 -o look.png --start 6 --still

依赖：ffmpeg（含 libx264）、numpy、Pillow。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict, fields
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

__version__ = "1.0.0"

# ----------------------------------------------------------------------------
# 调色预设
# ----------------------------------------------------------------------------
# satellite 的数值来自对参考帧的实测：抬黑（p1≈23）、压白（p99≈198）、
# 去饱和（饱和度均值 0.19）、偏紫灰（R≈B>G）。
LOOKS = {
    # 参考帧的"原教旨"版本（默认）：抬黑压白、去饱和、偏紫灰，
    # 数值对着 MV 参考帧标定，也是用户认可的"旧版配置"。
    "satellite": dict(
        contrast=1.14, gamma=1.02, saturation=0.63,
        lift=0.075, white=0.76,
        tint=(+0.014, -0.020, +0.030),
        tint_shadow=(+0.010, -0.006, +0.045),
        extra="",
        line_depth=0.26, hblur=7, grain=6, bloom=0.14,
        vignette=0.55, band_amp=0.16,
        bar_amp=0.70, bar_fringe=0.050, bar_core=0.0028, bar_fall=0.010,
    ),
    # 中间档：保留原片色彩和反差，扫描层只是盖上去
    "refined": dict(
        contrast=1.05, gamma=1.00, saturation=0.88,
        lift=0.033, white=0.94,
        tint=(+0.005, -0.008, +0.013),
        tint_shadow=(+0.004, -0.002, +0.018),
        extra="",
        line_depth=0.18, hblur=5, grain=3, bloom=0.09,
        vignette=0.34, band_amp=0.09,
        bar_amp=0.56, bar_fringe=0.041, bar_core=0.0024, bar_fall=0.008,
    ),
    # 颜色一点都不动：调色、暗角、色差全关，只加白条 + 一层很淡的扫描线
    "natural": dict(
        contrast=1.00, gamma=1.00, saturation=1.00,
        lift=0.0, white=1.0,
        tint=(0.0, 0.0, 0.0),
        tint_shadow=(0.0, 0.0, 0.0),
        extra="",
        line_depth=0.12, hblur=3, grain=2, bloom=0.06,
        vignette=0.0, band_amp=0.0, chroma_split=0,
        bar_amp=0.62, bar_fringe=0.042, bar_core=0.0024, bar_fall=0.008,
        bar_speed=0.30,
    ),
    # 热成像：灰度 -> 熔岩色表
    "thermal": dict(
        contrast=1.18, gamma=0.92, saturation=1.35,
        lift=0.020, white=0.96,
        tint=(0.0, 0.0, 0.0), tint_shadow=(0.0, 0.0, 0.0),
        extra="format=yuv444p,pseudocolor=preset=inferno",
    ),
    # 夜视：绿
    "night": dict(
        contrast=1.12, gamma=1.06, saturation=0.30,
        lift=0.035, white=0.87,
        tint=(-0.020, +0.055, -0.045),
        tint_shadow=(-0.030, +0.060, -0.055),
        extra="",
    ),
}
DEFAULT_LOOK = "satellite"


@dataclass
class Look:
    """一次转换的全部可调参数。默认值对齐参考帧的实测统计量。"""

    width: int = 1920
    height: int = 820
    fit: str = "stretch"          # stretch | cover | pad
    look: str = "satellite"

    # ---- 光栅 ----
    pitch: float = 0.0082         # 扫描线间距 = pitch × 画面高（参考帧实测 5.2/646）
    line_depth: float = 0.26      # 扫描线明暗幅度（±，相对亮度）
    line_wobble: float = 1.2      # 扫描线横向相位漂移（像素）
    bloom: float = 0.14           # 扫描线的柔光溢出

    # ---- 传感器 ----
    hblur: int = 7                # 行内横向拖影半径（1920 宽下的像素，按宽度等比缩放）
    grain: int = 6                # 颗粒噪声强度（0..100）

    # ---- 白条扫描（这套效果的主角）----
    bar_amp: float = 0.70         # 白条峰值亮度（screen 混合量，1.0 = 压成死白）
    bar_speed: float = 0.34       # 白条下移速度（每秒多少个画面高）
    bar_period: float = 3.0       # 一屏里有几根白条（整数才能无缝循环）
    bar_core: float = 0.0028      # 白条本体（细亮线）半宽，画面高的比例
    bar_fringe: float = 0.050     # 白条外面那圈“稍淡的边框”半宽 <- 想更粗就调大
    bar_fall: float = 0.010       # 下沿余晖的衰减长度
    bar_luma_only: int = 0        # 1 = 只提亮度不动色度（颜色完全不变）

    # ---- 走带 / 扫描 ----
    band_amp: float = 0.16        # 粗亮带强度（overlay 混合，0.5 为中性）
    band_scroll: float = 0.05     # 亮带上滚速度（每秒多少个画面高）
    sweep_speed: float = 0.0      # 单根扫描光束速度（画面高/秒），0 = 关
    sweep_gain: float = 0.0       # 扫描光束亮度

    # ---- 调色 ----
    contrast: float = 1.14
    gamma: float = 1.02
    saturation: float = 0.63
    lift: float = 0.075
    white: float = 0.76
    tint: tuple = (+0.014, -0.020, +0.030)
    tint_shadow: tuple = (+0.010, -0.006, +0.045)
    extra: str = ""

    # ---- 其他 ----
    vignette: float = 0.55        # 0 关闭
    chroma_split: int = 1         # 色差分离像素
    hud: str = "none"             # none | basic | full
    fps: float = 0.0              # 0 = 保持源帧率
    speed: float = 3.0            # 输出倍速（1 = 原速）
    seed: int = 20251019

    def scaled(self, k: float) -> "Look":
        """整体强度缩放：1.0 默认，0 等于原片，>1 更狠。"""
        k = max(0.0, float(k))
        d = Look(**asdict(self))
        d.line_depth *= k
        d.line_wobble *= k
        d.bloom *= k
        d.hblur = int(round(self.hblur * k))
        d.grain = int(round(self.grain * k))
        d.bar_amp = min(1.0, self.bar_amp * k)
        d.band_amp = min(0.95, self.band_amp * k)
        d.sweep_gain *= k
        d.vignette = min(1.0, self.vignette * k)
        d.saturation = 1.0 + (self.saturation - 1.0) * k
        d.contrast = 1.0 + (self.contrast - 1.0) * k
        d.lift = self.lift * k
        d.white = 1.0 + (self.white - 1.0) * k
        d.tint = tuple(v * k for v in self.tint)
        d.tint_shadow = tuple(v * k for v in self.tint_shadow)
        d.chroma_split = int(round(self.chroma_split * k))
        return d


# ----------------------------------------------------------------------------
# 纹理合成
# ----------------------------------------------------------------------------
def _ridge(n: int, sharp: float = 0.16, center: float = 0.18) -> np.ndarray:
    """一个扫描周期内的光束截面：亮脊 + 暗缝。"""
    f = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.exp(-((f - center) ** 2) / (2.0 * sharp ** 2))


def _box_blur_v(a: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return a
    k = np.ones(2 * r + 1) / (2 * r + 1)
    out = np.empty_like(a)
    for i in range(a.shape[1]):
        out[:, i] = np.convolve(a[:, i], k, mode="same")
    return out


def make_raster(W: int, H: int, pitch: float, depth: float, wobble: float,
                bloom: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """返回 (光栅纹理, 柔光纹理)，均为 8bit 灰度，128 为中性。

    参考帧实测：间距 ≈ 0.8% 画面高；明暗幅度约为可见亮度的 ±10%；
    扫描线相位沿 x 缓慢漂移（相隔 1100px 时相关系数已降到 0.05）。
    """
    rng = np.random.default_rng(seed)
    x = np.arange(W, dtype=np.float64) / max(W, 1)

    # 沿 x 的相位漂移：几个低频正弦叠加 -> 有机的 S 形起伏
    wob = wobble * (
        0.55 * np.sin(2 * np.pi * (1.0 * x + 0.11))
        + 0.30 * np.sin(2 * np.pi * (2.7 * x + 0.53))
        + 0.15 * np.sin(2 * np.pi * (5.3 * x + 0.77))
    )

    period = max(2.0, pitch)
    n_prof = 64
    prof = _ridge(n_prof)
    prof = (prof - prof.mean()) / (np.abs(prof - prof.mean()).max() + 1e-9)

    y = np.arange(H, dtype=np.float64)[:, None]
    t = (y + wob[None, :]) / period
    frac = t - np.floor(t)
    ridge = prof[(frac * n_prof).astype(np.int32) % n_prof]

    # 逐行亮度抖动（模拟模拟信号的幅度不稳）
    n_lines = int(math.ceil(H / period)) + 3
    jit = rng.normal(0.0, 1.0, n_lines + 4)
    jit = np.convolve(jit, [0.25, 0.5, 0.25], mode="same")
    jit /= (np.abs(jit).max() + 1e-9)
    line_id = (np.floor(t).astype(np.int64) + n_lines) % n_lines
    jitter = 1.0 + 0.45 * jit[line_id]

    env = np.clip(ridge * jitter, -1.0, 1.0)
    raster = np.clip(128.0 + 127.0 * depth * env, 0, 255).astype(np.uint8)

    soft = np.clip(255.0 * bloom * np.clip(ridge, 0.0, 1.0) ** 2, 0, 255)
    soft = _box_blur_v(soft, max(1, int(round(period * 0.55))))
    return raster, soft.astype(np.uint8)


def make_bars(W: int, H: int, count: float, core_w: float, fringe_w: float,
              fall: float, amp: float, seed: int) -> np.ndarray:
    """白条扫描层：黑底 + 若干根白色横杠，向下循环滚动（screen 混合）。

    参考帧实测，一根白条其实由三部分叠出来：
      · 本体：一条细而硬的白亮线
      · 边框：紧贴着本体、**颜色稍淡一点**的一条粗带（不是光晕，是一条实带）
      · 下沿：约 0.02~0.03H 内就掉回背景，边缘很实（上沿则由边框自然收住）
    峰值只比周围高 30~58 灰阶，并不是纯白压死。
    一屏 3 根，间隔 ≈ 0.32 画面高；同一条白条沿 x 的亮度也不均匀。
    """
    rng = np.random.default_rng(seed + 21)
    n = max(1, int(round(count)))
    period = H / float(n)

    # 在一个周期内造形，再按周期铺满整屏 -> 滚到任何位置都无缝
    t = np.arange(H, dtype=np.float64) % period
    d = t - 0.45 * period                       # 相对白条中心的有符号距离
    d = np.where(d > period / 2, d - period, d)  # 折回 [-period/2, period/2]
    z = d / H                                   # 归一化成“画面高的比例”，z>0 在下方

    core = np.exp(-((z / max(1e-4, core_w)) ** 2))        # 本体：细亮线
    # 边框：顶是平的、边是清楚的淡白带（不是渐隐的晕）
    #   0.72 以内满亮，0.72→1.0 用 smoothstep 收边 -> 看得见"带子在哪里结束"
    u = np.abs(z) / max(1e-4, fringe_w)
    s = np.clip((u - 0.72) / 0.30, 0.0, 1.0)
    fringe = 1.0 - s * s * (3.0 - 2.0 * s)
    tail = np.where(z > 0, np.exp(-z / max(1e-4, fall)), 0.0)   # 下沿余晖

    prof = 0.30 * core + 0.70 * fringe + 0.08 * tail
    prof = prof / (prof.max() + 1e-9) * amp

    # 沿 x 的能量起伏（扫描束不是均匀的）
    x = np.arange(W, dtype=np.float64) / max(W, 1)
    ex = 1.0 + 0.22 * (0.55 * np.sin(2 * np.pi * (0.9 * x + 0.2))
                       + 0.45 * np.sin(2 * np.pi * (3.1 * x + 0.7)))
    ex += 0.08 * rng.normal(0.0, 1.0, W)          # 一点横向噪点
    ex = np.clip(ex, 0.62, 1.18)

    return np.clip(255.0 * prof[:, None] * ex[None, :], 0, 255).astype(np.uint8)


def make_bands(W: int, H: int, amp: float, seed: int) -> np.ndarray:
    """粗亮带：周期取 H/n 以保证无缝滚动，128 为中性，供 overlay 混合。"""
    rng = np.random.default_rng(seed + 7)
    y = np.arange(H, dtype=np.float64)
    prof = np.zeros(H)
    # 参考帧实测主要带周期 161/107/72/64 px @646 高 ≈ H/4, H/6, H/9, H/10
    for n, w in ((3, 0.42), (4, 0.55), (6, 0.60), (9, 0.42), (10, 0.30), (14, 0.22)):
        prof += w * np.sin(2 * np.pi * n * y / H + rng.uniform(0, 2 * np.pi))
    prof /= (np.abs(prof).max() + 1e-9)
    # 顶部压平、过零点变陡：亮带走过去时带“硬边”
    prof = np.sign(prof) * np.abs(prof) ** 0.62
    col = np.clip(128.0 + 127.0 * amp * prof, 0, 255).astype(np.uint8)
    return np.repeat(col[:, None], W, axis=1)


def make_sweep(W: int, height: int, gain: float) -> np.ndarray:
    """扫描光束：白色 RGBA 条带，alpha 即叠加量（等价于 screen 混合）。"""
    h = max(8, int(height))
    y = np.linspace(-1.0, 1.0, h)
    core = np.exp(-(y ** 2) / (2 * 0.055 ** 2))          # 细亮线
    halo = np.exp(-(y ** 2) / (2 * 0.30 ** 2)) * 0.35    # 两侧晕开
    a = np.clip((core + halo) * gain, 0, 1) * 255.0
    arr = np.zeros((h, W, 4), np.uint8)
    arr[..., :3] = 255
    arr[..., 3] = a[:, None].astype(np.uint8)
    return arr


def _font(size: int, bold: bool = False):
    cands = ["consolab.ttf" if bold else "consola.ttf",
             "bahnschrift.ttf", "arialbd.ttf" if bold else "arial.ttf"]
    for name in cands:
        p = Path("C:/Windows/Fonts") / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                pass
    return ImageFont.load_default()


def make_hud(W: int, H: int, level: str, accent=(255, 255, 255)) -> np.ndarray:
    """卫星下传画面常见的 HUD：角标、准星、比例尺、坐标/高度/分辨率。"""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = H / 820.0
    alpha = 150 if level == "basic" else 205
    col = accent + (alpha,)
    thin = max(1, int(round(1.6 * s)))

    # 四角括号
    m, L = int(34 * s), int(64 * s)
    for (cx, cy, dx, dy) in ((m, m, 1, 1), (W - m, m, -1, 1),
                             (m, H - m, 1, -1), (W - m, H - m, -1, -1)):
        d.line([(cx, cy), (cx + dx * L, cy)], fill=col, width=thin)
        d.line([(cx, cy), (cx, cy + dy * L)], fill=col, width=thin)

    # 中心准星
    cx, cy = W // 2, H // 2
    g, gap = int(46 * s), int(16 * s)
    for (x0, y0, x1, y1) in ((cx - g, cy, cx - gap, cy), (cx + gap, cy, cx + g, cy),
                             (cx, cy - g, cx, cy - gap), (cx, cy + gap, cx, cy + g)):
        d.line([(x0, y0), (x1, y1)], fill=col, width=thin)
    r = int(11 * s)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=thin)

    font_big = _font(int(23 * s), bold=True)
    font = _font(int(19 * s))
    font_sm = _font(int(15 * s))

    def text(xy, s_, f, fill=col):
        if f is not None:
            d.text(xy, s_, font=f, fill=fill)

    text((m + int(14 * s), m + int(10 * s)), "SAT-3 / OPTICAL SCAN", font_big)
    text((m + int(14 * s), m + int(42 * s)), "PASS 0417   NADIR", font_sm)

    rx = W - m - int(300 * s)
    text((rx, m + int(10 * s)), "LAT  39 54 26 N", font)
    text((rx, m + int(36 * s)), "LON 116 23 29 E", font)
    text((rx, m + int(62 * s)), "ALT 000512 KM", font)
    text((rx, m + int(88 * s)), "GSD 000.50 M", font)

    if level == "full":
        text((rx, m + int(130 * s)), "CH-1  VIS 0.45-0.90", font_sm)
        text((rx, m + int(152 * s)), "GAIN  +12.0 DB", font_sm)
        text((rx, m + int(174 * s)), "SCAN  1.00 HZ", font_sm)
        text((m + int(14 * s), m + int(66 * s)), "LINK 0x2A7F  LOCKED", font_sm)

    # 底部比例尺
    by = H - m - int(26 * s)
    x0 = m + int(14 * s)
    seg = int(74 * s)
    for i in range(4):
        d.line([(x0 + i * seg, by), (x0 + i * seg, by - int(11 * s))], fill=col, width=thin)
        d.line([(x0 + i * seg, by), (x0 + (i + 1) * seg, by)], fill=col, width=thin)
        if i % 2 == 0:
            text((x0 + i * seg + int(3 * s), by - int(30 * s)), "%d M" % (i * 50), font_sm)

    text((W - m - int(300 * s), by - int(30 * s)), "FRAME 0000  SYNC OK", font_sm)
    return np.asarray(img)


# ----------------------------------------------------------------------------
# filtergraph
# ----------------------------------------------------------------------------
def _num(v: float) -> str:
    return ("%.4f" % float(v)).rstrip("0").rstrip(".")


class Renderer:
    """把 Look 翻译成一份 ffmpeg filter_complex。"""

    def __init__(self, look: Look, texdir: Path, fps: float):
        self.L = look
        self.dir = texdir
        self.fps = fps
        self.inputs: list[str] = []      # 纹理输入文件（序号 1..N，0 是视频）

    def add_texture(self, arr: np.ndarray, name: str) -> int:
        mode = "RGBA" if arr.ndim == 3 and arr.shape[-1] == 4 else "L"
        path = self.dir / f"{name}.png"
        Image.fromarray(arr, mode).save(path)
        self.inputs.append(str(path))
        return len(self.inputs)          # 输入 0 是视频，纹理从 1 开始

    # -- 调色曲线 -------------------------------------------------------------
    def curves(self) -> str:
        L = self.L
        lift, white = L.lift, L.white
        keys = [(0.0, lift),
                (0.18, lift + 0.20 * (0.92 - lift)),
                (0.50, lift + 0.50 * (0.86 - lift)),
                (0.82, 0.80),
                (1.0, white)]
        master = " ".join(f"{x:.3f}/{y:.3f}" for x, y in keys)
        out = [f"curves=m='{master}'"]
        mid = lift + 0.5 * (0.86 - lift)
        for i, ch in enumerate("rgb"):
            t, ts = L.tint[i], L.tint_shadow[i]
            ck = [(0.0, min(1.0, max(0.0, lift + ts))),
                  (0.5, min(1.0, max(0.0, mid + t))),
                  (1.0, min(1.0, max(0.0, white + t * 0.5)))]
            out.append("curves=%s='%s'" % (
                ch, " ".join(f"{x:.3f}/{y:.3f}" for x, y in ck)))
        return ",".join(out)

    # -- 整条链 ---------------------------------------------------------------
    def graph(self, hud_idx: int, sweep_idx: int, bands_idx: int,
              raster_idx: int, soft_idx: int, bars_idx: int = -1) -> str:
        L = self.L
        W, H = L.width, L.height
        stmts: list[str] = []
        lvl = 0

        def nxt() -> str:
            nonlocal lvl
            lvl += 1
            return f"[g{lvl}]"

        # ---- 1. 规整尺寸 -> 调色 -> 颗粒 -> 横向拖影 ----------------------
        head: list[str] = []
        if self.fps > 0:
            head.append("fps=" + _num(round(self.fps, 6)))
        if L.fit == "stretch":
            head.append(f"scale={W}:{H}:flags=lanczos")
        elif L.fit == "cover":
            head.append(f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos")
            head.append(f"crop={W}:{H}")
        else:  # pad
            head.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos")
            head.append(f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black")
        head += ["setsar=1",
                 "format=yuv444p" if L.bar_luma_only else "format=gbrp"]
        # 调色只在真的动了参数时才挂上去：全中性就一点色彩处理都不做
        neutral = (abs(L.contrast - 1.0) < 1e-6 and abs(L.gamma - 1.0) < 1e-6
                   and abs(L.saturation - 1.0) < 1e-6 and abs(L.lift) < 1e-6
                   and abs(L.white - 1.0) < 1e-6
                   and not any(abs(v) > 1e-6 for v in L.tint)
                   and not any(abs(v) > 1e-6 for v in L.tint_shadow))
        if L.extra:
            head += [
                f"eq=contrast={_num(round(L.contrast, 4))}"
                f":gamma={_num(round(L.gamma, 4))}"
                f":saturation={_num(round(L.saturation, 4))}",
                L.extra, "format=gbrp"]
        elif not neutral:
            head += [f"eq=contrast={_num(round(L.contrast, 4))}"
                     f":gamma={_num(round(L.gamma, 4))}"
                     f":saturation={_num(round(L.saturation, 4))}",
                     self.curves()]
        # 颗粒必须在横向拖影之前：线扫传感器沿 x 积分，颗粒也被拖成横丝
        if L.grain > 0:
            head.append(f"noise=alls={int(L.grain)}:allf=t+u")
        if L.hblur > 0:
            nb = max(1, int(round(L.hblur * W / 1920.0)))
            head.append(f"avgblur=sizeX={nb}:sizeY=0")
        stmts.append("[0:v]" + ",".join(head) + "[g0]")
        cur = "[g0]"

        # ---- 2~4. 纹理层（luma_only 时只碰 Y 平面，色度一个像素都不改） ----
        luma_only = bool(L.bar_luma_only)
        pix = "yuv444p" if luma_only else "gbrp"

        def tex_blend(idx: int, tag: str, mode: str) -> None:
            nonlocal cur
            stmts.append(f"[{idx}:v]format={pix}[{tag}]")
            if luma_only:
                spec = (f"c0_mode={mode}:c1_mode=none:c2_mode=none")
            else:
                spec = f"all_mode={mode}"
            stmts.append(f"{cur}[{tag}]blend={spec}:shortest=0{nxt()}")
            cur = f"[g{lvl}]"

        # ---- 2. 光栅（overlay：中性 128，暗缝压暗、亮脊提亮） -------------
        if raster_idx > 0:
            tex_blend(raster_idx, "rst", "overlay")

        # ---- 3. 扫描线柔光 -------------------------------------------------
        if soft_idx > 0:
            tex_blend(soft_idx, "sft", "screen")

        # ---- 4. 白条自上而下扫描（screen：叠上去就是白光，压掉底下的细节） --
        if bars_idx > 0:
            # bar_speed 按"输出时间"定义：变速之后白条的走速保持不变，
            # 所以这里要先除回 speed（setpts 会把时间轴压快 speed 倍）
            rate = abs(L.bar_speed) / max(float(L.speed or 1.0), 1e-6)
            spd = -rate if L.bar_speed else 0.0                 # 负号 = 向下
            pre = f",scroll=vertical={_num(round(spd, 6))}:horizontal=0"
            stmts.append(f"[{bars_idx}:v]format={pix}{pre}[brs]")
            if luma_only:
                spec = "c0_mode=screen:c1_mode=none:c2_mode=none"
            else:
                spec = "all_mode=screen"
            stmts.append(f"{cur}[brs]blend={spec}:shortest=0{nxt()}")
            cur = f"[g{lvl}]"

        # ---- 5. 走带亮带（可滚动） -----------------------------------------
        if bands_idx > 0:
            pre = ""
            if L.band_scroll:
                pre = f",scroll=vertical={_num(round(L.band_scroll, 5))}:horizontal=0"
            stmts.append(f"[{bands_idx}:v]format=gbrp{pre}[bd]")
            stmts.append(f"{cur}[bd]blend=all_mode=overlay:shortest=0{nxt()}")
            cur = f"[g{lvl}]"

        # ---- 5. 扫描光束（白色 + alpha 叠加 == screen） -------------------
        if sweep_idx > 0:
            sh = int(round(H * 0.30))
            span = H + sh
            stmts.append(f"[{sweep_idx}:v]format=rgba[swp]")
            stmts.append(
                f"{cur}[swp]overlay=x=0:y='mod(t*{_num(round(L.sweep_speed, 4))}*{span}"
                f"\\,{span})-{sh}':shortest=0{nxt()}")
            cur = f"[g{lvl}]"

        # ---- 6. 四角压暗 / 色差 --------------------------------------------
        tail = ["format=yuv444p"]
        if L.vignette > 0:
            ang = 0.10 + 0.62 * min(1.0, L.vignette)
            tail.append(f"vignette=angle={ang:.4f}:mode=forward")
        if L.chroma_split > 0:
            c = int(round(L.chroma_split))
            tail.append(f"rgbashift=rh={c}:bh=-{c}:edge=wrap")
        stmts.append(cur + ",".join(tail) + nxt())
        cur = f"[g{lvl}]"

        # ---- 7. HUD ---------------------------------------------------------
        if hud_idx > 0:
            stmts.append(f"[{hud_idx}:v]format=rgba[hud]")
            stmts.append(f"{cur}[hud]overlay=x=0:y=0:shortest=0{nxt()}")
            cur = f"[g{lvl}]"

        # ---- 8. 变速 ---------------------------------------------------------
        # 放在最后：先把 1× 的画面做完，再整体压缩时间轴 -> 所有效果一起变快
        end = ""
        if L.speed and abs(L.speed - 1.0) > 1e-6:
            end = f",setpts=PTS/{_num(round(float(L.speed), 6))}"
        stmts.append(f"{cur}format=yuv420p{end}[v]")
        return ";".join(stmts)


# ----------------------------------------------------------------------------
# ffmpeg 调用
# ----------------------------------------------------------------------------
def probe(path: str) -> dict:
    exe = shutil.which("ffprobe") or "ffprobe"
    out = subprocess.run(
        [exe, "-v", "error", "-select_streams", "v:0",
         "-show_entries",
         "stream=width,height,sample_aspect_ratio,display_aspect_ratio,r_frame_rate"
         ":format=duration",
         "-of", "json", path], capture_output=True, text=True)
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _texture_inputs(R: Renderer) -> list[str]:
    args: list[str] = []
    for path in R.inputs:
        # 静态图无限循环；靠输出端的 -t 收尾
        args += ["-loop", "1", "-framerate", _num(round(max(R.fps, 1.0), 6)),
                 "-i", path]
    return args


def build_command(src: str, dst: str, L: Look, R: Renderer, seq: tuple,
                  start: float | None, duration: float, crf: int,
                  preset: str, audio: str) -> list[str]:
    exe = shutil.which("ffmpeg") or "ffmpeg"
    cmd: list[str] = [exe, "-hide_banner", "-loglevel", "error", "-stats", "-y"]
    if start:
        cmd += ["-ss", _num(round(start, 6))]
    cmd += ["-i", src]
    cmd += _texture_inputs(R)

    raster_idx, soft_idx, bands_idx, sweep_idx, hud_idx = seq[:5]
    bars_idx = seq[5] if len(seq) > 5 else -1
    cmd += ["-filter_complex", R.graph(hud_idx, sweep_idx, bands_idx,
                                       raster_idx, soft_idx, bars_idx)]
    cmd += ["-map", "[v]"]
    # 变速：音频用 atempo（>2 时拆成多级，兼容老版本）
    spd = float(L.speed) if L.speed else 1.0
    afilter = ""
    if abs(spd - 1.0) > 1e-6:
        chain, rest = [], spd
        while rest > 2.0:
            chain.append("atempo=2.0")
            rest /= 2.0
        while rest < 0.5:
            chain.append("atempo=0.5")
            rest /= 0.5
        chain.append("atempo=%s" % _num(round(rest, 6)))
        afilter = ",".join(chain)
    # 注意：这条 LD/MKV 的 FLAC 音轨用 -c:a copy 会带出错误时长（6s 变 12.6s），
    # 所以默认重编码。flac 无损、aac 通用。
    acodec = audio
    if acodec == "auto":
        acodec = "aac" if dst.lower().endswith((".mp4", ".m4v")) else "flac"
    if acodec == "copy" and afilter:
        acodec = "aac" if dst.lower().endswith((".mp4", ".m4v")) else "flac"
    if acodec == "copy":
        cmd += ["-map", "0:a:0?", "-c:a", "copy"]
    elif acodec == "flac":
        cmd += ["-map", "0:a:0?", "-c:a", "flac", "-compression_level", "8"]
    elif acodec == "aac":
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"]
    if afilter:
        cmd += ["-af", afilter]
    if duration and duration > 0:
        # 加了 setpts 之后输出时间轴被压过，-t 也跟着除
        cmd += ["-t", _num(round(duration / spd, 6))]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-x264-params", "keyint=120:min-keyint=60"]
    if dst.lower().endswith(".mp4"):
        cmd += ["-f", "mp4"]
    if L.fps > 0:
        cmd += ["-r", _num(round(L.fps, 6))]
    cmd += [dst]
    return cmd


def render_still(src: str, dst: str, R: Renderer, seq: tuple, at: float) -> None:
    exe = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", _num(round(at, 6)), "-i", src]
    cmd += _texture_inputs(R)
    raster_idx, soft_idx, bands_idx, sweep_idx, hud_idx = seq[:5]
    bars_idx = seq[5] if len(seq) > 5 else -1
    cmd += ["-filter_complex",
            R.graph(hud_idx, sweep_idx, bands_idx, raster_idx, soft_idx, bars_idx),
            "-map", "[v]", "-frames:v", "1", dst]
    _run(cmd)


def _run(cmd: list[str]) -> None:
    if os.environ.get("SATSHOW"):
        print(" ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, bufsize=1, errors="replace")
    tail: list[str] = []
    assert proc.stderr is not None
    for line in proc.stderr:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-40:]
        if "frame=" in line or "rror" in line:
            sys.stderr.write("\r" + line[-150:].ljust(150))
            sys.stderr.flush()
    rc = proc.wait()
    sys.stderr.write("\r" + " " * 150 + "\r")
    if rc != 0:
        sys.stderr.write("\n".join(tail) + "\n")
        raise SystemExit(f"ffmpeg 失败（退出码 {rc}）")


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="satellite_scan.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="把普通视频转成『人造卫星光学扫描』风格的视频。",
        epilog="例: python satellite_scan.py 首都.mkv -o out.mkv "
               "--start 271.5 --duration 12 --hud full")
    ap.add_argument("--version", action="version",
                    version=f"satellite_scan {__version__}")
    ap.add_argument("input", help="输入视频")
    ap.add_argument("-o", "--output", required=True,
                    help="输出文件（.mp4/.mkv；--still 时写 .png）")

    g = ap.add_argument_group("画面")
    g.add_argument("--width", type=int, default=1920)
    g.add_argument("--height", type=int, default=820)
    g.add_argument("--fit", choices=["stretch", "cover", "pad"], default="stretch",
                   help="stretch=横向拉成宽画幅（参考帧就是 4:3 拉宽的，默认）；"
                        "cover=保持比例裁切填满；pad=加黑边")
    g.add_argument("--aspect", default=None, help="用比例代替 --height，如 2.34:1")
    g.add_argument("--look", choices=sorted(LOOKS), default=DEFAULT_LOOK,
                   help="satellite=对着 MV 参考帧的紫灰版（默认）；refined=保留原片色彩；"
                        "natural=完全不改颜色")
    g.add_argument("--intensity", type=float, default=1.0, help="整体强度，0=原片")
    g.add_argument("--pitch", type=float, default=None, help="扫描线间距（占画面高的比例）")
    g.add_argument("--line-depth", type=float, default=None, help="扫描线明暗幅度")
    g.add_argument("--hblur", type=int, default=None,
                   help="行内横向拖影半径（1920 宽下的像素）")
    g.add_argument("--grain", type=int, default=None, help="颗粒强度 0..100")
    g.add_argument("--bar-amp", type=float, default=None,
                   help="白条强度 0..1（1 = 压成死白）")
    g.add_argument("--bar-speed", type=float, default=None,
                   help="白条下移速度（画面高/秒）")
    g.add_argument("--bar-period", type=float, default=None,
                   help="一屏里有几根白条（整数）")
    g.add_argument("--bar-core", type=float, default=None,
                   help="白条本体（细亮线）半宽，占画面高的比例")
    g.add_argument("--bar-fringe", type=float, default=None,
                   help="白条外那圈淡色边框的半宽，占画面高的比例（越大边框越粗）")
    g.add_argument("--band-amp", type=float, default=None, help="走带亮带强度 0..0.9")
    g.add_argument("--band-scroll", type=float, default=None,
                   help="亮带上滚速度（画面高/秒）")
    g.add_argument("--sweep", type=float, default=None,
                   help="扫描光束速度（画面高/秒），0=关")
    g.add_argument("--sweep-gain", type=float, default=None)
    g.add_argument("--vignette", type=float, default=None, help="四角压暗 0..1")
    g.add_argument("--noise-seed", type=int, default=None)
    g.add_argument("--hud", choices=["none", "basic", "full"], default="none")

    t = ap.add_argument_group("时间")
    t.add_argument("--start", type=float, default=None, help="起始秒")
    t.add_argument("--duration", type=float, default=None,
                   help="时长秒（按原片时间算，变速后输出会自动缩短）")
    t.add_argument("--speed", type=float, default=None,
                   help="输出倍速，默认 3（1 = 原速，0.5 = 慢放）")
    t.add_argument("--still", action="store_true", help="只输出 --start 处的一帧 PNG")
    t.add_argument("--fps", type=float, default=0.0, help="输出帧率，0=跟源")

    e = ap.add_argument_group("编码")
    e.add_argument("--crf", type=int, default=19)
    e.add_argument("--preset", default="veryfast", help="x264 preset: ultrafast…slower")
    e.add_argument("--audio", choices=["auto", "flac", "aac", "copy", "none"],
                   default="auto",
                   help="auto=mp4 用 aac / 其他用 flac（无损）")
    e.add_argument("--tex-dir", default=None, help="纹理输出目录（默认输出文件旁）")
    e.add_argument("--keep-textures", action="store_true", help="保留纹理 PNG 便于调试")
    e.add_argument("--dry-run", action="store_true", help="只打印 ffmpeg 命令")
    e.add_argument("--set", action="append", default=[], metavar="KEY=VAL",
                   help="直接覆盖任意参数，可重复，如 --set line_wobble=1.2")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)

    if not Path(a.input).exists():
        raise SystemExit(f"找不到输入文件：{a.input}")

    # ---- 参数装配 ----------------------------------------------------------
    # 顺序很重要：预设 -> 命令行覆盖 -> intensity -> --set
    look = Look(look=a.look)
    for k, v in LOOKS[a.look].items():
        setattr(look, k, v)

    look.width, look.height = a.width, a.height
    if a.aspect:
        aw, ah = (a.aspect.split(":", 1) if ":" in a.aspect
                  else (a.aspect, "1"))
        look.height = int(round(look.width / (float(aw) / float(ah)) / 2) * 2)
    look.fit = a.fit
    look.hud = a.hud
    look.fps = a.fps

    for attr, val in (("pitch", a.pitch), ("line_depth", a.line_depth),
                      ("hblur", a.hblur), ("grain", a.grain),
                      ("bar_amp", a.bar_amp), ("bar_speed", a.bar_speed),
                      ("bar_period", a.bar_period), ("bar_core", a.bar_core),
                      ("bar_fringe", a.bar_fringe),
                      ("band_amp", a.band_amp), ("band_scroll", a.band_scroll),
                      ("sweep_speed", a.sweep), ("sweep_gain", a.sweep_gain),
                      ("vignette", a.vignette), ("seed", a.noise_seed),
                      ("speed", a.speed)):
        if val is not None:
            setattr(look, attr, val)

    if a.intensity != 1.0:
        look = look.scaled(a.intensity)

    for item in a.set:
        if "=" not in item:
            raise SystemExit(f"--set 需要 KEY=VAL 形式：{item}")
        key, val = (s.strip() for s in item.split("=", 1))
        if not hasattr(look, key):
            raise SystemExit(f"未知参数：{key}（可用："
                             f"{', '.join(f.name for f in fields(Look))}）")
        cur = getattr(look, key)
        if isinstance(cur, bool):
            setattr(look, key, val.lower() in ("1", "true", "yes", "on"))
        elif isinstance(cur, int):
            setattr(look, key, int(float(val)))
        elif isinstance(cur, float):
            setattr(look, key, float(val))
        elif isinstance(cur, tuple):
            setattr(look, key, tuple(float(x) for x in val.strip("()").split(",")))
        else:
            setattr(look, key, val)

    look.width -= look.width % 2
    look.height -= look.height % 2

    # ---- 源信息 ------------------------------------------------------------
    info = probe(a.input)
    fps = look.fps
    if fps <= 0:
        try:
            num, den = info["streams"][0]["r_frame_rate"].split("/")
            fps = float(num) / float(den)
        except Exception:
            fps = 25.0
    look.fps = round(fps, 6)

    total = 0.0
    try:
        total = float(info.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        total = 0.0
    dur = a.duration
    if total > 0:
        remain = max(0.0, total - (a.start or 0.0))
        dur = remain if dur is None else min(dur, remain)
    if dur is None:
        dur = 0.0

    # ---- 纹理 --------------------------------------------------------------
    if a.tex_dir:
        tex_dir = Path(a.tex_dir)
    else:
        tex_dir = Path(a.output).resolve().parent / f".sattex_{os.getpid()}"
    tex_dir.mkdir(parents=True, exist_ok=True)
    if not a.keep_textures and not a.tex_dir:
        # 只有自动创建的临时目录才清理；显式给了 --tex-dir 就留着
        import atexit
        atexit.register(lambda: shutil.rmtree(tex_dir, ignore_errors=True))

    W, H = look.width, look.height
    pitch = max(2.0, look.pitch * H)
    raster, soft = make_raster(W, H, pitch, look.line_depth, look.line_wobble,
                               look.bloom, look.seed)

    R = Renderer(look, tex_dir, look.fps)
    seq_raster = R.add_texture(raster, "raster")
    seq_soft = R.add_texture(soft, "bloom") if look.bloom > 0 else -1
    seq_bands = (R.add_texture(make_bands(W, H, look.band_amp, look.seed), "bands")
                 if look.band_amp > 0 else -1)
    seq_sweep = (R.add_texture(make_sweep(W, int(H * 0.30), look.sweep_gain), "sweep")
                 if (look.sweep_gain > 0 and look.sweep_speed > 0) else -1)
    seq_hud = (R.add_texture(make_hud(W, H, look.hud), "hud")
               if look.hud != "none" else -1)
    seq_bars = (R.add_texture(
        make_bars(W, H, look.bar_period, look.bar_core, look.bar_fringe,
                  look.bar_fall, look.bar_amp, look.seed), "bars")
        if look.bar_amp > 0 else -1)
    seq = (seq_raster, seq_soft, seq_bands, seq_sweep, seq_hud, seq_bars)

    # ---- 出片 --------------------------------------------------------------
    if a.still:
        render_still(a.input, a.output, R, seq, a.start or 0.0)
        print(f"已输出单帧 → {a.output}")
        return 0

    cmd = build_command(a.input, a.output, look, R, seq, a.start, dur,
                        a.crf, a.preset, a.audio)
    if a.dry_run:
        print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        return 0

    print(f"输入 {a.input}")
    print(f"输出 {a.output}  {W}x{H}  {look.fps:.3f}fps  look={a.look}  "
          f"fit={look.fit}  原片 {dur:.1f}s -> 输出 {dur / max(look.speed, 1e-6):.1f}s"
          f"（{look.speed:g}× 速）")
    print(f"扫描线 {pitch:.2f}px  横向拖影 {look.hblur}px  颗粒 {look.grain}  "
          f"白条 {look.bar_period:g} 根 × {look.bar_amp:.2f}  "
          f"下移 {look.bar_speed:.2f} 画面高/秒")
    _run(cmd)
    size = Path(a.output).stat().st_size / 1e6
    print(f"完成 → {a.output}  ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
