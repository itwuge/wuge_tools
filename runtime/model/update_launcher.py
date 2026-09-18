# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: update_launcher.py
# 归属: runtime/model 业务模型层(MVC-M) —— 外部更新助手启动器与残留清理器
# ------------------------------------------------------------------------------
# 文件用途:
#   主程序侧的"外部更新助手"启动器与更新残留清理器——负责定位更新助手、
#   生成更新任务 JSON、以无控制台黑窗的 GUI 方式启动同目录 update.exe,
#   以及主程序每次启动时应用助手自更新并清理上轮残留[纯标准库,不依赖
#   PySide6].
# ------------------------------------------------------------------------------
# 架构定位:
#   Model 角色;被 controller(更新确认/主程序启动流程)调用:
#   用户确认更新后写任务并启动助手,主程序启动最早期执行残留清理;
#   不依赖任何 view;Windows 下经 ctypes 调用 shell32.ShellExecuteW.
#
#   关联组件:
#     - 上游: controller(更新确认/主程序启动流程)
#     - 下游: runtime.model.app_config、ctypes(仅 Windows 分支)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 定位更新助手(兼容 onefile 与 onedir 两种 PyInstaller 布局)
#   2. 写 _update_task.json(主程序路径/PID/版本/数据目录/完整配置)
#   3. ShellExecuteW 以 windowed 方式启动助手(零 cmd/bat/VBS、零黑窗)
#   4. 启动时延迟替换 update.exe.new_pending、归档助手日志、
#      清理 *.old_del/_update_stage/旧 bat/VBS/残留任务文件
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责更新助手的启动与残留清理,不实现更新下载逻辑
#   - 更新下载与安装由独立的 update.exe 助手完成
#   - 不依赖任何 view 或 PySide6
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块函数均在调用方线程中同步执行;
#   ShellExecuteW 启动助手为异步进程,不阻塞调用方;
#   文件 IO 操作无并发保护,由调用方保证串行调用.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: json、logging、os、shutil、subprocess、sys、typing、
#             ctypes(仅 Windows 分支运行时导入)
#   - 第三方: 无
#   - 项目内: runtime.model.app_config(APP_BASE_DIR 软件根目录、
#             get_data_store_root 数据目录根)
# ==============================================================================
# =========** [UpdateLauncher]Model =========
import json  # 更新任务 JSON 序列化
import logging  # 标准日志库
import os  # 路径拼接/文件与目录存在判断/删除/替换
import shutil  # 高级文件操作:rmtree 递归删 stage 临时目录
import subprocess  # 非 Windows 平台启动助手(Popen)
import sys  # 判断 frozen 打包态/platform/executable/argv
from typing import List, Optional, Tuple  # 类型提示

from runtime.model.app_config import (  # 软件安装根目录、数据目录及实际配置定位信息
    APP_BASE_DIR,
    CONFIG_MANIFEST_FILENAME,
    get_active_config_root,
    get_data_store_root,
)

_logger = logging.getLogger("UpdateLauncher")  # 本模块专用 logger,日志名 UpdateLauncher

# 更新助手可执行文件名(与主程序同目录)
UPDATER_EXE_NAME = "update.exe" if sys.platform.startswith("win") else "update"  # Windows 带 .exe,类 Unix 无后缀
# 安装阶段临时目录/后缀
STAGE_DIR_NAME = "_update_stage"  # 解压新版本用的临时暂存目录名
OLD_EXE_SUFFIX = ".old_del"  # 被替换旧主程序的改名后缀,待下次启动清理
# 更新助手正在运行无法覆盖自身,新版先落为 .new_pending,主程序下次启动时替换
PENDING_SUFFIX = ".new_pending"  # 助手新版暂存后缀
# 任务描述文件(主程序写 -> 更新助手读 -> 安装结束删除)
TASK_FILE_NAME = "_update_task.json"  # 跨进程任务交接文件名
# 更新助手运行日志(归档进app.log后清理)
UPDATER_LOG_NAME = "_updater.log"  # 助手独立运行期写的日志,主程序启动时回收
# 旧版bat方案遗留物,顺手清理
LEGACY_FILES = (  # 历史版本曾用的脚本/日志,升级后统一删除
    "_update_restart.bat",  # 旧 Windows 重启批处理
    "_update_restart.sh",  # 旧类 Unix 重启脚本
    "_start_app.vbs",  # 旧 VBS 无窗启动脚本
    "_update_error.log",  # 旧错误日志
)


