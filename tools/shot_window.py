# -*- coding: utf-8 -*-
"""抓取包含 Windows 原生标题栏的整窗截图

为什么单独写：Qt 的 widget.grab() 只截客户区，看不到标题栏，
而标题栏恰恰是要验证的对象。这里用 PrintWindow 把整窗（含边框/标题栏）
画到位图上，再裁出来看。
"""
import ctypes
import os
import sys
from ctypes import wintypes

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def find_window(title_part="ZeroFall"):
    result = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_part in buf.value:
            result.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return result


def capture(hwnd, out_path):
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        print("  窗口尺寸无效")
        return None

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)

    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0

    buf_len = w * h * 4
    buf = ctypes.create_string_buffer(buf_len)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)

    im = Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1)
    im = im.convert('RGB')
    im.save(out_path)

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    return im, ok


def main():
    wins = find_window("ZeroFall")
    if not wins:
        print("  没找到 ZeroFall 窗口（程序没启动？）")
        return 1
    out_dir = os.path.join(os.environ.get('TEMP', '.'), 'zf_ui')
    os.makedirs(out_dir, exist_ok=True)
    for hwnd, title in wins:
        print("  窗口: {!r} hwnd={}".format(title, hwnd))
        res = capture(hwnd, os.path.join(out_dir, 'full_window.png'))
        if res:
            im, ok = res
            print("  已抓取 {}x{} PrintWindow={}".format(
                im.width, im.height, ok))
            # 单独裁出标题栏（顶部 40px）
            bar = im.crop((0, 0, im.width, min(40, im.height)))
            bar = bar.resize((bar.width, bar.height * 3), Image.NEAREST)
            p2 = os.path.join(out_dir, 'titlebar.png')
            bar.save(p2)
            print("  标题栏裁图:", p2)
            # 采样标题栏颜色
            r, g, b = im.getpixel((im.width // 2, 12))
            print("  标题栏取样 rgb({},{},{})  #{:02x}{:02x}{:02x}".format(
                r, g, b, r, g, b))
            dark = (r + g + b) / 3 < 90
            print("  标题栏是否为深色:", "是" if dark else "否（仍是浅色）")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
