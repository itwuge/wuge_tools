# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: build_zip.py
# 归属: 项目根目录/构建辅助脚本层 —— Windows 打包产物归档脚本
# ------------------------------------------------------------------------------
# 文件用途:
#   将 PyInstaller 构建产物(dist/ 目录下的主程序与更新助手)打包为 ZIP
#   归档文件,供 GitHub Actions 发布 Release 使用.脚本仅运行于 Windows 平台,
#   将 onedir 布局的主程序(个人小工具.exe)与更新助手(update/ 目录)
#   以「程序根平铺」结构写入 ZIP,用户解压后即可直接运行.
#   打包完成后执行内置校验(CRC 完整性、结构完整性、黑名单目录检查).
# ------------------------------------------------------------------------------
# 架构定位:
#   属于构建辅助脚本(Build Helper),由 GitHub Actions workflow(build.yml)
#   在 PyInstaller 构建完成后调用.本脚本不参与应用运行时逻辑,仅在 CI/CD
#   流水线的 Windows Runner 上执行一次,产物为 wuge_tools-Windows.zip.
#
#   关联组件:
#     - 上游: .github/workflows/build.yml(在「打包为 ZIP」步骤调用)
#     - 下游: 无(纯标准库实现,不依赖项目业务模块)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 从环境变量 OS_NAME 读取平台名(固定为 Windows)
#   2. 定位构建产物:dist/个人小工具.exe + dist/update/ 目录
#   3. 前置校验:主程序与更新助手可执行文件必须存在,否则抛错终止
#   4. 打包为 ZIP(DEFLATED 压缩,程序根平铺结构)
#   5. 内置校验:
#      - CRC 完整性校验(testzip 无损坏成员)
#      - 结构校验:主程序 .exe 存在、update/update.exe 存在、update/lib/ 存在
#      - 黑名单: 禁止打包用户数据目录(data_store、accounts、logs 等)
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责将已有构建产物打包归档,不执行 PyInstaller 构建
#   - 不修改任何源码或配置文件
#   - 不处理 Linux/macOS 平台(由 build_tar.py 负责)
#   - 内置校验失败时直接抛 AssertionError,由 CI 流水线捕获并标记构建失败
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: os(环境变量、路径拼接、文件遍历)、zipfile(ZIP 读写与 CRC 校验)
#   - 第三方: 无
#   - 项目内: 无(纯标准库,可独立运行)
# ==============================================================================

# 导入操作系统接口库,用于读取环境变量、拼接路径、遍历目录树
import os

# 导入 sys 标准库,用于重配置标准输出流的编码(解决 Windows cp1252 无法编码中文的问题)
import sys