def get_updater_path() -> Optional[str]:
    """返回更新助手绝对路径;源码运行模式(无独立打包的助手)返回None

    兼容三种打包布局:
        - Windows onefile: 软件目录/update.exe
        - Windows onedir:  软件目录/update/update.exe
        - Linux:           软件目录/update/update(onedir)
        - macOS:           主程序.app/Contents/Resources/update.app/Contents/MacOS/update
                           (update.app 嵌入主程序 .app 包内,确保始终在一起)
    :return: 助手可执行文件绝对路径;源码运行或布局均缺失时返回 None
    :rtype: Optional[str]
    """
    if not getattr(sys, "frozen", False):  # PyInstaller 打包后才有 frozen 属性;源码运行无独立助手
        return None  # 源码模式直接判定不可用,无需继续查找文件
    # macOS: update.app 嵌入主程序 .app 包的 Contents/Resources 下
    if sys.platform == "darwin":
        # 主程序可执行文件名(如 "个人小工具"),对应 .app 包名 "个人小工具.app"
        main_exe_name = os.path.basename(os.path.abspath(sys.executable))
        # 主程序 .app 包路径: APP_BASE_DIR(= .app 父目录) + 主程序名.app
        main_app_dir = os.path.join(APP_BASE_DIR, main_exe_name + ".app")
        # 更新助手嵌入路径: 主程序.app/Contents/Resources/update.app/Contents/MacOS/update
        macos_path = os.path.join(
            main_app_dir, "Contents", "Resources", "update.app",
            "Contents", "MacOS", UPDATER_EXE_NAME,
        )
        if os.path.isfile(macos_path):  # .app 包内可执行文件存在
            return macos_path  # 返回 macOS 布局的助手路径
        # 兼容旧布局: update.app 与主程序 .app 平级(同目录)
        legacy_path = os.path.join(APP_BASE_DIR, "update.app", "Contents", "MacOS", UPDATER_EXE_NAME)
        if os.path.isfile(legacy_path):
            return legacy_path
        return None  # macOS 所有布局都未找到
    # 非 macOS 平台:onefile 模式优先(向后兼容)
    path = os.path.join(APP_BASE_DIR, UPDATER_EXE_NAME)  # 软件根目录/助手名(onefile 布局)
    if os.path.isfile(path):  # 文件存在说明是 onefile 打包
        return path  # 返回 onefile 布局的助手路径
    # onedir 模式:update/ 子目录
    onedir_path = os.path.join(APP_BASE_DIR, "update", UPDATER_EXE_NAME)  # 软件根目录/update/助手名(onedir 布局)
    if os.path.isfile(onedir_path):  # 文件存在说明是 onedir 打包
        return onedir_path  # 返回 onedir 布局的助手路径
    return None  # 所有布局都没找到,判定助手未随包发布


def is_update_assistant_available() -> bool:
    """更新助手是否随主程序一起发布在同目录
    :return: True=存在可用助手;False=源码模式或文件缺失
    :rtype: bool
    """
    return bool(get_updater_path())  # 路径非空即视为可用


def get_task_file_path() -> str:
    """任务JSON固定放在数据目录(已被.gitignore,且更新助手能通过指针/环境变量找到)
    :return: _update_task.json 的绝对路径
    :rtype: str
    """
    return os.path.join(get_data_store_root(), TASK_FILE_NAME)  # 数据根目录 + 任务文件名


