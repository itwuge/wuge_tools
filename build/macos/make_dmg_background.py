# -*- coding: utf-8 -*-
# ============================================================================
# 生成 DMG 安装引导背景图(豆包式, 美化版)
# ----------------------------------------------------------------------------
# 原理:
#   macOS DMG 打开后 Finder 显示的安装引导 = 背景图(静态元素) + Finder 图标
#   背景图中的"箭头 + 文案"是静态元素, 与 .DS_Store 设定的图标位置精确对齐,
#   引导用户双击图标启动安装流程。
#
# 尺寸:
#   1200x640 @ 144 DPI(2x Retina, 豆包官方同款)。Finder 按 DPI 缩放显示为
#   600x320。PNG 源在构建时由 sips 转 tiff@144dpi 使用(见 build_dmg_macos.sh)。
#
# 坐标设计(注意: PIL 的 Y 轴从顶部向下, 与 AppKit 相反):
#   图标中心(2x) = .DS_Store Iloc(302,100) 逻辑 x2 = (604, 200)
#   布局自上而下: 图标卡片(75~325) -> 箭头(340~445) -> 主文案(480) -> 小字(560)
#
# 依赖: Pillow (pip install Pillow)
# 用法: python make_dmg_background.py [输出路径]
# ============================================================================

import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1200, 640             # 2x 画布尺寸(对应 600x320 逻辑)
ICON_CX, ICON_CY = 604, 200  # 图标中心(2x) - 对应 .DS_Store Iloc (302,100) 1x
# 深色适配配色(macOS 深色模式 Finder 会压暗背景图, 用偏亮元素抵消):
BRAND = (122, 188, 255)      # 箭头亮蓝(深色背景下醒目)
TEXT_MAIN = (255, 255, 255)  # 主文案纯白(压暗后仍清晰)
TEXT_SUB = (196, 210, 228)   # 小字浅灰蓝
CARD_OUTLINE = (214, 228, 250, 235)  # 卡片亮描边
CARD_FILL = (255, 255, 255, 235)     # 卡片亮白半透明(压暗后呈浅灰)
CARD_SHADOW = (0, 0, 0, 90)          # 深色投影(深色背景上更自然)
GRAD_TOP = (54, 74, 102)     # 渐变顶部: 深蓝灰
GRAD_BOTTOM = (28, 40, 58)   # 渐变底部: 更深蓝灰
GLOW_COLOR = (140, 180, 235) # 顶部光晕: 淡蓝(提亮图标区)
GLOW_ALPHA = 70


def find_font(bold: bool = False):
    """探测系统中文字体(Windows 微软雅黑 / macOS 苹方), 保证 CI 与本地一致"""
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def vertical_gradient(w: int, h: int, top, bottom):
    """垂直渐变底色(顶部浅蓝 -> 底部近白)"""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        d.line([(0, y), (w, y)], fill=color)
    return img


def radial_glow(w: int, h: int, cx: int, cy: int, radius: int, color, alpha: int):
    """径向光斑(半透明柔和光晕), 用于顶部提亮"""
    layer = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(layer)
    steps = 24
    for i in range(steps, 0, -1):
        r = int(radius * i / steps)
        a = int(alpha / steps)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=a)
    layer = layer.filter(ImageFilter.GaussianBlur(radius / 3))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow.paste(color, (0, 0), layer)
    return glow


def rounded_card_with_shadow(img, box, radius, fill, outline, outline_w, shadow):
    """带柔和投影的圆角卡片(图标底衬), 先画偏移模糊投影再叠卡片"""
    x1, y1, x2, y2 = box
    # 投影层
    shadow_img = Image.new("RGBA", (img.size[0] + 40, img.size[1] + 40), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_img)
    sd.rounded_rectangle([x1 + 8, y1 + 16, x2 + 8, y2 + 16], radius=radius, fill=shadow)
    shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(14))
    img.alpha_composite(shadow_img, (0, 0))
    # 卡片本体
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=fill, outline=outline, width=outline_w)


