# -*- coding: utf-8 -*-
"""生成应用图标（科幻极简风，不含任何文字）

设计思路：
  参考主流桌面应用的图标语言 —— 一块圆角方底的"深空"背景，中间
  只放一个可辨识的几何符号，不放字母、不放小字。理由很实际：
  ico 里有 16x16 的尺寸，任何文字到那个尺寸都是一团糊。

  符号语义（ZeroFall = DeepSeek 的前置守护）：
    环形   → 守护/监控的边界（X 度缺口让它像 HUD 扫描环，不死板）
    中心点 → 被守护的对象
    底线   → 兜底基线，"掉下来接得住"

绘制方式：8 倍超采样后缩回目标尺寸，边缘才够干净；
渐变用逐行/逐角度插值手工算，避免出现色带。
"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 8                     # 超采样倍数
BASE = 256                # 目标最大尺寸
W = BASE * S

# 配色（与界面主题同源）
DEEP_TOP = (14, 20, 32)       # #0e1420
DEEP_BOTTOM = (10, 16, 28)    # #0a101c
ACCENT = (34, 211, 238)       # #22d3ee 青
ACCENT2 = (59, 130, 246)      # #3b82f6 蓝
GLOW = (168, 85, 247)         # #a855f7 紫（少量点缀）


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def vertical_gradient(size, top, bottom):
    img = Image.new('RGB', (1, size))
    px = img.load()
    for y in range(size):
        px[0, y] = lerp(top, bottom, y / max(1, size - 1))
    return img.resize((size, size), Image.BILINEAR)


def rounded_mask(size, radius_ratio=0.225):
    mask = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(mask)
    r = int(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    return mask


def radial_glow(size, color, strength=90):
    """中心的柔和辉光，让底色不显得死板"""
    g = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(g)
    cx = cy = size // 2
    steps = 46
    for i in range(steps, 0, -1):
        t = i / steps
        r = int(size * 0.46 * t)
        a = int(strength * (1 - t) ** 2)
        if r > 0 and a > 0:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=a)
    g = g.filter(ImageFilter.GaussianBlur(size * 0.05))
    layer = Image.new('RGB', (size, size), color)
    return layer, g


def draw_arc_gradient(img, cx, cy, radius, width, start_deg, end_deg,
                      c_from, c_to, steps=240):
    """按角度插值画渐变圆弧（PIL 没有原生渐变描边）"""
    d = ImageDraw.Draw(img)
    span = end_deg - start_deg
    for i in range(steps):
        t0 = i / steps
        t1 = (i + 1) / steps
        a0 = start_deg + span * t0
        a1 = start_deg + span * t1
        col = lerp(c_from, c_to, t0)
        box = [cx - radius, cy - radius, cx + radius, cy + radius]
        d.arc(box, a0, a1, fill=col + (255,), width=width)


def build():
    img = Image.new('RGBA', (W, W), (0, 0, 0, 0))

    # ── 底：圆角方 + 竖向渐变 ──
    grad = vertical_gradient(W, DEEP_TOP, DEEP_BOTTOM).convert('RGBA')
    mask = rounded_mask(W)
    img.paste(grad, (0, 0), mask)

    # ── 中心辉光（青，很淡）──
    glow_layer, glow_mask = radial_glow(W, ACCENT, strength=64)
    img.paste(glow_layer, (0, 0), Image.composite(
        glow_mask, Image.new('L', (W, W), 0), mask))

    cx = cy = W // 2

    # ── 主环：从青到蓝，右上留缺口（HUD 感）──
    outer_r = int(W * 0.315)
    ring_w = int(W * 0.058)
    # 外环：缺口约 55 度，位置右上，像 HUD 扫描环
    draw_arc_gradient(img, cx, cy, outer_r, ring_w,
                      start_deg=22, end_deg=22 + 305,
                      c_from=ACCENT, c_to=ACCENT2)

    # 内弧：与外环错开半圈，做出"轨道"层次（纯线条式科技感的关键）
    inner_r = int(W * 0.176)
    inner_w = int(W * 0.026)
    draw_arc_gradient(img, cx, cy, inner_r, inner_w,
                      start_deg=200, end_deg=200 + 268,
                      c_from=ACCENT2, c_to=ACCENT)

    # ── 缺口两端的短亮头（像扫描环的端点）──
    d = ImageDraw.Draw(img)
    for ang, col in ((22, ACCENT), (22 + 305, ACCENT2)):
        rad = math.radians(ang)
        x1 = cx + math.cos(rad) * (outer_r - ring_w * 0.5)
        y1 = cy + math.sin(rad) * (outer_r - ring_w * 0.5)
        rr = ring_w * 0.52
        d.ellipse([x1 - rr, y1 - rr, x1 + rr, y1 + rr], fill=col + (255,))

    # ── 中心点：被守护的对象 ──
    dot_r = int(W * 0.062)
    # 外圈淡光
    halo = Image.new('RGBA', (W, W), (0, 0, 0, 0))
    dh = ImageDraw.Draw(halo)
    hr = int(dot_r * 2.0)
    dh.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=ACCENT + (44,))
    halo = halo.filter(ImageFilter.GaussianBlur(W * 0.022))
    img.alpha_composite(halo)
    # 核心：外青内亮的渐变球，比纯白圆点更有"能量核心"的感觉
    d = ImageDraw.Draw(img)
    for i in range(24, 0, -1):
        t = i / 24.0
        rr = int(dot_r * t)
        col = lerp((236, 253, 255), ACCENT, t ** 1.6)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=col + (255,))


    # ── 内描边：1px 淡青，让边界更有科技感 ──
    edge = Image.new('RGBA', (W, W), (0, 0, 0, 0))
    de = ImageDraw.Draw(edge)
    inset = max(2, int(W * 0.012))
    de.rounded_rectangle(
        [inset, inset, W - inset - 1, W - inset - 1],
        radius=int(W * 0.225) - inset,
        outline=ACCENT + (70,), width=max(2, int(W * 0.006)))
    img.alpha_composite(edge)

    return img


def build_simple():
    """小尺寸专用简化版

    16/24px 上细节必然糊掉 —— 内弧、缺口端点、辉光在这个尺度都是噪点。
    所以小图标不复用大图缩略，而是重画：更粗的单环 + 更大的核心 + 更大的
    缺口，保证一眼能认出"环 + 点"这个骨架。
    """
    size = W
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    grad = vertical_gradient(size, DEEP_TOP, DEEP_BOTTOM).convert('RGBA')
    img.paste(grad, (0, 0), rounded_mask(size))

    cx = cy = size // 2
    d = ImageDraw.Draw(img)

    # 单环，明显更粗
    outer_r = int(size * 0.325)
    ring_w = int(size * 0.105)
    draw_arc_gradient(img, cx, cy, outer_r, ring_w,
                      start_deg=30, end_deg=30 + 275,
                      c_from=ACCENT, c_to=ACCENT2, steps=160)

    # 核心：实心亮圆，比例放大
    dot_r = int(size * 0.115)
    d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
              fill=(228, 250, 255, 255))
    d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
              outline=ACCENT + (255,), width=int(size * 0.026))

    # 外描边：把方块边界勾出来。
    # 深色底 #0c121e 在深色任务栏/托盘上会和背景融成一片，16px 时看着
    # 就像"只剩一个青色 C"，和桌面上那块醒目的深色圆角方块完全不像。
    # 描边用亮青（不透明度给足），深色和浅色背景上都能看清轮廓。
    inset = max(2, int(size * 0.020))
    d.rounded_rectangle(
        [inset, inset, size - inset - 1, size - inset - 1],
        radius=int(size * 0.225) - inset,
        outline=ACCENT + (170,),
        width=max(2, int(size * 0.016)))
    return img


def main(style='simple'):
    """生成全部图标资源

    style='simple'   —— 单环 + 中心点（托盘上看到的那个骨架），所有尺寸统一用它
    style='detailed' —— 双弧螺旋 + 缺口端点 + 辉光（早期大尺寸版本）

    2026-11-11 起默认 'simple'：原来 ico 里 ≤32px 用简化版、≥48px 用细节版，
    结果托盘是单环、桌面/任务栏是双弧，同一个人两副脸。统一成单环后，
    托盘/任务栏/桌面快捷方式/资源管理器/窗口标题栏看到的是同一个符号。
    """
    renderer = build_simple if style == 'simple' else build
    big = renderer()
    out_dir = os.path.dirname(os.path.abspath(__file__))
    print('  风格:', style)

    # 预览 PNG（256）
    preview = big.resize((BASE, BASE), Image.LANCZOS)
    preview.save(os.path.join(out_dir, 'icon_preview.png'))
    print('  预览:', os.path.join(out_dir, 'icon_preview.png'))

    # icon.png（256）：打包脚本与任何引用它的地方也必须是同一个符号
    preview.save(os.path.join(out_dir, 'icon.png'))
    print('  主图:', os.path.join(out_dir, 'icon.png'))

    # 多尺寸 ico：每一帧都由同一渲染器缩出，避免"小尺寸另一套设计"
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    frames = [big.resize(s, Image.LANCZOS) for s in sizes]
    ico_path = os.path.join(out_dir, 'ZeroFall.ico')
    frames[0].save(ico_path, format='ICO', sizes=sizes,
                   append_images=frames[1:])
    print('  ico:', ico_path, os.path.getsize(ico_path), 'bytes')

    # 各尺寸单独出图，便于核对小图标是否糊
    for s in (16, 32, 48, 64):
        big.resize((s, s), Image.LANCZOS).save(
            os.path.join(out_dir, 'icon_{}.png'.format(s)))
    print('  小尺寸样张已生成: icon_16/32/48/64.png')


if __name__ == '__main__':
    style = 'simple'
    for i, a in enumerate(sys.argv[1:]):
        if a in ('--style',) and i + 2 <= len(sys.argv) - 1:
            style = sys.argv[i + 2]
        elif a.startswith('--style='):
            style = a.split('=', 1)[1]
        elif a in ('detailed', 'simple'):
            style = a
    if style not in ('simple', 'detailed'):
        print('用法: python make_icon.py [--style simple|detailed]')
        raise SystemExit(2)
    main(style)