# 导入 zipfile 标准库,用于创建 ZIP 归档、读取成员列表、执行 CRC 校验
import zipfile

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
    主入口函数:将 dist/ 下的构建产物打包为 ZIP 归档文件并执行内置校验.

    执行流程:
        1. 从环境变量 OS_NAME 读取平台名(Windows)
        2. 定位主程序(dist/个人小工具.exe)与更新助手(dist/update/)路径
        3. 前置校验:关键可执行文件必须存在,否则抛 ValueError
        4. 创建 ZIP 归档(DEFLATED 压缩):
           - 写入主程序 .exe 到归档根目录
           - 递归遍历 update/ 目录,写入所有依赖文件
        5. 重新打开 ZIP 执行 CRC 校验、结构校验与黑名单检查

    :return: 无返回值;校验失败时抛出 AssertionError 或 ValueError
    :rtype: None
    :raises ValueError: 关键构建产物缺失时抛出
    :raises AssertionError: 归档结构或 CRC 校验失败时抛出
    """
    root = "dist"  # PyInstaller 默认输出目录,所有构建产物均位于此目录下
    bin_name = "个人小工具.exe"  # Windows 主程序可执行文件名
    update_root = os.path.join(root, "update")  # 更新助手 onedir 目录路径(dist/update)

    # ------------------------------------------------------------------
    # 阶段1: 前置存在性校验
    # ------------------------------------------------------------------
    # 检查主程序 .exe 和更新助手 .exe 是否都存在;任一缺失则收集到 missing 列表
    missing = [
        path for path in (os.path.join(root, bin_name), os.path.join(update_root, "update.exe"))
        if not os.path.isfile(path)
    ]
    # 若存在缺失的构建产物,抛出 ValueError 终止打包(由 CI 标记构建失败)
    if missing:
        raise ValueError(f"缺少打包成员:{missing}")

    os_name = os.environ["OS_NAME"]  # 从环境变量读取平台名(此处固定为 Windows)
    out_name = f"wuge_tools-{os_name}.zip"  # 输出归档文件名,如 wuge_tools-Windows.zip

    # ------------------------------------------------------------------
    # 阶段2: 创建 ZIP 归档并写入成员
    # ------------------------------------------------------------------
    # 以 DEFLATED(默认压缩)写入模式打开 ZIP 归档
    with zipfile.ZipFile(out_name, "w", zipfile.ZIP_DEFLATED) as z:
        # 写入主程序可执行文件到归档根目录(arcname 指定归档内的路径名)
        src_file = os.path.join(root, bin_name)  # 主程序源文件完整路径
        z.write(src_file, arcname=bin_name)  # 将主程序写入 ZIP,归档内名为 "个人小工具.exe"

        # 递归遍历 update/ 目录,将所有文件写入 ZIP
        # os.walk 返回 (当前目录路径, 子目录列表, 文件列表)
        for dp, _, fs in os.walk(update_root):
            for f in fs:  # 遍历当前目录下的每个文件
                fp = os.path.join(dp, f)  # 构造文件的完整源路径
                arc = os.path.relpath(fp, root)  # 计算相对于 dist/ 的相对路径(保持目录结构)
                z.write(fp, arcname=arc)  # 写入 ZIP,归档内路径保持 update/... 结构
    print(f"打包完成: {out_name}")  # 输出打包完成提示
    print("archive members:", [bin_name, "update"])  # 打印归档成员清单

    # ------------------------------------------------------------------
    # 阶段3: 重新打开 ZIP 执行 CRC 校验、结构校验与黑名单检查
    # ------------------------------------------------------------------
    with zipfile.ZipFile(out_name, "r") as z:  # 以只读模式打开刚生成的 ZIP 进行校验
        # 获取归档内所有成员路径名,并统一将反斜杠替换为正斜杠(兼容 Windows 路径)
        names = [name.replace("\\", "/") for name in z.namelist()]

        # CRC 完整性校验:testzip 会读取并校验所有成员的 CRC32,返回首个损坏成员名或 None
        bad_member = z.testzip()
        assert bad_member is None, f"ZIP成员CRC校验失败:{bad_member}"  # 存在损坏成员则断言失败

        print("ZIP内文件列表:", names)  # 打印完整文件列表,便于排查

        # 结构校验:确保关键成员都存在
        assert "个人小工具.exe" in names  # 主程序可执行文件必须在归档中
        assert "update/update.exe" in names  # 更新助手可执行文件必须在归档中
        # onedir 依赖目录:spec 中 contents_directory='lib',但兼容 PyInstaller 默认 _internal
        has_deps = any(
            n.startswith("update/lib/") or n.startswith("update/_internal/")
            for n in names
        )
        assert has_deps, "更新助手 onedir 依赖目录(lib/ 或 _internal/)必须存在"

        # 黑名单目录检查:禁止将用户运行数据目录打包进发布产物
        # 这些目录属于用户隐私/运行时数据,不应随发布包分发
        black = {"data_store", "accounts", "checkin_record", "cookies", "downloads", "logs", "mail_result"}
        for n in names:
            top_dir = n.replace("\\", "/").split("/")[0]  # 取成员路径的顶层目录名
            assert top_dir not in black, f"禁止目录被打包: {top_dir}"  # 命中黑名单则断言失败
    print("[OK] archive layout ok")  # 校验通过,输出成功标记(ASCII 安全,避免 cp1252 编码 emoji 失败)


if __name__ == "__main__":  # 判断是否作为主脚本直接运行(而非被其他模块 import)
    main()  # 调用主入口函数执行打包流程