def draw_arrow(d, cx, tip_y, color, width=10, head_w=58, head_h=44, tail=26):
    """扁平化短箭头: 纯色三角头 + 短杆, 朝下指向文案"""
    d.polygon([(cx, tip_y), (cx - head_w // 2, tip_y + head_h), (cx + head_w // 2, tip_y + head_h)], fill=color)
    d.line([(cx, tip_y + head_h), (cx, tip_y + head_h + tail)], fill=color, width=width)


def draw_text_center(d, text, y, size, color, font_path, spacing=0, shadow=None, ttc_index=0):
    """居中绘制文字, 支持字距微调与轻微阴影(提升精致感)"""
    f = ImageFont.truetype(font_path, size, index=ttc_index)
    tw = d.textlength(text, font=f)
    x = (W - tw) / 2
    # 阴影(先画偏移的深色文字)
    if shadow:
        sx = x + 2
        sy = y + 3
        if spacing == 0:
            d.text((sx, sy), text, font=f, fill=shadow)
        else:
            for ch in text:
                d.text((sx, sy), ch, font=f, fill=shadow)
                sx += d.textlength(ch, font=f) + spacing
        x = (W - tw) / 2
    # 主体
    if spacing == 0:
        d.text((x, y), text, font=f, fill=color)
    else:
        for ch in text:
            d.text((x, y), ch, font=f, fill=color)
            x += d.textlength(ch, font=f) + spacing
    return tw


def main(out_path: str) -> None:
    font_path = find_font(bold=False)
    font_path_b = find_font(bold=True) or font_path
    assert font_path, "未找到中文字体(需要微软雅黑/苹方)"

    # 1. 底色: 深蓝灰渐变(深色模式适配, 抵消 Finder 压暗)
    img = vertical_gradient(W, H, GRAD_TOP, GRAD_BOTTOM).convert("RGBA")

    # 2. 顶部柔和光晕(淡蓝提亮图标区域)
    glow = radial_glow(W, H, W // 2, 200, 460, GLOW_COLOR, GLOW_ALPHA)
    img.alpha_composite(glow)

    # 3. 图标底衬卡片: 亮白半透明 + 亮描边 + 深色投影(深色模式下呈浅灰圆角框)
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    card_size = 200
    card_cy = 225   # 卡片下移: 让 Finder 图标名显示在卡片上方深色区
    card_box = [ICON_CX - card_size // 2, card_cy - card_size // 2,
                ICON_CX + card_size // 2, card_cy + card_size // 2]
    rounded_card_with_shadow(
        card, card_box, radius=56,
        fill=CARD_FILL, outline=CARD_OUTLINE, outline_w=6,
        shadow=CARD_SHADOW,
    )
    img.alpha_composite(card)

    # 4. 箭头: 卡片下方, 精致圆头箭头(朝下, 亮蓝色)
    d = ImageDraw.Draw(img)
    # 中指手势(小巧版)
    mx, my = ICON_CX, 400
    skin = (255, 210, 175, 255)
    skin_dark = (220, 170, 130, 255)
    # 手掌
    d.rounded_rectangle([mx-24, my-15, mx+24, my+35], radius=12, fill=skin)
    # 中指
    d.rounded_rectangle([mx-8, my-58, mx+8, my-12], radius=8, fill=skin)
    # 中指指甲
    d.rounded_rectangle([mx-6, my-55, mx+6, my-45], radius=3, fill=(255,240,225,255))
    # 无名指
    d.rounded_rectangle([mx-20, my-28, mx-8, my-8], radius=5, fill=skin_dark)
    # 食指
    d.rounded_rectangle([mx+8, my-28, mx+20, my-8], radius=5, fill=skin_dark)

    # 5. 底部主文案(冬青黑体 W6, 深色阴影提亮)
    HIRAGINO = "/System/Library/Fonts/Hiragino Sans GB.ttc"
    draw_text_center(
        d, "双击 安装 软件", 470, 56, TEXT_MAIN, HIRAGINO,
        spacing=4, shadow=(0, 0, 0, 160), ttc_index=1,
    )

    # 6. 底部小字提示
    draw_text_center(d, "安装完成后,可到\"应用程序\"中启动", 540, 24, TEXT_SUB, font_path, spacing=2)

    # 7. 导出 PNG
    img.convert("RGB").save(out_path, "PNG")
    print(f"背景图已生成(美化版): {out_path} {W}x{H}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "dmg_background.png")
    main(out)
