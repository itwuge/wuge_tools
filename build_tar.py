# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: build_tar.py
# 归属: 项目根目录/构建辅助脚本层 —— Linux/macOS 打包产物归档脚本
# ------------------------------------------------------------------------------
# 文件用途:
#   将 PyInstaller 构建产物(dist/ 目录下的主程序与更新助手)打包为 tar.gz
#   归档文件,供 GitHub Actions 发布 Release 使用.脚本仅运行于非 Windows 平台
#   (Linux 与 macOS),并根据平台差异采用不同的打包策略:
#     - Linux:  主程序为单文件可执行程序(onefile),更新助手为 onedir 目录
#     - macOS:  主程序与更新助手均为 --windowed 生成的 .app 应用包目录
#   打包完成后执行内置校验,确保归档结构完整且未误包含用户数据目录.
# ------------------------------------------------------------------------------
# 架构定位:
#   属于构建辅助脚本(Build Helper),由 GitHub Actions workflow(build.yml)
#   在 PyInstaller 构建完成后调用.本脚本不参与应用运行时逻辑,仅在 CI/CD
#   流水线中执行一次,产物为 wuge_tools-{OS_NAME}.tar.gz.
#
#   关联组件:
#     - 上游: .github/workflows/build.yml(在「打包为 tar.gz」步骤调用)
#     - 下游: 无(纯标准库实现,不依赖项目业务模块)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 从环境变量 OS_NAME 读取当前平台名(Linux / macOS)
#   2. 根据平台定位构建产物路径:
#      - Linux:  dist/个人小工具 + dist/update/update
#      - macOS:  dist/个人小工具.app + dist/update.app
#   3. 前置校验:关键构建产物必须存在,否则抛错终止
#   4. 打包为 tar.gz(程序根平铺结构,解压即用)
#   5. 内置校验:
#      - Linux: 主程序文件存在、update 目录结构完整(update/update、update/lib/)
#      - macOS: 两个 .app 包均非空
#      - 黑名单: 禁止打包用户数据目录(data_store、accounts、logs 等)
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责将已有构建产物打包归档,不执行 PyInstaller 构建
#   - 不修改任何源码或配置文件
#   - 不处理 Windows 平台(Windows 由 build_zip.py 负责)
#   - 内置校验失败时直接抛 AssertionError,由 CI 流水线捕获并标记构建失败
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: os(环境变量、路径拼接、文件存在判断)、tarfile(tar.gz 读写)
#   - 第三方: 无
#   - 项目内: 无(纯标准库,可独立运行)
# ==============================================================================

# 导入操作系统接口库,用于读取环境变量、拼接路径、判断文件/目录是否存在
import os

# 导入 sys 标准库,用于重配置标准输出流的编码(解决 Windows cp1252 无法编码中文的问题)
import sys

# 导入 tarfile 标准库,用于创建和读取 tar.gz 归档文件
import tarfile

# ------------------------------------------------------------------------------
# 跨平台编码兼容: 将 stdout/stderr 重配置为 UTF-8
# Windows CI Runner 默认控制台编码为 cp1252,无法编码中文字符,
# 在此处统一切换为 UTF-8 并设置 errors='replace' 兜底,确保 print 不会因编码中断
# ------------------------------------------------------------------------------
try:
    # reconfigure 是 Python 3.7+ 运行时方法,typeshed 的 TextIO 类型存根未声明,
    # 故加 type: ignore[attr-defined] 抑制 pyright/pylance 的属性未知误报
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass  # 旧版 Python 或特殊环境下 reconfigure 不可用时静默忽略,不影响打包逻辑


