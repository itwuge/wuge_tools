# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: installer.py
# 归属: updater_app 更新助手包 —— 安装器模块(Installer / Service 层)
# ------------------------------------------------------------------------------
# 文件用途:
#   更新助手的文件替换核心模块[纯标准库实现,不依赖 PySide6,可单独测试].
#   负责执行完整的软件更新安装流程,包括等待旧进程退出、解压更新包、
#   备份旧文件、复制新文件、失败回滚、启动新程序等操作.
# ------------------------------------------------------------------------------
# 架构定位:
#   相当于 MVC 架构中的 Service 层(业务服务层),被 InstallWorker 在后台线程中调用.
#   纯算法实现,不涉及 UI、不涉及 Qt、不涉及网络,仅操作文件系统和进程.
#   设计目标是"无黑窗"——全程不生成/不启动任何 cmd/bat/VBS/taskkill/xcopy 脚本,
#   全部使用原生 API 和标准库实现.
#
#   关联组件:
#     - 上游: InstallWorker(Worker 层,在 QThread 中调用本模块)
#     - 下游: 无(纯标准库 + infrastructure.archive)
# ------------------------------------------------------------------------------
# 核心功能(安装步骤):
#   1. 等待旧主程序退出:
#      - 任务模式(old_pid>0): 精准等待主程序移交时的 PID
#      - 独立模式(old_pid=0): 按主程序进程名枚举等待(零外部程序,纯 API)
#   2. 前置校验: 主程序文件必须存在(助手放错目录时直接拦截,绝不误写其他文件)
#   3. 解压 tar.gz 到 _update_stage(兼容单层顶层目录嵌套)
#   4. 旧主程序改名 .old_del 备份(可回滚)
#   5. 递归复制新文件:
#      - 更新助手自身正在运行无法覆盖 -> 落为 update.exe.new_pending,
#        主程序下次启动时完成助手自我升级
#      - 其余文件直接覆盖
#   6. 成功: 删除备份/stage/更新包,返回新主程序路径
#      失败: 把 .old_del 改回原路径完成回滚,返回错误
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只做文件替换和进程操作,不涉及 UI、网络、配置读写
#   - 不创建线程,由调用方(InstallWorker)决定是否在子线程执行
#   - 失败时尽力回滚,保证主程序可用
#   - 全程不启动外部脚本/程序,避免黑窗和安全软件拦截
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块所有函数均为阻塞式同步调用,无内部线程.
#   实际使用时由 InstallWorker 在 QThread 子线程中调用,避免阻塞 UI 主线程.
#   内部使用 threading 相关的仅为日志回调(通过 log_cb 参数传入),
#   调用方需保证回调函数的线程安全性.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging, os, shutil, subprocess, sys, time, typing
#   - 第三方: 无
#   - 项目内: infrastructure.archive.extract(按扩展名解压ZIP/tar.gz)
# ==============================================================================

# 导入日志记录库,用于记录安装过程中的日志信息
import logging

# 导入操作系统接口库,用于路径操作、文件操作、环境变量、进程操作
import os

# 导入高级文件操作库,用于递归复制、删除目录等
import shutil

# 导入子进程管理库,用于非 Windows 平台启动主程序
import subprocess

# 导入系统相关库,用于平台判断、退出操作
import sys

# 导入时间处理库,用于超时等待和重试间隔
import time

# 导入类型提示库,用于函数签名的类型注解
from typing import Any, Callable, Dict, List, Optional

# 从基础设施层导入统一解压入口:Windows ZIP、Linux tar.gz(macOS 走 dmg 挂载流程)
from infrastructure.archive import extract

# 创建模块级日志器实例,命名空间为 Updater.Installer
_logger = logging.getLogger("Updater.Installer")

# ------------------------------------------------------------------------------
# 安装阶段常量定义(与主程序 runtime.model.update_launcher 保持一致)
# ------------------------------------------------------------------------------

# 临时解压目录名,用于存放解压后的更新文件
STAGE_DIR_NAME = "_update_stage"
# 旧主程序备份文件后缀,安装失败时用于回滚
OLD_EXE_SUFFIX = ".old_del"
# 更新助手待替换文件后缀(正在运行无法直接覆盖,下次启动生效)
PENDING_SUFFIX = ".new_pending"

# 任务模式: 等待指定旧主程序 PID 退出的超时时间(毫秒)
# 主程序移交后通常秒退,30 秒足够应对极端情况
WAIT_OLD_TIMEOUT_MS = 30000
# 独立模式: 用户手动关闭正在运行的主程序,给更宽的等待时间(毫秒)
# 2 分钟给用户足够的时间保存工作并关闭主程序
WAIT_NAMED_TIMEOUT_MS = 120000
# 文件 IO 重试次数(杀毒软件实时扫描、Windows 文件句柄延迟释放可能占用文件)
_IO_TRIES = 15
# 文件 IO 重试间隔(秒)
_IO_INTERVAL = 1.0

# 日志回调函数类型别名: 可选的字符串参数回调函数
LogCb = Optional[Callable[[str], None]]


