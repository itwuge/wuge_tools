#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# 生成 DMG 的 .DS_Store: 复刻豆包官方"双击即安装"引导布局
# ----------------------------------------------------------------------------
# 背景:
#   豆包官方 DMG 的安装引导界面(浅蓝渐变 + 居中图标 + "双击 安装豆包")
#   由两部分构成:
#     1. 窗口背景图(静态元素)        -> 见 make_dmg_background.swift
#     2. .DS_Store(图标位置/背景引用) -> 本脚本
#   .DS_Store 是 Finder 保存"文件夹视图布局"的二进制文件, 记录:
#     - bwsp : 窗口状态(位置/尺寸/工具栏显隐)
#     - icvp : 图标视图参数(背景图/图标尺寸/排列方式)
#     - Iloc : 每个图标的像素坐标(豆包同款: 图标居中于背景图头像下方)
#     - vSrn : 视图序列号(视图配置版本)
#
# 关键经验(踩坑记录, 每条都是实测验证过的):
#   1. 【模板法】不能从零创建 .DS_Store —— Finder 会整体忽略,
#      必须基于"Finder 原生 .DS_Store"(build/macos/dsstore_template.bin)
#      修改。该模板由 Finder 在真实卷上生成, 含 bwsp/icvp/pBB0/pBBk/
#      vSrn/Iloc 六条记录, 页大小 4096, 结构被 Finder 认可。
#   2. 【两阶段写入】icvp 条目较大(含背景图 Alias), 直接 insert 替换
#      会触发 B-tree 分裂(ds_store 1.3.3 的 _split 在页满时返回 None
#      直接崩溃)。必须先 delete 旧 icvp(释放空间)再 insert, 避免分裂。
#   3. 【ds_store 库两个 bug 需 vendor 修复】(见 build/macos/vendor/):
#      a. byte_length() 用 plistlib 重编码长度而非原始 blob 字节数,
#         delete 时多删 3 字节导致页内偏移错乱(BuddyError/写坏文件);
#       b. macOS 15 Finder 写入的 pBBk 不是标准 bookmark, 原版 codecs
#          表里的 BookmarkCodec 读取会崩溃, 已从 codecs 表移除。
#      本脚本通过 sys.path 优先加载 vendor/ds_store, 不依赖全局安装。
#   4. 【Alias 修正】mac_alias 生成的 Carbon Alias 有两个字段 Finder
#      不认可(实测不显示背景图), 必须按 Finder 原生值修正:
#      - disk_type(偏移 45): 0x0000 -> 0x0005
#      - attribute_flags(偏移 136): 0x00000000 -> 0x0d020000
#      修正后 Finder 正常显示背景图(实测通过)。
#      ⚠ 偏移 45/136 仅对 mac_alias==2.2.3(414B 结构)有效;
#        2.1.x(410B 结构)对应偏移为 44/134 但 Finder 不认(已实测排除),
#        CI 必须锁 mac_alias==2.2.3(见 requirements.txt)。
#   5. 背景图必须用 2x Retina 尺寸(1200x640), 转 tiff@144dpi 后
#      Finder 按 600x320 显示完整内容(否则 1200 宽被窗口裁切)。
#
# 用法:
#   python3 make_dsstore.py <挂载点> <背景图绝对路径>
#   前提: 挂载点内已有从模板复制的 .DS_Store(见 build_dmg_macos.sh)
# ============================================================================

import os
import sys

# 优先加载 vendor 里的 patched ds_store(修复 byte_length/pBBk 两个 bug)
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

from ds_store import DSStore, DSStoreEntry       # noqa: E402  (vendor 加载后导入)
from ds_store.store import ILocCodec, PlistCodec  # noqa: E402
from mac_alias import Alias                        # noqa: E402


def _make_alias(bg_path: str) -> bytes:
    """生成指向背景图的 Carbon Alias, 并修正 Finder 必需的字段.

    :param bg_path: 背景图绝对路径(必须位于已挂载的 DMG 卷内)
    :return: 修正后的 Alias 二进制数据
    """
    # 注意: 必须用「字节 patch」而非「设置对象属性后再 to_bytes」——
    # 设置 disk_type=5 会让 mac_alias 序列化时改变布局(attribute_flags 从
    # 偏移 136 移到 134、字节序反转), Finder 不认; 字节 patch 只改字节、
    # 保持原始布局, Finder 才认(实测验证)。
    # 字段偏移基于 mac_alias v2.2.3 Carbon Alias 固定结构(暴力扫描实测,
    # 与 2.1.x 版本不同——CI 必须锁 mac_alias==2.2.3, 见 requirements.txt):
    #   偏移 45: disk_type(2字节)  Finder 原生为 5(HFS+ 卷)
    #   偏移 136: attribute_flags(4字节) Finder 原生为 0x0d020000
    raw = bytearray(Alias.for_file(bg_path).to_bytes())
    raw[45:47] = b"\x00\x05"
    raw[136:140] = b"\x0d\x02\x00\x00"
    return bytes(raw)