# ==============================================================================
# 函数: main
# ==============================================================================
def main() -> None:
    """
    主入口函数:将 dist/ 下的构建产物打包为 tar.gz 归档文件并执行内置校验.

    执行流程:
        1. 读取环境变量 OS_NAME 判断当前平台(Linux / macOS)
        2. 根据平台定位主程序与更新助手的构建产物路径
        3. 前置校验:关键产物必须存在,否则抛 ValueError
        4. 创建 tar.gz 归档,按平台差异写入对应成员
        5. 重新打开归档执行结构校验与黑名单检查

    :return: 无返回值;校验失败时抛出 AssertionError 或 ValueError
    :rtype: None
    :raises ValueError: 关键构建产物缺失时抛出
    :raises AssertionError: 归档结构校验失败时抛出
    """
    root = "dist"  # PyInstaller 默认输出目录,所有构建产物均位于此目录下
    os_name = os.environ["OS_NAME"]  # 从环境变量读取平台名,由 workflow 矩阵注入
    # macOS 有 arm64(macos-latest)和 x86_64(macos-13)两种架构,os_name 分别为 macOS-arm64 / macOS-x86_64
    # 统一用 startswith("macOS") 判断,确保两种架构都按 .app 包结构处理
    is_macos = os_name.startswith("macOS")  # macOS 上 --windowed 生成 .app 包(目录),Linux 生成单文件

    # ------------------------------------------------------------------
    # 阶段1: 根据平台定位构建产物路径并执行前置存在性校验
    # ------------------------------------------------------------------
    if is_macos:
        # ==================== macOS 分支 ====================
        # macOS: --windowed 产物为 .app 应用包目录
        main_path = os.path.join(root, "个人小工具.app")  # 主程序 .app 包路径
        update_path = os.path.join(root, "update.app")     # 更新助手 .app 包路径
        # 校验两个 .app 目录都存在;任一缺失则收集到 missing 列表
        missing = [p for p in (main_path, update_path) if not os.path.isdir(p)]
    else:
        # ==================== Linux 分支 ====================
        # Linux: 主程序为单文件可执行程序(onefile),更新助手为 onedir 目录
        bin_name = "个人小工具"  # Linux 主程序可执行文件名(无扩展名)
        main_path = os.path.join(root, bin_name)  # 主程序可执行文件路径
        update_path = os.path.join(root, "update", "update")  # 更新助手 onedir 可执行文件路径
        # 校验两个文件都存在;任一缺失则收集到 missing 列表
        missing = [p for p in (main_path, update_path) if not os.path.isfile(p)]

    # 若存在缺失的构建产物,抛出 ValueError 终止打包(由 CI 标记构建失败)
    if missing:
        raise ValueError(f"缺少打包成员:{missing}")

    # ------------------------------------------------------------------
    # 阶段2: 创建 tar.gz 归档文件并写入成员
    # ------------------------------------------------------------------
    out_name = f"wuge_tools-{os_name}.tar.gz"  # 输出归档文件名,如 wuge_tools-macOS.tar.gz

    # 以 gzip 压缩写入模式打开 tar 归档
    with tarfile.open(out_name, "w:gz") as t:
        if is_macos:
            # macOS: 打包两个 .app 应用包目录(递归包含包内所有文件)
            t.add(main_path, arcname="个人小工具.app")  # 主程序 .app 包写入归档根目录
            t.add(update_path, arcname="update.app")   # 更新助手 .app 包写入归档根目录
            print("archive members:", ["个人小工具.app", "update.app"])  # 打印归档成员清单
        else:
            # Linux: 打包主程序单文件 + update 目录(递归包含 lib/ 等依赖)
            t.add(main_path, arcname="个人小工具")  # 主程序可执行文件写入归档根目录
            t.add(os.path.join(root, "update"), arcname="update")  # update 目录整体写入归档根目录
            print("archive members:", ["个人小工具", "update"])  # 打印归档成员清单
    print(f"打包完成: {out_name}")  # 输出打包完成提示

    # ------------------------------------------------------------------
    # 阶段3: 重新打开归档执行结构校验与黑名单检查
    # ------------------------------------------------------------------
    # 以只读模式打开刚生成的 tar.gz 归档进行校验
    with tarfile.open(out_name, "r:gz") as t:
        names = t.getnames()  # 获取归档内所有成员的路径名列表
        print("tar内文件列表:", names)  # 打印完整文件列表,便于排查

        if is_macos:
            # macOS 校验:两个 .app 包都必须存在且内部有文件(非空目录)
            # any() 判断是否存在以 "个人小工具.app/" 开头的成员路径
            assert any(n.startswith("个人小工具.app/") for n in names), "主程序 .app 包为空"
            assert any(n.startswith("update.app/") for n in names), "更新助手 .app 包为空"
        else:
            # Linux 校验:主程序文件存在,update 目录结构完整
            assert "个人小工具" in names  # 主程序可执行文件必须在归档中
            assert "update/update" in names  # 更新助手可执行文件必须在归档中
            # onedir 依赖目录:PyInstaller 默认名为 _internal,spec 中可自定义为 lib
            # 命令行构建(Linux/macOS)用默认 _internal,spec 构建(Windows)用 lib
            # 此处同时兼容两种命名,避免跨平台断言不一致
            has_deps = any(
                n.startswith("update/_internal/") or n.startswith("update/lib/")
                for n in names
            )
            assert has_deps, "更新助手 onedir 依赖目录(_internal/ 或 lib/)必须存在"

        # 黑名单目录检查:禁止将用户运行数据目录打包进发布产物
        # 这些目录属于用户隐私/运行时数据,不应随发布包分发
        black = {"data_store", "accounts", "checkin_record", "cookies", "downloads", "logs", "mail_result"}
        for n in names:
            top_dir = n.split("/")[0]  # 取成员路径的顶层目录名(如 "update/lib/xxx" -> "update")
            assert top_dir not in black, f"禁止目录被打包: {top_dir}"  # 命中黑名单则断言失败
    print("[OK] archive layout ok")  # 校验通过,输出成功标记(ASCII 安全,避免 cp1252 编码 emoji 失败)


if __name__ == "__main__":  # 判断是否作为主脚本直接运行(而非被其他模块 import)
    main()  # 调用主入口函数执行打包流程