# ==============================================================================
# 函数: install_update
# ==============================================================================
def install_update(
    package_path: str,
    app_dir: str,
    app_exe: str,
    old_pid: int,
    self_exe_name: str,
    log_cb: LogCb = None,
    wait_exe_name: str = "",
) -> Dict[str, Any]:
    """
    执行完整的更新安装流程.

    详细说明:
        这是安装器的主入口函数,执行以下步骤:
        1. 前置校验(主程序文件必须存在)
        2. 等待旧主程序退出(按 PID 或按进程名)
        3. 解压更新包到临时目录
        4. 兼容单层顶层目录嵌套
        5. 备份旧主程序(改名为 .old_del)
        6. 递归复制新文件到软件目录(助手自身走 pending)
        7. 确定安装后的主程序路径
        8. 清理临时文件和备份
        9. 失败时自动回滚

        失败时已尽力回滚,保证主程序可用.

    参数:
        package_path (str): 更新包绝对路径(Windows .zip,Linux .tar.gz,macOS .dmg)
        app_dir (str): 软件安装目录的绝对路径(替换目标目录)
        app_exe (str): 旧主程序可执行文件的绝对路径
        old_pid (int): 旧主程序的进程 ID;任务模式>0,独立模式=0
        self_exe_name (str): 更新助手自身的可执行文件名(用于 pending 逻辑)
        log_cb (LogCb): 日志回调函数,接收字符串参数;可选,默认为 None
        wait_exe_name (str): 独立模式下按此进程名等待主程序关闭;默认为空串

    返回值:
        Dict[str, Any]: 安装结果字典,包含以下字段:
            - ok (bool): 安装是否成功
            - launch_exe (str): 应启动的主程序路径(成功为新路径,失败为旧路径)
            - msg (str): 结果描述信息

    异常:
        不向外抛出异常;所有异常都在内部捕获并通过返回值传达
    """
    # ------------------------------------------------------------------
    # 内部辅助: 日志输出(同时写文件日志和回调 UI 日志)
    # ------------------------------------------------------------------
    def log(msg: str):
        # 写入文件日志(INFO 级别)
        _logger.info(msg)
        # 如果提供了日志回调函数,则调用回调(用于 UI 日志显示)
        if log_cb:
            try:
                # 调用回调函数,传入日志消息
                log_cb(msg)
            except Exception:
                # 回调异常静默忽略,不影响安装流程
                pass

    # ------------------------------------------------------------------
    # 路径规范化
    # ------------------------------------------------------------------
    # 将更新包路径转为绝对路径
    package_path = os.path.abspath(package_path)
    # 将软件目录转为绝对路径
    app_dir = os.path.abspath(app_dir)
    # 空串保留为空(abspath("") 会变成当前工作目录,污染前置校验的报错信息)
    app_exe = os.path.abspath(app_exe) if app_exe else ""
    # 构造临时解压目录的完整路径
    stage_root = os.path.join(app_dir, STAGE_DIR_NAME)

    # macOS: 主程序是 .app 包(目录),备份/回滚需操作整个 .app 目录而非单个可执行文件
    if sys.platform == "darwin" and app_exe:
        # 从 app_exe 路径中提取 .app 包目录路径
        # 例如: /App/个人小工具.app/Contents/MacOS/个人小工具 -> /App/个人小工具.app
        _app_bundle_dir = ""  # .app 包目录的绝对路径
        _parts = app_exe.split(os.sep)
        for _i, _p in enumerate(_parts):
            if _p.endswith(".app"):
                _app_bundle_dir = os.sep.join(_parts[:_i + 1])  # 拼接 .app 包完整路径
                break
        if _app_bundle_dir and os.path.isdir(_app_bundle_dir):
            # 备份路径: .app 包目录 + .old_del 后缀
            backup_exe = _app_bundle_dir + OLD_EXE_SUFFIX
        else:
            # 未找到 .app 包,退化为单文件备份
            backup_exe = app_exe + OLD_EXE_SUFFIX
    else:
        # 非 macOS: 备份单个可执行文件
        backup_exe = app_exe + OLD_EXE_SUFFIX

    # ------------------------------------------------------------------
    # 0. 前置校验: 主程序必须真实存在
    # ------------------------------------------------------------------
    # 防止助手被放错目录时向无关目录写文件,造成安全风险
    if not app_exe or not os.path.isfile(app_exe):
        # 构造错误提示信息
        msg = (
            f"未找到主程序({app_exe or wait_exe_name or '未知'}),无法安装。\n"
            "请把更新助手与主程序放在同一软件目录后重试。"
        )
        # 记录错误日志
        log(f"❌ {msg}")
        # 返回失败结果,launch_exe 为原路径(可能为空)
        return {"ok": False, "launch_exe": app_exe, "msg": msg}

    # ------------------------------------------------------------------
    # 1. 等待旧主程序退出
    # ------------------------------------------------------------------
    # 任务模式按 PID 精准等待;独立模式按进程名等待
    if old_pid > 0:
        # 任务模式: 等待指定 PID 的进程退出
        log(f"⏳ 等待主程序退出(PID={old_pid})……")
        # 调用等待函数,设置超时时间
        if not _wait_process_exit(old_pid, WAIT_OLD_TIMEOUT_MS):
            # 超时未退出,构造错误信息
            msg = f"等待主程序(PID={old_pid})退出超时,已取消本次更新"
            # 记录错误日志
            log(f"❌ {msg}")
            # 返回失败结果
            return {"ok": False, "launch_exe": app_exe, "msg": msg}
    elif wait_exe_name:
        # 独立模式: 按进程名等待所有同名主程序退出
        log(f"⏳ 确认主程序({wait_exe_name})已关闭……")
        # 调用按名称等待函数,排除自身进程
        if not _wait_named_processes_exit(
            wait_exe_name, WAIT_NAMED_TIMEOUT_MS, log, exclude_pid=os.getpid()
        ):
            # 超时仍有进程在运行,构造错误信息
            msg = f"等待主程序({wait_exe_name})关闭超时,已取消本次更新"
            # 记录错误日志
            log(f"❌ {msg}")
            # 返回失败结果
            return {"ok": False, "launch_exe": app_exe, "msg": msg}
    # 主程序已成功退出,记录日志
    log("✅ 主程序已退出,开始安装")

    # 等待 2 秒,让 Windows 完全释放进程持有的文件句柄
    # 进程退出后内核仍需时间清理句柄表,立即操作文件可能触发 WinError 32
    time.sleep(2)

    # ------------------------------------------------------------------
    # Windows 安装版分支: 通过 setup.exe 静默覆盖安装(Inno Setup)
    # 安装目录(如 Program Files)通常只读,不能直接替换文件;
    # 安装程序会关闭运行中的程序(CloseApplications)并保留数据目录
    # ------------------------------------------------------------------
    if sys.platform.startswith("win") and os.path.isfile(os.path.join(app_dir, "installed.flag")):
        return _install_from_setup(package_path, app_dir, log)

    # ------------------------------------------------------------------
    # 2. 解压更新包
    # ------------------------------------------------------------------
    # 先清理可能存在的旧 stage 目录(忽略错误)
    _retry_io(lambda: shutil.rmtree(stage_root, ignore_errors=True))

    # ------------------------------------------------------------------
    # macOS DMG 分支: 挂载磁盘镜像 -> 复制 .app 包 -> 卸载
    # 不走 tar.gz 解压流程, 由 _install_from_dmg 独立完成安装与回滚
    # ------------------------------------------------------------------
    if sys.platform == "darwin" and package_path.lower().endswith(".dmg"):
        return _install_from_dmg(package_path, app_dir, app_exe, backup_exe, self_exe_name, log)

    # 按扩展名调用统一解压入口:Windows只使用ZIP,Linux只使用tar.gz
    # (macOS 旧格式 tar.gz 仍走此分支作为兜底)
    expected_ext = ".zip" if sys.platform.startswith("win") else ".tar.gz"
    if not package_path.lower().endswith(expected_ext):
        msg = f"当前平台更新包格式必须为{expected_ext}"
        log(f"❌ {msg}")
        _safe_rmtree(stage_root)
        return {"ok": False, "launch_exe": app_exe, "msg": msg}
    if not extract(package_path, stage_root):
        # 解压失败,构造错误信息
        msg = "更新包解压失败,主程序未被改动"
        # 记录错误日志
        log(f"❌ {msg}")
        # 安全清理 stage 目录(忽略错误)
        _safe_rmtree(stage_root)
        # 返回失败结果
        return {"ok": False, "launch_exe": app_exe, "msg": msg}

    # ------------------------------------------------------------------
    # 2.1 兼容打包时的单层顶层目录
    # ------------------------------------------------------------------
    # 若归档内意外套一层同名目录,检测后提升为实际更新源
    stage_dir = stage_root  # 默认使用 stage_root 作为源目录
    try:
        # 列出解压目录下的所有条目
        entries = os.listdir(stage_root)
        # 如果只有一个条目且是目录,则认为是单层顶层目录嵌套
        if len(entries) == 1 and os.path.isdir(os.path.join(stage_root, entries[0])):
            # 将该子目录提升为实际的更新源目录
            stage_dir = os.path.join(stage_root, entries[0])
            # 记录日志,告知用户检测到顶层目录
            log(f"📁 检测到顶层目录'{entries[0]}',提升为更新源")
    except OSError as e:
        # 读取解压目录失败,清理并返回错误
        _safe_rmtree(stage_root)
        return {"ok": False, "launch_exe": app_exe, "msg": f"读取解压目录失败:{e}"}

    # ------------------------------------------------------------------
    # 3. 备份旧主程序 + 4. 复制新文件 + 5. 确定启动路径 + 6. 清理
    # ------------------------------------------------------------------
    # 使用 try-except 包裹,失败时执行回滚
    renamed = False  # 标记是否已重命名(备份)旧主程序
    try:
        # 3. 备份旧主程序(如果存在)
        # macOS 上 backup_exe 是 .app 目录路径,非 macOS 上是单文件路径
        backup_target = backup_exe  # 备份目标路径(.app 目录或单文件)
        if os.path.exists(backup_target):  # 旧主程序存在(.app 目录或文件)
            # 如果备份已存在(上次异常残留),先删除
            if os.path.exists(backup_exe):
                if os.path.isdir(backup_exe):
                    _retry_io(lambda: shutil.rmtree(backup_exe, ignore_errors=True))
                else:
                    _retry_io(lambda: os.remove(backup_exe))
            # 将旧主程序重命名为备份
            _retry_io(lambda: os.rename(backup_target, backup_exe))
            # 标记已完成重命名
            renamed = True
            # 记录备份日志
            log(f"📦 旧主程序已备份:{os.path.basename(backup_exe)}")

        # 4. 复制新文件(助手自身走 pending,其余直接覆盖)
        # 更新包只允许包含程序文件;用户数据目录必须保留在原位置且不能被包覆盖
        pending_files = _copy_tree(stage_dir, app_dir, self_exe_name, log)

        # 5. 确定安装后的主程序路径(包内主程序名可能变化)
        launch_exe = _resolve_launch_exe(stage_dir, app_dir, app_exe)
        # 校验新主程序是否存在
        if not launch_exe or not os.path.isfile(launch_exe):
            # 未找到主程序,抛出运行时异常,触发回滚
            raise RuntimeError(f"安装后未找到主程序:{launch_exe}")
        # 非 Windows 平台: 确保主程序有可执行权限
        if not sys.platform.startswith("win"):
            try:
                # 设置文件权限为 755(所有者读写执行,组和其他读执行)
                os.chmod(launch_exe, 0o755)
            except OSError:
                # 权限设置失败静默忽略,不影响安装结果
                pass
        # 记录安装完成日志
        log(f"🎉 文件替换完成:{os.path.basename(launch_exe)}"
            + (f",更新助手{len(pending_files)}个文件待下次启动升级" if pending_files else ""))

        # 6. 清理备份/stage/更新包(主程序已退出,备份可直接删)
        # 删除备份(如果存在);macOS 上备份是 .app 目录,需用 rmtree
        if renamed and os.path.exists(backup_exe):
            if os.path.isdir(backup_exe):
                _retry_io(lambda: shutil.rmtree(backup_exe, ignore_errors=True), raises=False)
            else:
                _retry_io(lambda: os.remove(backup_exe), raises=False)
        # 删除 stage 临时目录
        _safe_rmtree(stage_root)
        # 删除更新包源文件
        _retry_io(lambda: os.remove(package_path), raises=False)
        # 记录清理完成日志
        log("🧹 临时文件与更新包已清理")
        # 返回成功结果
        return {"ok": True, "launch_exe": launch_exe, "msg": "安装完成"}

    except Exception as e:
        # 安装过程中出现异常,记录错误日志(带堆栈)
        _logger.error(f"❌ 安装失败:{e}", exc_info=True)
        # 记录错误到 UI 日志
        log(f"❌ 安装失败:{e},正在回滚……")

        # 7. 回滚: 删除半成品主程序,把备份改回原路径
        if renamed:
            try:
                # 如果新主程序存在(半成品),先删除
                if os.path.exists(backup_target):
                    if os.path.isdir(backup_target):
                        shutil.rmtree(backup_target, ignore_errors=True)
                    else:
                        os.remove(backup_target)
                # 如果备份存在,改回原路径
                if os.path.exists(backup_exe):
                    os.rename(backup_exe, backup_target)
                # 记录回滚成功日志
                log(f"↩️ 已回滚旧主程序:{os.path.basename(backup_target)}")
            except OSError as rb_err:
                # 回滚失败,记录严重错误日志(带堆栈)
                _logger.critical(f"💥 回滚失败:{rb_err}", exc_info=True)
                # 记录回滚失败到 UI 日志
                log(f"💥 回滚失败:{rb_err},请从压缩包手动恢复")
        # 清理 stage 临时目录
        _safe_rmtree(stage_root)
        # 返回失败结果
        return {"ok": False, "launch_exe": app_exe, "msg": f"安装失败已回滚:{e}"}