def write_dsstore(mount_point: str, bg_path: str) -> None:
    """在模板 .DS_Store 上写入豆包安装引导布局.

    :param mount_point: DMG 挂载点绝对路径(卷内须已有模板 .DS_Store)
    :param bg_path: 背景图绝对路径(位于挂载卷内)
    """
    assert os.path.isfile(bg_path), f"背景图不存在: {bg_path}"
    ds_path = os.path.join(mount_point, ".DS_Store")
    assert os.path.isfile(ds_path), (
        f".DS_Store 不存在: {ds_path}\n"
        f"必须先把模板 build/macos/dsstore_template.bin 复制到卷内"
        f"(见 build_dmg_macos.sh), 从零创建的 .DS_Store 不被 Finder 认可"
    )

    # 1. 生成并修正背景图 Alias
    alias_bytes = _make_alias(bg_path)

    # 2. 构造布局记录(豆包同款: 窗口 600x342, 图标 100pt 居中 302,100)
    bwsp = {
        "ContainerShowSidebar": True,
        "ShowPathbar": False,
        "ShowSidebar": True,
        "ShowStatusBar": False,
        "ShowTabView": False,
        "ShowToolbar": False,
        "SidebarWidth": 0,
        "WindowBounds": "{{100, 100}, {600, 342}}",
    }
    icvp = {
        "backgroundType": 2,
        "backgroundColorRed": 1.0,
        "backgroundColorGreen": 1.0,
        "backgroundColorBlue": 1.0,
        "showIconPreview": True,
        "showItemInfo": False,
        "textSize": 12.0,
        "iconSize": 64.0,
        "viewOptionsVersion": 1,
        "gridSpacing": 100.0,
        "gridOffsetX": 0.0,
        "gridOffsetY": 0.0,
        "labelOnBottom": True,
        "arrangeBy": "none",
        "backgroundImageAlias": alias_bytes,
    }

    # 3. 两阶段写入: 先删旧 icvp 释放空间, 再插入新记录(避免 B-tree 分裂)
    with DSStore.open(ds_path, "r+") as ds:
        # icvp 最大, 必须先删(模板里的旧 icvp 含指向旧卷的 Alias)
        ds.delete(".", b"icvp")
        ds.insert(DSStoreEntry(".", b"icvp", PlistCodec, icvp))
        # 其余条目较小, 直接 insert(存在则替换, 不会触发分裂)
        ds.insert(DSStoreEntry(".", b"bwsp", PlistCodec, bwsp))
        ds.insert(DSStoreEntry(".", b"vSrn", "long", 1))
        ds.insert(DSStoreEntry("个人小工具.app", b"Iloc", ILocCodec, (302, 100)))

    # 4. 读回校验: 确认 icvp 背景设置与背景图 Alias 已正确写入
    #    (防止 mac_alias 版本漂移/序列化异常导致背景图静默失效, CI 上早失败)
    with DSStore.open(ds_path, "r") as ds:
        icvp_entry = next((e for e in ds if e.code == b"icvp"), None)  # 定位 icvp 记录
        assert icvp_entry is not None, "icvp 记录缺失"  # 布局核心记录必须存在
        icvp_data = icvp_entry.value  # 解码后的视图参数
        assert icvp_data.get("backgroundType") == 2, "backgroundType 异常"  # 必须为图片背景
        alias_data = icvp_data.get("backgroundImageAlias") or b""  # 背景图 Alias 数据
        assert len(alias_data) >= 100, f"背景图 Alias 数据异常:{len(alias_data)}B"  # 有效 Alias 至少数百字节

    print(f".DS_Store 已写入(豆包式引导布局): {ds_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python3 make_dsstore.py <挂载点> <背景图绝对路径>")
        sys.exit(1)
    write_dsstore(sys.argv[1], sys.argv[2])