def write_update_task(
    local_version: str,
    remote_version: str,
) -> Tuple[bool, str, str]:
    """主程序调用:生成更新任务JSON

    :param local_version: 当前本地版本号
    :type local_version: str
    :param remote_version: 待升级的远端版本号
    :type remote_version: str
    :return: (ok, task_path, msg);成功 msg 为 "ok",失败含错误描述
    :rtype: Tuple[bool, str, str]
    :raises OSError: 内部捕获(目录创建/文件写入失败)并转失败三元组
    """
    task_path = get_task_file_path()  # 任务 JSON 目标路径
    task = {  # 跨进程交接的全部上下文(助手无法读主程序内存)
        "app_exe": os.path.abspath(sys.executable),  # 主程序可执行文件绝对路径(冻结后即 exe)
        "app_dir": APP_BASE_DIR,  # 软件安装根目录,替换文件的目标目录
        "main_exe_name": os.path.basename(os.path.abspath(sys.executable)),  # 主程序文件名,助手按名等待进程退出
        "updater_exe": get_updater_path() or "",  # 助手自身路径(自更新/重启时需要)
        "old_pid": os.getpid(),  # 当前主程序 PID,助手精准等待其退出释放文件锁
        "local_version": local_version,  # 升级前版本
        "remote_version": remote_version,  # 目标版本
        "data_store_root": get_data_store_root(),  # 数据目录根,助手回读任务/日志输出位置
        "config_root": get_active_config_root(),  # 主程序当前实际配置目录,保证助手准确回填配置
        "manifest_filename": CONFIG_MANIFEST_FILENAME,  # 更新助手从该清单获取真实模块文件名
    }
    try:  # 建目录与写文件均可能失败
        os.makedirs(os.path.dirname(task_path), exist_ok=True)  # 确保数据目录存在
        with open(task_path, "w", encoding="utf-8") as f:  # UTF-8 覆盖写任务文件
            json.dump(task, f, ensure_ascii=False, indent=2)  # 中文不转义、缩进2格,便于排查
        _logger.info(f"📝 更新任务已写入:{task_path} | {local_version} -> {remote_version}")  # 留痕版本链路
        return True, task_path, "ok"  # 成功三元组
    except OSError as e:  # 磁盘/权限/路径异常
        _logger.error(f"❌ 写入更新任务失败:{e}", exc_info=True)  # error 级并带堆栈
        return False, task_path, f"写入更新任务失败:{e}"  # 失败三元组,UI 可提示用户


def launch_update_assistant(task_path: str = "") -> Tuple[bool, str]:
    """主程序调用:以GUI方式启动更新助手(等同资源管理器双击,不产生控制台窗口)

    :param task_path: 可选任务JSON路径.
        - 传空串(推荐,链路完全内聚在助手自身):助手无参自举,自行定位软件目录/
          数据目录/主程序/配置,并在安装阶段按主程序进程名等待其退出;
        - 传路径:主程序移交模式,助手按任务中的精准old_pid等待(向后兼容).
    :type task_path: str
    :return: (ok, msg);成功 msg 为 "ok",失败含错误码/异常描述
    :rtype: Tuple[bool, str]
    :raises Exception: 内部捕获启动期全部异常并转失败二元组
    """
    updater_exe = get_updater_path()  # 再次定位助手(必须打包态且文件存在)
    if not updater_exe:  # 源码模式或助手缺失
        return False, "更新助手不存在,请重新下载完整版软件包"  # 给出可操作提示
    params = f'"{task_path}"' if task_path else None  # Windows 命令行参数需带引号防路径空格;自举模式传 None
    try:  # 启动进程可能抛异常
        if sys.platform.startswith("win"):  # Windows:走 ShellExecute 无黑窗方案
            import ctypes  # 运行时导入:仅 Windows 需要,避免类 Unix 平台无此库

            # ShellExecuteW走shell启动windowed程序,无控制台黑窗;返回值>32表示成功
            rc = ctypes.windll.shell32.ShellExecuteW(  # 宽字符版 Shell API
                None, "open", updater_exe, params, APP_BASE_DIR, 1  # 父窗口无;动词open;目标exe;参数;工作目录=软件根;1=SW_SHOWNORMAL
            )
            if rc <= 32:  # ShellExecute 约定:返回值不大于32为错误码(如文件不存在=2,关联失败=31)
                return False, f"启动更新助手失败(错误码{rc})"  # 失败并回传 Windows 错误码
        else:  # 类 Unix 平台(开发/测试用)
            cmd = [updater_exe]  # 命令以助手路径开头
            if task_path:  # 移交模式才附加任务路径
                cmd.append(task_path)  # 追加任务 JSON 路径参数
            subprocess.Popen(  # 直接 fork 子进程
                cmd,  # 命令列表
                cwd=APP_BASE_DIR,  # 工作目录锁定软件根
                close_fds=True,  # 关闭继承的文件描述符
                start_new_session=True,  # 新会话脱离父进程,主程序退出不带走助手
                stdin=subprocess.DEVNULL,  # 标准输入丢弃
                stdout=subprocess.DEVNULL,  # 标准输出丢弃
                stderr=subprocess.DEVNULL,  # 标准错误丢弃
            )
        _logger.info(f"🚀 更新助手已启动:{updater_exe} {task_path or '(自举模式)'}")  # 留痕启动方式
        return True, "ok"  # 成功二元组
    except Exception as e:  # ctypes/Popen 等任意异常
        _logger.error(f"❌ 启动更新助手异常:{e}", exc_info=True)  # error 级带堆栈
        return False, f"启动更新助手异常:{e}"  # 失败二元组