# ======================================================================
# 内部工具函数区域
# ======================================================================

# ==============================================================================
# 函数: _copy_tree
# ==============================================================================
def _copy_tree(src_dir: str, dst_dir: str, self_exe_name: str, log: Callable[[str], None]) -> list:
    """
    递归复制更新源目录到目标软件目录.

    详细说明:
        更新助手自身正在运行,其可执行文件及 onedir 布局下 lib/ 目录中的
        DLL/PYD 等依赖文件均被进程锁定,无法直接覆盖.
        本函数先扫描定位 self_exe_name 所在目录,该目录下的所有文件都走 pending
        机制(改落为 <name>.new_pending),由主程序下次启动时递归替换.
        匹配范围不限定顶层目录:
        - onefile 布局下 update.exe 在根目录,无独立依赖目录
        - onedir 布局下 update.exe 在 update/ 子目录,lib/ 依赖同目录

    参数:
        src_dir (str): 源目录路径(解压后的更新文件目录)
        dst_dir (str): 目标目录路径(软件安装目录)
        self_exe_name (str): 更新助手自身的可执行文件名
        log (Callable[[str], None]): 日志回调函数

    返回值:
        list: 走 pending 机制的文件名列表(即更新助手自身的文件)

    异常:
        文件操作失败时抛出 OSError,由调用方捕获并处理回滚
    """
    # 初始化 pending 文件列表
    pending = []
    # 数据目录属于用户运行数据,不应由更新包解压覆盖或散落到软件根目录
    protected_top_dirs = {
        "data_store", "accounts", "checkin_record", "cookies",
        "downloads", "logs", "mail_result", "config",
    }
    # 将助手文件名转为小写,用于不区分大小写的比较
    self_lower = (self_exe_name or "").lower()

    # ------------------------------------------------------------------
    # 第一阶段: 扫描定位更新助手所在目录(相对于 src_dir)
    # onedir 布局下 lib/ 中的 DLL/PYD 也会被运行中的 update.exe 锁定,
    # 必须将这些文件也走 pending 机制,而不能直接覆盖
    # ------------------------------------------------------------------
    self_dir_rel = ""  # 更新助手所在目录的相对路径(默认空=根目录)
    for root, _d, files in os.walk(src_dir):
        for name in files:
            if name.lower() == self_lower:
                self_dir_rel = os.path.relpath(root, src_dir)
                break
        if self_dir_rel:
            break
    # 记录助手目录,便于调试
    if self_dir_rel and self_dir_rel != ".":
        log(f"📁 检测到更新助手目录:{self_dir_rel},该目录下文件将走 pending")

    # ------------------------------------------------------------------
    # 第二阶段: 递归复制,助手目录下的文件走 pending,其余直接覆盖
    # ------------------------------------------------------------------
    # 递归遍历源目录
    # root: 当前目录路径,_dirs: 子目录列表(不使用,用下划线标记),files: 文件列表
    for root, dirs, files in os.walk(src_dir):
        # 在源根层直接剪枝用户数据目录,兼容并防御历史错误更新包
        rel = os.path.relpath(root, src_dir)
        if rel == ".":
            skipped = [name for name in dirs if name.lower() in protected_top_dirs]
            dirs[:] = [name for name in dirs if name.lower() not in protected_top_dirs]
            for name in skipped:
                log(f"⏭️ 跳过更新包中的用户数据目录:{name}")
        # 计算当前目录相对于源目录的相对路径
        # 计算目标目录路径: 根目录直接用 dst_dir,子目录拼接相对路径
        target_dir = dst_dir if rel == "." else os.path.join(dst_dir, rel)
        # 确保目标目录存在(不存在则创建)
        os.makedirs(target_dir, exist_ok=True)

        # 判断当前目录是否在更新助手目录下(含助手目录自身)
        # self_dir_rel="update" 时,匹配 "update" 和 "update/xxx"
        in_self_dir = False
        if self_dir_rel:
            if rel == self_dir_rel:
                in_self_dir = True
            elif self_dir_rel != "." and rel.startswith(self_dir_rel + os.sep):
                in_self_dir = True

        # 遍历当前目录下的所有文件
        for name in files:
            # 构造源文件的完整路径
            src_file = os.path.join(root, name)
            # 判断是否需要走 pending:
            # 1. 文件名等于助手本体(不区分目录层级) -> onefile 的 update.exe
            # 2. 文件在助手目录下 -> onedir 的 lib/*.dll 等
            if name.lower() == self_lower or in_self_dir:
                # 走 pending: 目标路径加 .new_pending 后缀
                dst_file = os.path.join(target_dir, name + PENDING_SUFFIX)
                # 将文件名加入 pending 列表
                pending.append(name)
                # 记录 pending 日志
                if name.lower() == self_lower:
                    log(f"⏭️ 更新助手运行中,新版暂存:{name}{PENDING_SUFFIX}(下次启动生效)")
                else:
                    log(f"⏭️ 助手依赖被占用,新版暂存:{name}{PENDING_SUFFIX}(下次启动生效)")
            else:
                # 普通文件,目标路径为目标目录 + 文件名
                dst_file = os.path.join(target_dir, name)
            # 复制文件(保留元数据),使用重试机制应对杀软占用
            # Windows 上直接覆盖写容易触发 WinError 32(文件被占用),
            # 先尝试删除目标文件(删除比覆盖更容易成功),再复制新文件
            _retry_io(lambda d=dst_file: _safe_replace_file(d))  # 先清理目标文件
            _retry_io(lambda s=src_file, d=dst_file: shutil.copy2(s, d))  # 再复制新文件
    # 返回 pending 文件列表
    return pending


