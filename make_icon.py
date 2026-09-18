# -*- coding: utf-8 -*-
"""
图标生成脚本: 蓝色圆角方块 + 白色对勾
- 重绘 1024x1024 高清源图(解决 macOS 大尺寸图标发糊/马赛克)
- 生成完整 app_icon.icns(16/32/64/128/256/512/1024 全部尺寸)
- 更新 app_icon.png(1024 源图) 与 app_icon.ico(多尺寸)
依赖: Pillow (pip install pillow)
用法: python make_icon.py
"""
import io
import os
import struct

from PIL import Image, ImageDraw

# ---------- 设计参数(与原图标一致) ----------
BLUE = (59, 130, 246, 255)          # #3B82F6 Tailwind blue-500
WHITE = (255, 255, 255, 255)
SIZE = 1024                          # 源图尺寸(原生高清)
RADIUS = int(SIZE * 0.225)           # 圆角半径 ≈22.5%(macOS 图标风格)
CHECK_WIDTH = int(SIZE * 0.13)       # 对勾线宽
CHECK_POINTS = [                     # 对勾三点(相对坐标 x1,y1, x2,y2, x3,y3)
    (int(SIZE * 0.325), int(SIZE * 0.535)),
    (int(SIZE * 0.450), int(SIZE * 0.660)),
    (int(SIZE * 0.700), int(SIZE * 0.400)),
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def draw_icon(size: int) -> Image.Image:
    """按设计参数绘制指定尺寸的图标(抗锯齿缩放自 1024 源)"""
    if size == SIZE:
        im = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=RADIUS, fill=BLUE)
        d.line(CHECK_POINTS, fill=WHITE, width=CHECK_WIDTH, joint="curve")
        return im
    return draw_icon(SIZE).resize((size, size), Image.LANCZOS)


def png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def build_icns(im1024: Image.Image) -> bytes:
    """生成完整 icns: header + 各尺寸 PNG 块"""
    blocks = [
        (16, "icp4"), (32, "icp5"), (64, "icp6"), (128, "ic07"),
        (256, "ic08"), (512, "ic09"), (1024, "ic10"),
        (32, "ic11"), (64, "ic12"), (256, "ic13"), (512, "ic14"),
    ]
    parts = []
    for size, typ in blocks:
        data = png_bytes(im1024.resize((size, size), Image.LANCZOS))
        parts.append(typ.encode("ascii") + struct.pack(">I", len(data) + 8) + data)
    body = b"".join(parts)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def build_ico(im1024: Image.Image) -> bytes:
    """生成多尺寸 ico(16/24/32/48/64/128/256)"""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for s in sizes:
        im = im1024.resize((s, s), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        images.append((s, buf.getvalue()))
    # ICONDIR(6) + ICONDIRENTRY * n(16 each) + 图像数据
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    for s, data in images:
        entries += struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0,
                               0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(d for _s, d in images)


def main():
    im1024 = draw_icon(SIZE)
    # 1. 高清源图
    png1024 = os.path.join(BASE_DIR, "app_icon.png")
    im1024.save(png1024, format="PNG")
    print(f"OK app_icon.png 1024x1024 -> {os.path.getsize(png1024)}B")
    # 2. 完整 icns
    icns = build_icns(im1024)
    icns_path = os.path.join(BASE_DIR, "app_icon.icns")
    with open(icns_path, "wb") as f:
        f.write(icns)
    print(f"OK app_icon.icns {len(icns)}B")
    # 3. 多尺寸 ico(Windows 安装包图标同步升级)
    ico = build_ico(im1024)
    ico_path = os.path.join(BASE_DIR, "app_icon.ico")
    with open(ico_path, "wb") as f:
        f.write(ico)
    print(f"OK app_icon.ico {len(ico)}B")


if __name__ == "__main__":
    main()