def apply_pending_and_cleanup(logger: Optional[logging.Logger] = None) -> List[str]:
    """主程序每次启动最早期调用(纯文件操作,不阻塞启动)

    1. 更新助手上轮自我更新:update.exe.new_pending -> update.exe(助手此刻未运行)
    2. 归档更新助手日志到app.log
    3. 清理残留:*.old_del / _update_stage / 旧bat/VBS脚本
    任何异常都只警告不抛出.
    :param logger: 可选外部 logger(启动早期可能用临时 logger);缺省用模块 logger
    :type logger: Optional[logging.Logger]
    :return: 实际处理的残留文件名列表(供启动日志/排查)
    :rtype: List[str]
    """
    log = logger or _logger  # 允许调用方注入启动期 logger
    handled: List[str] = []  # 本轮实际处理掉的残留名
    try:  # 整个清理过程绝不能阻断主程序启动
        # 1. 更新助手延迟替换(助手运行时无法覆盖自己及 lib/ 依赖,
        #    上轮把新版存为 .new_pending,主程序启动时递归替换)
        updater_path = get_updater_path()  # 当前助手路径(打包态才可能有 pending)
        if updater_path:  # 助手存在才检查自更新
            # 1a. 替换助手可执行文件本体: update.exe.new_pending -> update.exe
            pending_path = updater_path + PENDING_SUFFIX  # 新版暂存路径 update.exe.new_pending
            if os.path.isfile(pending_path):  # 存在待替换新版
                try:  # replace 在杀软占用时可能失败
                    os.replace(pending_path, updater_path)  # 原子替换(同卷 rename 覆盖),此刻助手未运行
                    handled.append(os.path.basename(updater_path) + PENDING_SUFFIX)  # 记录已处理文件名
                    log.info("🔄 更新助手已完成自我升级")  # 留痕
                except OSError as e:  # 文件被占用等
                    log.warning(f"⚠ 更新助手暂未替换成功(可能正在运行),下次启动再试:{e}")  # 下轮启动重试

            # 1b. 递归替换助手目录下 lib/ 中的 .new_pending 依赖文件
            # onedir 布局: update/lib/*.dll.new_pending -> update/lib/*.dll
            updater_dir = os.path.dirname(updater_path)  # 助手所在目录(onefile=根目录,onedir=update/)
            pending_count = 0  # 本次替换的依赖文件计数
            for root, _dirs, files in os.walk(updater_dir):  # 递归遍历助手目录
                for name in files:  # 遍历每个文件
                    if not name.endswith(PENDING_SUFFIX):  # 不是 pending 文件则跳过
                        continue
                    # 构造 pending 文件路径(源)和目标路径(去掉 .new_pending 后缀)
                    pending_file = os.path.join(root, name)  # 完整 pending 路径
                    target_file = pending_file[:-len(PENDING_SUFFIX)]  # 去掉后缀得到目标路径
                    try:  # 替换可能失败(文件被占用)
                        os.replace(pending_file, target_file)  # 原子替换
                        pending_count += 1  # 计数加一
                    except OSError as e:  # 文件被占用等
                        log.warning(f"⚠ 助手依赖暂未替换成功:{name} | {e}")  # 告警,下轮再试
            if pending_count > 0:  # 有依赖文件被替换
                handled.append(f"lib/*{PENDING_SUFFIX}x{pending_count}")  # 记录lib依赖替换汇总
                log.info(f"🔄 更新助手依赖已替换({pending_count}个文件)")  # 留痕

        # 2. 归档更新助手日志
        updater_log = os.path.join(APP_BASE_DIR, UPDATER_LOG_NAME)  # 助手独立运行期写在软件根的日志
        if os.path.isfile(updater_log):  # 有日志才回收
            try:  # 读取可能被占用/编码异常
                with open(updater_log, "r", encoding="utf-8", errors="replace") as f:  # UTF-8 读,坏字符替换不抛
                    for line in f.read().splitlines():  # 按行归档
                        line = line.strip()  # 去空白
                        if line:  # 跳过空行
                            log.info(f"[更新助手] {line}")  # 加前缀并入主程序 app.log
            except OSError:  # 读失败不影响后续删除尝试
                pass  # 静默忽略读取异常
            try:  # 归档后删除原文件
                os.remove(updater_log)  # 删除助手日志
                handled.append(UPDATER_LOG_NAME)  # 记录
            except OSError:  # 占用/权限
                pass  # 下轮启动再删

        # 3. 旧主程序备份与stage目录(安装成功后通常已被助手删除,此处为崩溃兜底)
        for name in os.listdir(APP_BASE_DIR):  # 遍历软件根全部条目
            if name.endswith(OLD_EXE_SUFFIX):  # 命中 *.old_del 旧程序备份
                path = os.path.join(APP_BASE_DIR, name)  # 拼完整路径
                try:  # 删除可能失败
                    if os.path.isdir(path):  # macOS 上 .app.old_del 是目录,需递归删除
                        shutil.rmtree(path, ignore_errors=True)
                    else:  # Windows/Linux 上是单文件备份
                        os.remove(path)  # 删除旧备份
                    handled.append(name)  # 记录
                    log.info(f"🧹 清理旧程序备份:{name}")  # 留痕
                except OSError as e:  # 占用/权限
                    log.warning(f"⚠ 旧程序备份清理失败:{name} | {e}")  # 告警,下轮再试
        stage_dir = os.path.join(APP_BASE_DIR, STAGE_DIR_NAME)  # 安装暂存目录路径
        if os.path.isdir(stage_dir):  # 残留暂存目录存在(说明上轮异常中断)
            shutil.rmtree(stage_dir, ignore_errors=True)  # 递归删除,内部错误忽略
            handled.append(STAGE_DIR_NAME)  # 记录
        for name in LEGACY_FILES:  # 遍历旧版脚本遗留物清单
            path = os.path.join(APP_BASE_DIR, name)  # 拼路径
            if os.path.isfile(path):  # 存在才删
                try:  # 删除兜底
                    os.remove(path)  # 删除旧 bat/sh/vbs/日志
                    handled.append(name)  # 记录
                except OSError:  # 占用等
                    pass  # 静默,下轮再试
        # 4. 残留任务文件(更新助手异常退出未清理)
        task_path = get_task_file_path()  # 数据目录中的任务 JSON
        if os.path.isfile(task_path):  # 上轮任务残留
            try:  # 删除兜底
                os.remove(task_path)  # 删除过期任务,防本轮更新误读旧参数
            except OSError:  # 占用/权限
                pass  # 静默忽略
    except Exception as e:  # 目录无法列举等意料外异常的总兜底
        log.warning(f"⚠ 更新残留清理异常(不影响启动):{e}", exc_info=True)  # 告警但不抛出,保证启动继续
    return handled  # 返回本轮已处理残留清单


__all__ = [  # 模块公开 API 白名单
    "UPDATER_EXE_NAME",  # 助手可执行文件名常量
    "STAGE_DIR_NAME",  # 暂存目录名常量
    "OLD_EXE_SUFFIX",  # 旧程序备份后缀常量
    "PENDING_SUFFIX",  # 助手自更新暂存后缀常量
    "TASK_FILE_NAME",  # 任务 JSON 文件名常量
    "UPDATER_LOG_NAME",  # 助手日志文件名常量
    "get_updater_path",  # 定位助手可执行文件
    "is_update_assistant_available",  # 助手是否可用
    "get_task_file_path",  # 任务 JSON 路径
    "write_update_task",  # 生成更新任务
    "launch_update_assistant",  # 启动更新助手
    "apply_pending_and_cleanup",  # 启动期自替换与残留清理
]