# ==============================================================================
# 函数: _resolve_launch_exe
# ==============================================================================
def _resolve_launch_exe(stage_dir: str, app_dir: str, app_exe: str) -> str:
    """
    识别更新包内的主程序,返回安装后的绝对路径.

    详细说明:
        优先匹配旧主程序的同名文件;如果包内没有同名文件,
        则取包内第一个非 update 的可执行文件作为主程序.
        用于处理更新包中主程序文件名可能变化的情况.

    参数:
        stage_dir (str): 更新源目录路径(解压后的目录)
        app_dir (str): 软件安装目录路径(安装目标)
        app_exe (str): 旧主程序的完整路径,用于提取文件名做匹配

    返回值:
        str: 安装后主程序的绝对路径字符串

    异常:
        目录读取失败时抛出 OSError,由调用方捕获
    """
    # 提取旧主程序的文件名(不含路径)
    cur_name = os.path.basename(app_exe)
    # macOS: 主程序是 .app 包(目录),需特殊处理
    if sys.platform == "darwin":
        # 优先匹配旧主程序所在的 .app 包名
        # app_exe 形如 /App/个人小工具.app/Contents/MacOS/个人小工具
        # 需要找到 .app 包目录名
        old_app_bundle = ""
        parts = app_exe.split(os.sep)
        for i, p in enumerate(parts):
            if p.endswith(".app"):
                old_app_bundle = p  # 找到旧主程序的 .app 包名
                break
        # 列出 stage 目录下的所有 .app 包,排除 update.app
        candidates = [
            f for f in os.listdir(stage_dir)
            if f.endswith(".app")
            and os.path.isdir(os.path.join(stage_dir, f))
            and f.lower() != "update.app"
        ]
        # 确定主程序 .app 包名: 同名优先,否则取第一个候选
        bundle_name = old_app_bundle if old_app_bundle in candidates else (
            candidates[0] if candidates else (old_app_bundle or "")
        )
        if not bundle_name:
            return ""  # 未找到任何 .app 包
        # 可执行文件名通常与 .app 包名(去掉 .app)一致
        exe_name = bundle_name[:-4] if bundle_name.endswith(".app") else bundle_name
        # 返回安装后 .app 包内的可执行文件路径
        return os.path.join(app_dir, bundle_name, "Contents", "MacOS", exe_name)
    # 根据平台筛选候选可执行文件
    if sys.platform.startswith("win"):
        # Windows 平台: 筛选所有 .exe 文件,排除 update.exe
        candidates = [
            f for f in os.listdir(stage_dir)
            if f.lower().endswith(".exe")
            and os.path.isfile(os.path.join(stage_dir, f))
            and f.lower() != "update.exe"
        ]
    else:
        # 非 Windows 平台: 筛选所有可执行文件,排除库文件和 update
        candidates = []
        for f in os.listdir(stage_dir):
            # 构造文件完整路径
            p = os.path.join(stage_dir, f)
            # 判断是否为普通文件、是否有可执行权限、不是库文件、不是更新助手
            if (os.path.isfile(p) and os.access(p, os.X_OK)
                    and not f.endswith((".so", ".dylib")) and f != "update"):
                candidates.append(f)
    # 确定主程序名: 同名优先,否则取第一个候选,都没有则用旧名
    name = cur_name if cur_name in candidates else (candidates[0] if candidates else cur_name)
    # 拼接安装后的完整路径并返回
    return os.path.join(app_dir, name)


