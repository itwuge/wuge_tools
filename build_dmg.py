# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: build_dmg.py
# 归属: 项目根目录/构建辅助脚本层 —— macOS DMG 打包产物归档脚本
# ------------------------------------------------------------------------------
# 文件用途:
#   将 PyInstaller 构建产物(dist/ 目录下的主程序 .app 与更新助手 .app)
#   打包为 macOS 原生 DMG 磁盘镜像,供 GitHub Actions 发布 Release 使用.
#   脚本仅运行于 macOS 平台.
#
#   v1.0.8 起改为"豆包式"双击即安装引导:
#     - DMG 内仅一个主程序图标(个人小工具.app),不含独立"安装.app"
#     - 双击主程序图标 → main.py::_auto_install_from_dmg 自动安装到 /Applications
#     - 引导界面(渐变背景 + 居中图标 + 箭头 + "双击 安装"文案)由
#       build/macos/build_dmg_macos.sh 两阶段打包生成(豆包官方同款)
#     - 更新助手 update.app 仍嵌入主程序包 Contents/Resources 内
#
#   打包完成后执行内置校验,确保 DMG 文件已生成且非空.
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 从环境变量 OS_NAME 读取当前平台名(macOS-arm64 / macOS-x86_64)
#   2. 定位构建产物:dist/个人小工具.app + dist/update.app
#   3. 前置校验:两个 .app 包必须存在,否则抛错终止
#   4. 构造临时目录,复制主程序 .app 并将 update.app 嵌入其 Contents/Resources
#   5. 调用 build/macos/build_dmg_macos.sh 完成豆包式两阶段打包
#   6. 校验 DMG 文件存在且非空,清理临时目录
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: os, sys, shutil, subprocess
#   - 系统命令: bash, hdiutil, sips(macOS 自带)
#   - 项目脚本: build/macos/build_dmg_macos.sh, build/macos/make_dsstore.py
# ==============================================================================

# 导入操作系统接口库,用于读取环境变量、路径拼接、文件/目录判断
import os

# 导入高级文件操作库,用于递归复制目录、删除目录树
import shutil

# 导入子进程管理库,用于调用 bash 打包脚本
import subprocess

# 导入 sys 标准库,用于重配置标准输出流的编码
import sys

# ------------------------------------------------------------------------------
# 跨平台编码兼容: 将 stdout/stderr 重配置为 UTF-8
# ------------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# ==============================================================================
# 函数: main
# ==============================================================================
def main() -> None:
    """
    主入口函数:将 dist/ 下的主程序与更新助手打包为"豆包式"DMG 磁盘镜像并校验.

    :return: 无返回值;校验失败时抛出 AssertionError 或 ValueError
    :rtype: None
    :raises ValueError: 关键构建产物缺失时抛出
    :raises RuntimeError: 打包脚本执行失败时抛出
    :raises AssertionError: DMG 产物校验失败时抛出
    """
    root = "dist"  # PyInstaller 默认输出目录
    os_name = os.environ["OS_NAME"]  # macOS-arm64 / macOS-x86_64

    # ------------------------------------------------------------------
    # 阶段1: 定位构建产物并执行前置存在性校验
    # ------------------------------------------------------------------
    main_app = os.path.join(root, "个人小工具.app")  # 主程序 .app 包路径
    update_app = os.path.join(root, "update.app")    # 更新助手 .app 包路径
    # 两个 .app 目录都必须存在(v1.0.8 起不再需要独立的"安装.app")
    missing = [p for p in (main_app, update_app) if not os.path.isdir(p)]
    if missing:
        raise ValueError(f"缺少打包成员:{missing}")

    # ------------------------------------------------------------------
    # 阶段2: 构造 DMG 内容源临时目录(主程序 + 嵌入更新助手)
    # ------------------------------------------------------------------
    out_name = f"wuge_tools-{os_name}.dmg"       # 输出 DMG 文件名
    # 卷标名带版本号: 规避 Finder 按卷名缓存旧布局(同名卷会复用无背景缓存)
    version_tag = os.environ.get("VERSION", "").strip()  # CI 传入 github.ref_name(如 v1.0.2)
    volname = (  # 卷标名: 带版本则含版本号, 否则保持原名
        f"wuge_tools-{version_tag}-{os_name}" if version_tag else f"wuge_tools-{os_name}"
    )
    stage = "_dmg_stage"                          # 临时内容源目录

    # 清理可能残留的 stage 目录
    if os.path.exists(stage):
        shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)

    # 复制主程序 .app 包到 stage
    shutil.copytree(main_app, os.path.join(stage, "个人小工具.app"))
    # 将更新助手 update.app 嵌入主程序 .app 包的 Contents/Resources 下,
    # 确保主程序与更新助手始终在同一软件包内(更新器从包内定位 update.app)
    resources_dir = os.path.join(stage, "个人小工具.app", "Contents", "Resources")
    os.makedirs(resources_dir, exist_ok=True)
    shutil.copytree(update_app, os.path.join(resources_dir, "update.app"))

    # ------------------------------------------------------------------
    # 阶段3: 调用豆包式两阶段打包脚本(引导背景图 + .DS_Store 布局)
    # ------------------------------------------------------------------
    # build_dmg_macos.sh 负责: 阶段A Finder 生成原生 .DS_Store → 写入引导布局
    # → 阶段B 新 UUID 卷重建 → UDZO 压缩。背景图为 tiff@144dpi(豆包同款)。
    script = os.path.join("build", "macos", "build_dmg_macos.sh")
    cmd = ["bash", script, os.path.join(stage, "个人小工具.app"), out_name]
    print(f"执行: {' '.join(cmd)} (VOLNAME={volname})")
    env = {**os.environ, "VOLNAME": volname}
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"build_dmg_macos.sh 执行失败(exit={result.returncode})")

    # ------------------------------------------------------------------
    # 阶段4: 校验 DMG 产物并清理临时目录
    # ------------------------------------------------------------------
    assert os.path.isfile(out_name), f"DMG 文件未生成:{out_name}"
    assert os.path.getsize(out_name) > 0, "DMG 文件为空"
    print(f"打包完成: {out_name} ({os.path.getsize(out_name) // 1024 // 1024} MB)")

    # 清理临时内容源目录
    shutil.rmtree(stage, ignore_errors=True)
    print("[OK] dmg layout ok")

if __name__ == "__main__":
    main()