# ==============================================================================
# 函数: _install_from_dmg
# ==============================================================================
def _install_from_setup(
    package_path: str,
    app_dir: str,
    log: Callable[[str], None],
) -> Dict[str, Any]:
    """Windows 安装版更新: 静默运行 setup.exe 由 Inno Setup 完成覆盖安装.

    安装版程序目录(Program Files)对普通用户只读,不能直接替换文件,
    只能把新版本 setup.exe 交给 Inno Setup 静默执行(/VERYSILENT),
    安装程序会自动关闭运行中的进程(含更新助手自身)并覆盖旧版本.

    :param package_path: 下载的 setup.exe 绝对路径
    :type package_path: str
    :param app_dir: 软件安装目录(工作目录,供安装程序定位)
    :type app_dir: str
    :param log: 日志回调函数
    :type log: Callable[[str], None]
    :return: 结果字典;ok=True 且 setup_launched=True 表示已启动安装程序
    :rtype: Dict[str, Any]
    """
    # 前置校验: 安装版更新包必须是 setup.exe
    if not package_path.lower().endswith('.exe'):
        msg = '安装版更新包必须为 setup.exe'
        log(f"❌ {msg}")
        return {"ok": False, "launch_exe": "", "msg": msg}
    # 静默安装参数: /VERYSILENT 无界面 /SUPPRESSMSGBOXES 不弹消息框
    # /NORESTART 不重启 /SP- 跳过"准备安装"页(需要管理员时仍会弹 UAC 由用户确认)
    log("🪟 检测到安装版,启动静默安装程序(覆盖安装)……")
    try:
        subprocess.Popen(
            [package_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
            cwd=app_dir,
            close_fds=True,
        )
    except OSError as e:
        msg = f"启动安装程序失败:{e}"
        log(f"❌ {msg}")
        return {"ok": False, "launch_exe": "", "msg": msg}
    # 安装程序启动成功后立即返回;Inno Setup 的 CloseApplications 会关闭更新助手,
    # 安装完成后按 [Run] 自动启动新版本主程序
    return {"ok": True, "launch_exe": "", "msg": "已启动安装程序", "setup_launched": True}


def _install_from_dmg(
    package_path: str,
    app_dir: str,
    app_exe: str,
    backup_exe: str,
    self_exe_name: str,
    log: Callable[[str], None],
) -> Dict[str, Any]:
    """
    macOS DMG 更新安装: 挂载磁盘镜像, 复制 .app 包替换旧版本, 卸载镜像.

    详细说明:
        与 tar.gz 解压安装不同, DMG 是磁盘镜像, 需通过 hdiutil 挂载后
        直接复制其中的 .app 包.DMG 内仅含一个主程序 .app 包,
        更新助手 update.app 已嵌入主程序 .app/Contents/Resources/ 下,
        因此复制主程序 .app 即同时完成主程序与更新助手的替换.
        流程: 备份旧 .app(改名 .old_del) -> 复制新 .app -> 清理备份.
        失败时回滚旧 .app 包, 保证主程序可用.
        注: 更新助手正在旧 .app 包内运行, macOS 允许重命名运行中进程的父目录,
            新 .app 复制完成后助手退出, 旧 .old_del 由主程序启动时清理.

    参数:
        package_path (str): DMG 文件绝对路径
        app_dir (str): 软件安装目录
        app_exe (str): 旧主程序可执行文件路径
        backup_exe (str): 旧主程序备份路径(.app 目录 + .old_del)
        self_exe_name (str): 更新助手自身可执行文件名
        log (Callable[[str], None]): 日志回调

    返回值:
        Dict[str, Any]: 安装结果字典, 同 install_update 返回结构
    """
    mount_point = ""  # DMG 挂载点, finally 中确保卸载
    renamed = False   # 是否已备份旧主程序
    dst_main = ""     # 新主程序 .app 目标路径(回滚用)
    orig_bundle_dir = ""  # 旧主程序 .app 目录路径(回滚用)
    try:
        # 1. 挂载 DMG
        mount_point = _mount_dmg(package_path)
        if not mount_point:
            return {"ok": False, "launch_exe": app_exe, "msg": "DMG 挂载失败"}
        log(f"📀 DMG 已挂载: {mount_point}")

        # 2. 定位 DMG 内的主程序 .app 包(update.app 已嵌入主程序包内, DMG 根仅一个 .app)
        entries = os.listdir(mount_point)
        app_bundles = [
            e for e in entries
            if e.endswith(".app") and os.path.isdir(os.path.join(mount_point, e))
        ]
        if not app_bundles:
            raise RuntimeError("DMG 内未找到主程序 .app 包")
        # 精确匹配主程序名:不取 app_bundles[0](os.listdir 顺序不保证, v1.0.7 曾因
        # DMG 根混入"安装.app"而误装), 始终选择主程序"个人小工具.app";
        # 若未来 DMG 内出现其他 .app, 精确匹配可防止再次误选
        MAIN_BUNDLE_NAME = "个人小工具.app"
        main_bundle = MAIN_BUNDLE_NAME if MAIN_BUNDLE_NAME in app_bundles else app_bundles[0]

        # 旧主程序 .app 包目录路径 = backup_exe 去掉 .old_del 后缀
        orig_bundle_dir = backup_exe[:-len(OLD_EXE_SUFFIX)] if backup_exe.endswith(OLD_EXE_SUFFIX) else ""
        # 新主程序目标路径
        dst_main = os.path.join(app_dir, main_bundle)

        # 3. 备份旧主程序 .app 包(改名为 .old_del)
        if orig_bundle_dir and os.path.isdir(orig_bundle_dir):
            if os.path.exists(backup_exe):
                _retry_io(lambda: shutil.rmtree(backup_exe, ignore_errors=True))
            _retry_io(lambda: os.rename(orig_bundle_dir, backup_exe))
            renamed = True
            log(f"📦 旧主程序已备份: {os.path.basename(backup_exe)}")

        # 4. 复制新主程序 .app 包到安装目录(内含新版 update.app)
        src_main = os.path.join(mount_point, main_bundle)
        _retry_io(lambda: shutil.copytree(src_main, dst_main))
        log(f"✅ 主程序已替换: {main_bundle}(含更新助手)")

        # 5. 确定新主程序可执行文件路径并设置权限
        exe_name = main_bundle[:-4] if main_bundle.endswith(".app") else main_bundle
        launch_exe = os.path.join(app_dir, main_bundle, "Contents", "MacOS", exe_name)
        if not os.path.isfile(launch_exe):
            raise RuntimeError(f"安装后未找到主程序: {launch_exe}")
        try:
            os.chmod(launch_exe, 0o755)
        except OSError:
            pass

        # 6. 清理备份(更新助手退出后由主程序启动清理 .old_del, 此处尽力而为)
        if renamed and os.path.exists(backup_exe):
            _retry_io(lambda: shutil.rmtree(backup_exe, ignore_errors=True), raises=False)
        log("🧹 临时文件已清理")
        return {"ok": True, "launch_exe": launch_exe, "msg": "安装完成"}

    except Exception as e:
        _logger.error(f"❌ DMG 安装失败: {e}", exc_info=True)
        log(f"❌ DMG 安装失败: {e}, 正在回滚……")
        # 回滚: 删除新包, 把备份改回原路径
        if renamed:
            try:
                if os.path.exists(dst_main):
                    shutil.rmtree(dst_main, ignore_errors=True)
                if os.path.exists(backup_exe):
                    os.rename(backup_exe, orig_bundle_dir)
                log("↩️ 已回滚旧主程序")
            except OSError as rb_err:
                _logger.critical(f"💥 回滚失败: {rb_err}", exc_info=True)
                log(f"💥 回滚失败: {rb_err}")
        return {"ok": False, "launch_exe": app_exe, "msg": f"安装失败已回滚: {e}"}
    finally:
        # 7. 卸载 DMG(无论成功失败); 卸载失败必须记录, 否则 /Volumes 残留同名卷
        if mount_point and not _detach_dmg(mount_point):
            _logger.error(f"DMG 卸载失败, 残留挂载卷: {mount_point}")


# ==============================================================================
# 函数: _mount_dmg
# ==============================================================================
def _mount_dmg(dmg_path: str) -> str:
    """
    挂载 DMG 磁盘镜像, 返回挂载点路径; 失败返回空串.

    参数:
        dmg_path (str): DMG 文件路径

    返回值:
        str: 挂载点路径(如 /Volumes/wuge_tools-macOS-arm64); 失败返回 ""
    """
    try:
        # -nobrowse: 不在 Finder 中显示(避免弹出 Finder 窗口)
        # -plist: 结构化输出, 挂载点含空格序号(如 "/Volumes/xxx 2")时
        #         split() 文本解析会拆坏路径(曾导致 os.listdir 报目录不存在),
        #         plist 的 system-entities[].mount-point 是完整可靠的挂载点
        result = subprocess.run(
            ["hdiutil", "attach", dmg_path, "-nobrowse", "-plist"],
            capture_output=True, text=True, timeout=180,
        )
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0:
            _logger.error(f"hdiutil attach 失败: {combined.strip()}")
            return ""
        # 首选 plist 解析: 取最后一个带挂载点的 system-entities 实体
        try:
            import plistlib
            plist = plistlib.loads((result.stdout or "").encode("utf-8"))
            for ent in reversed(plist.get("system-entities", [])):
                mp = ent.get("mount-point", "")
                if mp:
                    return mp
            # plist 存在但无挂载点: 记录并继续走文本兜底
            _logger.warning(f"attach plist 无挂载点, 尝试文本解析: {combined.strip()}")
        except Exception as pe:
            _logger.warning(f"attach plist 解析失败, 尝试文本解析: {pe}")
        # 兜底: 文本解析(找 /Volumes/ 开头的字段; 挂载点带空格序号时无法还原, 仅兼容旧 hdiutil)
        for line in combined.splitlines():
            if "/Volumes/" in line:
                parts = line.split()
                for p in parts:
                    if p.startswith("/Volumes/"):
                        return p
        # 挂载命令成功但未解析到挂载点: 记录完整输出供排查
        _logger.error(f"hdiutil attach 成功但未解析到挂载点: {combined.strip()}")
        return ""
    except Exception as e:
        _logger.error(f"挂载 DMG 异常: {e}")
        return ""


# ==============================================================================
# 函数: _detach_dmg
# ==============================================================================
def _detach_dmg(mount_point: str) -> bool:
    """
    卸载 DMG 挂载点; 常规卸载失败时自动以 -force 重试一次.

    说明:
        早期版本不检查 returncode, detach 失败被静默吞掉,
        导致 /Volumes 下残留同名挂载卷, 下次更新 attach 冲突;
        现在返回 bool 供调用方记录, 并输出可手动执行的卸载命令.

    参数:
        mount_point (str): 挂载点路径

    返回值:
        bool: True 表示卸载成功(含无需卸载); False 表示卸载失败(已记录日志)
    """
    if not mount_point:
        return True
    for attempt, force in enumerate((False, True)):  # 先常规卸载, 失败再 -force 强卸
        try:
            cmd = ["hdiutil", "detach", mount_point, "-quiet"]
            if force:
                cmd.append("-force")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                return True
            if attempt == 0:  # 第一次失败: 记录原因并准备强卸
                _logger.warning(
                    f"卸载 DMG 常规失败(ret={proc.returncode}): {mount_point} | "
                    f"{proc.stderr.strip()[:200]}, 尝试 -force")
        except Exception as e:
            _logger.warning(f"卸载 DMG 异常: {mount_point} | {e}")
    _logger.error(f"卸载 DMG 失败(已重试-force): {mount_point}, "
                  f"请手动执行: hdiutil detach {mount_point}")
    return False


# ==============================================================================
# 函数: _wait_process_exit
# ==============================================================================
def _wait_process_exit(pid: int, timeout_ms: int) -> bool:
    """
    等待指定 PID 的进程退出.

    详细说明:
        纯 API 实现,不启动 taskkill 等外部程序,避免黑窗.
        Windows 平台使用 OpenProcess/WaitForSingleObject API;
        非 Windows 平台使用 os.kill(pid, 0) 轮询检测.

    参数:
        pid (int): 要等待的进程 ID;<=0 时直接返回 True
        timeout_ms (int): 超时时间,单位为毫秒

    返回值:
        bool: True 表示进程已退出;False 表示超时仍未退出

    异常:
        不向外抛出异常;API 调用失败时按"进程已退出"处理(通常是进程已不存在)
    """
    # PID 无效时直接返回 True(视为已退出)
    if pid <= 0:
        return True
    # 根据平台选择实现方式
    if sys.platform.startswith("win"):
        # Windows 平台: 使用 Win32 API
        import ctypes

        # 获取 kernel32.dll 的函数表
        k32 = ctypes.windll.kernel32
        # 同步访问权限标志
        SYNCHRONIZE = 0x00100000
        # 进程仍在运行的退出码
        STILL_ACTIVE = 259
        # 打开进程句柄,请求同步权限
        handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
        # 打不开句柄通常意味着进程已退出,直接返回 True
        if not handle:
            return True
        try:
            # 设置 WaitForSingleObject 的参数类型
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            # 设置 WaitForSingleObject 的返回值类型
            k32.WaitForSingleObject.restype = ctypes.c_uint32
            # 等待进程对象变为有信号状态(即进程退出)
            k32.WaitForSingleObject(handle, timeout_ms)
            # 准备退出码变量
            exit_code = ctypes.c_ulong()
            # 获取进程退出码
            if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                # 获取失败,返回 False(不确定状态)
                return False
            # 退出码不等于 STILL_ACTIVE 表示进程已退出
            return exit_code.value != STILL_ACTIVE
        finally:
            # 确保关闭进程句柄,释放资源
            k32.CloseHandle(handle)
    else:
        # 非 Windows 平台: 使用 os.kill 轮询
        # 计算超时截止时间
        deadline = time.time() + timeout_ms / 1000.0
        # 循环检测直到超时
        while time.time() < deadline:
            try:
                # 发送信号 0 不实际影响进程,仅用于检测进程是否存在
                os.kill(pid, 0)
            except ProcessLookupError:
                # 进程不存在,返回 True
                return True
            except PermissionError:
                # 权限不足但进程存在,继续等待
                pass
            # 休眠 0.2 秒后再检测
            time.sleep(0.2)
        # 超时,返回 False
        return False


# ==============================================================================
# 函数: _list_pids_by_name
# ==============================================================================
def _list_pids_by_name(exe_name: str) -> List[int]:
    """
    按可执行文件名枚举所有匹配的进程 PID.

    详细说明:
        纯 API/标准库实现,不启动 taskkill 等外部程序,避免黑窗.
        - Windows: 使用 Toolhelp32 进程快照 API
        - Linux: 扫描 /proc/<pid>/comm 文件
        - 其余平台: 返回空列表

    参数:
        exe_name (str): 要匹配的可执行文件名(不区分大小写)

    返回值:
        List[int]: 匹配的进程 ID 列表;无匹配或平台不支持时返回空列表

    异常:
        不向外抛出异常;API 调用失败时返回空列表
    """
    # 提取文件名并转为小写(去除可能的路径,不区分大小写比较)
    target = os.path.basename(exe_name or "").lower()
    # 目标名为空时直接返回空列表
    if not target:
        return []
    # 初始化 PID 列表
    pids: List[int] = []
    # 根据平台选择实现方式
    if sys.platform.startswith("win"):
        # Windows 平台: 使用 Toolhelp32 快照 API
        import ctypes
        from ctypes import wintypes

        # 进程快照标志: 快照中包含所有进程
        TH32CS_SNAPPROCESS = 0x00000002
        # 无效句柄值
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        # 定义 PROCESSENTRY32W 结构体(Unicode 版本)
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),              # 结构体大小
                ("cntUsage", wintypes.DWORD),             # 进程引用计数
                ("th32ProcessID", wintypes.DWORD),        # 进程 ID
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),  # 默认堆 ID
                ("th32ModuleID", wintypes.DWORD),         # 模块 ID
                ("cntThreads", wintypes.DWORD),          # 线程数
                ("th32ParentProcessID", wintypes.DWORD),  # 父进程 ID
                ("pcPriClassBase", ctypes.c_long),        # 优先级基数
                ("dwFlags", wintypes.DWORD),              # 标志
                ("szExeFile", ctypes.c_wchar * 260),      # 可执行文件名(宽字符数组)
            ]

        # 获取 kernel32.dll 的函数表
        k32 = ctypes.windll.kernel32
        # 创建进程快照
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        # 检查快照是否创建成功
        if not snap or snap == INVALID_HANDLE_VALUE:
            return []
        try:
            # 初始化进程条目结构体
            entry = PROCESSENTRY32W()
            # 设置结构体大小(必须,API 要求)
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            # 获取第一个进程条目
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            # 循环遍历所有进程
            while ok:
                # 比较可执行文件名(不区分大小写)
                if entry.szExeFile.lower() == target:
                    # 匹配成功,将 PID 加入列表
                    pids.append(int(entry.th32ProcessID))
                # 获取下一个进程条目
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            # 确保关闭快照句柄,释放资源
            k32.CloseHandle(snap)
    elif sys.platform.startswith("linux"):
        # Linux 平台: 扫描 /proc 文件系统
        # 遍历 /proc 目录下的所有条目
        for name in os.listdir("/proc"):
            # 只处理数字命名的目录(即进程 ID)
            if not name.isdigit():
                continue
            try:
                # 读取进程的 comm 文件(进程名)
                with open(f"/proc/{name}/comm", "r", encoding="utf-8", errors="ignore") as f:
                    # 比较进程名(去除首尾空白,不区分大小写)
                    if f.read().strip().lower() == target:
                        # 匹配成功,将 PID 加入列表
                        pids.append(int(name))
            except OSError:
                # 读取失败(进程可能已退出),跳过
                continue
    # 返回匹配的 PID 列表
    return pids


# ==============================================================================
# 函数: _wait_named_processes_exit
# ==============================================================================
def _wait_named_processes_exit(
    exe_name: str,
    timeout_ms: int,
    log: Callable[[str], None],
    exclude_pid: int = 0,
) -> bool:
    """
    独立模式: 等待所有同名主程序进程退出.

    详细说明:
        轮询间隔 0.5 秒;仅 frozen 产物执行(源码运行时主程序名是 python,等待无意义).
        在剩余 15 秒时会再次提示用户,避免用户以为程序卡死.

    参数:
        exe_name (str): 要等待的可执行文件名
        timeout_ms (int): 超时时间,单位为毫秒
        log (Callable[[str], None]): 日志回调函数
        exclude_pid (int): 需要排除的进程 ID(通常是更新助手自身);默认为 0(不排除)

    返回值:
        bool: True 表示所有匹配进程已退出;False 表示超时仍有进程在运行

    异常:
        不向外抛出异常
    """
    # 非 frozen 模式下直接返回 True(源码运行时主程序是 python 进程,等待无意义)
    if not getattr(sys, "frozen", False):
        return True
    # 获取所有匹配的 PID,排除指定的 PID
    pids = [p for p in _list_pids_by_name(exe_name) if p != exclude_pid]
    # 没有匹配的进程,直接返回 True
    if not pids:
        return True
    # 记录等待日志,告知用户需要关闭主程序
    log(
        f"⏳ 主程序 {os.path.basename(exe_name)} 仍在运行(PID={','.join(map(str, pids))}),"
        f"请关闭主程序以继续安装(最长等待{timeout_ms // 1000}秒)……"
    )
    # 计算超时截止时间
    deadline = time.time() + timeout_ms / 1000.0
    # 标记是否已经发出过剩余时间提示
    announced = False
    # 循环检测直到超时
    while time.time() < deadline:
        # 休眠 0.5 秒
        time.sleep(0.5)
        # 重新获取匹配的 PID 列表
        pids = [p for p in _list_pids_by_name(exe_name) if p != exclude_pid]
        # 所有进程都已退出,返回 True
        if not pids:
            return True
        # 计算剩余时间
        remain = deadline - time.time()
        # 剩余 15 秒时再提示一次,避免用户以为程序卡死
        if not announced and remain <= 15:
            log(f"⚠ 仍在等待主程序关闭(PID={','.join(map(str, pids))}),剩余等待约{int(remain)}秒……")
            # 标记已发出提示,避免重复提示
            announced = True
    # 超时,返回 False
    return False


# ==============================================================================
# 函数: launch_app
# ==============================================================================
def launch_app(exe_path: str, app_dir: str, log_cb: LogCb = None) -> bool:
    """
    以与资源管理器双击等效的方式启动 GUI 主程序(无控制台窗口).

    详细说明:
        Windows 平台使用 ShellExecuteW API,与用户双击效果一致,不会产生黑窗;
        非 Windows 平台使用 subprocess.Popen 启动,并重定向所有标准流.
        启动后立即返回,不等待程序退出.

    参数:
        exe_path (str): 要启动的可执行文件的绝对路径
        app_dir (str): 程序的工作目录
        log_cb (LogCb): 日志回调函数;可选,默认为 None

    返回值:
        bool: True 表示启动成功;False 表示启动失败

    异常:
        不向外抛出异常;所有启动异常都被捕获并返回 False
    """
    # ------------------------------------------------------------------
    # 内部辅助: 日志输出
    # ------------------------------------------------------------------
    def log(msg: str):
        # 写入文件日志
        _logger.info(msg)
        # 如果提供了回调,调用回调输出 UI 日志
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                # 回调异常静默忽略
                pass

    try:
        # 根据平台选择启动方式
        if sys.platform.startswith("win"):
            # Windows 平台: 使用 ShellExecuteW API
            import ctypes

            # 调用 ShellExecuteW 打开程序
            # 参数: 父窗口句柄(None)、操作("open")、程序路径、参数(None)、工作目录、显示方式(1=SW_SHOWNORMAL)
            rc = ctypes.windll.shell32.ShellExecuteW(None, "open", exe_path, None, app_dir, 1)
            # 返回值 <= 32 表示失败
            if rc <= 32:
                log(f"❌ ShellExecuteW启动失败,错误码={rc}")
                return False
        else:
            # 非 Windows 平台
            if sys.platform == "darwin":
                # macOS: 使用 open 命令启动 .app 包,确保以 GUI 应用方式运行
                # 从可执行文件路径反推 .app 包路径
                # 例如: /App/个人小工具.app/Contents/MacOS/个人小工具 -> /App/个人小工具.app
                app_bundle = exe_path
                parts = exe_path.split(os.sep)
                for i, p in enumerate(parts):
                    if p.endswith(".app"):
                        app_bundle = os.sep.join(parts[:i + 1])  # 拼接 .app 包完整路径
                        break
                # 使用 open 命令启动 .app 包
                subprocess.Popen(
                    ["open", app_bundle],  # open 命令 + .app 包路径
                    cwd=app_dir,           # 工作目录
                    close_fds=True,        # 关闭父进程的文件描述符
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                # Linux: 直接启动可执行文件
                subprocess.Popen(
                    [exe_path],           # 命令行参数列表
                    cwd=app_dir,          # 工作目录
                    close_fds=True,       # 关闭父进程的文件描述符
                    start_new_session=True,  # 创建新会话(脱离终端)
                    stdin=subprocess.DEVNULL,   # 标准输入重定向到 /dev/null
                    stdout=subprocess.DEVNULL,  # 标准输出重定向到 /dev/null
                    stderr=subprocess.DEVNULL,  # 标准错误重定向到 /dev/null
                )
        # 记录启动成功日志
        log(f"🚀 已启动主程序:{exe_path}")
        return True
    except Exception as e:
        # 捕获所有启动异常,记录失败日志
        log(f"❌ 启动主程序失败:{exe_path} | {e}")
        return False


# ==============================================================================
# 函数: hard_exit
# ==============================================================================
def hard_exit() -> None:
    """
    更新助手硬退出(强制终止进程).

    详细说明:
        Windows 平台使用 TerminateProcess API 直接终止当前进程,
        避免 PyInstaller 清理 _MEI 目录时弹出控制台黑窗.
        非 Windows 平台使用 os._exit(0) 直接退出,跳过常规清理.
        此函数不会返回,进程直接终止.

    参数:
        无

    返回值:
        None(函数不会返回,进程直接终止)

    异常:
        无(进程终止前不会抛出异常)
    """
    # 记录退出日志
    _logger.info("🛑 更新助手进程退出")
    try:
        # 刷新标准输出缓冲区,确保日志都写入
        sys.stdout.flush()
        # 刷新标准错误缓冲区
        sys.stderr.flush()
    except Exception:
        # 刷新失败静默忽略,不影响退出
        pass
    # 根据平台选择退出方式
    if sys.platform.startswith("win"):
        # Windows 平台: 使用 TerminateProcess 强制终止
        import ctypes

        # 获取当前进程句柄,然后以退出码 0 终止
        ctypes.windll.kernel32.TerminateProcess(
            ctypes.windll.kernel32.GetCurrentProcess(), 0
        )
    # 非 Windows 平台(或 Windows TerminateProcess 未生效的兜底): 直接退出
    os._exit(0)


# ==============================================================================
# 函数: _safe_rmtree
# ==============================================================================
def _safe_rmtree(path: str) -> None:
    """
    安全删除目录树,忽略所有错误.

    详细说明:
        对 shutil.rmtree 的包装,捕获所有异常,确保删除操作不会中断主流程.
        用于清理临时文件,即使失败也不影响安装结果.

    参数:
        path (str): 要删除的目录路径

    返回值:
        None

    异常:
        不向外抛出任何异常
    """
    try:
        # 递归删除目录树,忽略错误
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        # 捕获所有异常,静默忽略
        pass


# ==============================================================================
# 函数: _safe_replace_file
# ==============================================================================
def _safe_replace_file(dst_file: str):
    """
    安全清理目标文件,为复制新文件做准备.

    详细说明:
        Windows 上直接覆盖已有文件容易触发 WinError 32(文件被占用).
        本函数按以下策略清理目标文件:
        1. 目标文件不存在 → 直接返回(无需清理)
        2. 尝试直接删除 → 成功则返回
        3. 删除失败(被占用) → 尝试重命名为 .del_tmp 后缀
        4. 重命名也失败 → 抛出 OSError,由上层重试

    参数:
        dst_file (str): 目标文件路径

    返回值:
        None

    异常:
        OSError: 文件被占用且无法删除或重命名时抛出
    """
    # 目标文件不存在,无需清理
    if not os.path.exists(dst_file):
        return
    # 尝试直接删除
    try:
        os.remove(dst_file)
        return
    except OSError:
        # 删除失败(可能被占用),尝试重命名到临时名
        pass
    # 尝试重命名为 .del_tmp 后缀(重命名比删除更容易成功)
    tmp = dst_file + ".del_tmp"
    try:
        # 先清理可能残留的临时文件
        if os.path.exists(tmp):
            os.remove(tmp)
        # 重命名目标文件到临时名
        os.rename(dst_file, tmp)
        # 重命名成功后删除临时文件(忽略错误,下次启动会清理)
        try:
            os.remove(tmp)
        except OSError:
            pass
    except OSError:
        # 重命名也失败,抛出异常让上层重试
        raise


# ==============================================================================
# 函数: _retry_io
# ==============================================================================
def _retry_io(fn, tries: int = _IO_TRIES, interval: float = _IO_INTERVAL, raises: bool = True):
    """
    文件 IO 重试机制,应对杀毒软件实时扫描及 Windows 文件句柄延迟释放导致的文件占用.

    详细说明:
        杀毒软件的实时扫描、Windows Search 索引服务、文件句柄延迟释放
        都可能在文件创建/修改后短暂锁定文件,导致首次 IO 操作失败.
        通过多次重试可以解决大部分此类问题.
        默认重试 15 次,每次间隔 1.0 秒,总共约 15 秒的容错窗口.

    参数:
        fn (Callable): 要执行的 IO 操作函数(无参数)
        tries (int): 最大重试次数;默认为 _IO_TRIES(15 次)
        interval (float): 重试间隔时间(秒);默认为 _IO_INTERVAL(1.0 秒)
        raises (bool): 全部失败后是否抛出异常;默认为 True

    返回值:
        Any: fn() 的返回值;失败且 raises=False 时返回 None

    异常:
        OSError: 当 raises=True 且所有重试都失败时抛出最后一次的异常
    """
    # 记录最后一次的异常
    last_err: Optional[OSError] = None
    # 循环重试指定次数
    for _ in range(tries):
        try:
            # 执行 IO 操作,成功则直接返回结果
            return fn()
        except OSError as e:
            # 记录异常
            last_err = e
            # 等待指定间隔后重试
            time.sleep(interval)
    # 所有重试都失败
    if raises and last_err is not None:
        # 需要抛出异常,则抛出最后一次的异常
        raise last_err
    # 不抛出异常,返回 None
    return None
