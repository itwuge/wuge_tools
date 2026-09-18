# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: bootstrap.py
# 归属: updater_app 更新助手包 —— 启动引导层(Bootstrap Layer)
# ------------------------------------------------------------------------------
# 文件用途:
#   更新助手"自给自足"启动解析器[纯标准库实现,可在 import config_manager 之前运行].
#   负责在程序最早期解析命令行参数、定位软件目录、数据目录、主程序路径,
#   并通过环境变量 SERVICE_DATA_DIR 注入数据目录路径,确保后续配置模块
#   与主程序使用同一路径口径.
# ------------------------------------------------------------------------------
# 架构定位:
#   位于 MVC 架构之下的基础设施层,是整个更新助手的"第零阶段".
#   必须在任何业务模块(Model/View/Controller)导入之前执行,
#   因为 config_manager 等模块在 import 期就会固化数据目录路径.
#
#   调用关系: updater_main.py → early_bootstrap() → 注入环境变量 → import config_manager
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 支持两种启动模式:
#      - 独立模式(无参/参数无效): 助手自行推断所有路径信息
#      - 任务模式(首参为任务JSON): 主程序移交,任务字段优先
#   2. 软件目录检测(detect_app_dir)
#   3. 数据目录指针文件读取(_read_data_pointer)
#   4. 数据目录根路径解析(detect_data_store_root)
#   5. 主程序可执行文件定位(detect_main_exe)
#   6. 独立模式默认任务构造(build_self_task)
#   7. 任务 JSON 文件加载(_load_task_file)
#   8. 最早期引导入口(early_bootstrap)
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅使用 Python 标准库,不依赖 PySide6、不依赖任何业务模块
#   - 不创建任何 UI 控件、不启动任何线程
#   - 只做路径解析和任务构造,不执行业务逻辑
#   - 数据目录指针读取口径必须与主程序 runtime.model.app_config 保持一致
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块所有函数均为纯函数式调用,无状态、无全局变量(除常量),
#   在主线程中同步执行,不涉及多线程操作.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: json, os, sys, typing
#   - 第三方: 无
#   - 项目内: 无(纯标准库,保证最早可执行)
# ==============================================================================

# 导入 JSON 解析库,用于读取任务 JSON 文件和数据目录指针文件
import json
# 导入操作系统接口库,用于路径操作、文件检测、环境变量读写
import os
# 导入系统相关库,用于获取命令行参数、判断是否为 frozen 打包状态
import sys
# 导入类型提示库,用于函数签名的类型注解
from typing import Any, Dict, Tuple

# ------------------------------------------------------------------------------
# 全局常量定义
# ------------------------------------------------------------------------------

# 主程序可执行文件名(优先精确匹配;找不到时 detect_main_exe 自动扫描目录排除 update.exe)
# 项目复用时可在任务JSON的 main_exe_name 字段指定,或直接由自动扫描兜底
# Windows 平台下后缀为 .exe,其他平台为无扩展名的可执行文件
MAIN_EXE_NAME = "个人小工具.exe" if sys.platform.startswith("win") else "个人小工具"

# 更新助手自身可执行文件名(与 runtime.model.update_launcher.UPDATER_EXE_NAME 一致)
# 用于在扫描目录时排除自身,避免把更新助手当主程序
UPDATER_EXE_NAME = "update.exe" if sys.platform.startswith("win") else "update"

# 数据目录指针文件名(与 app_config.DATA_ROOT_POINTER_FILE 同名同结构)
# 该文件存放在软件目录下,指向实际的数据存储目录路径
# 前缀下划线表示为模块内部私有常量
_DATA_POINTER_FILE = ".data_dir_pointer.json"


# ==============================================================================
# 函数: detect_app_dir
# ==============================================================================
def detect_app_dir() -> str:
    """
    检测并返回软件安装目录的绝对路径.

    详细说明:
        根据运行环境(frozen 打包 / 源码运行)自动推断软件目录.
        支持三种打包布局:
        - Windows/Linux onedir: update.exe 位于软件目录下的 update/ 子目录中,回退一级
        - Windows/Linux onefile: update.exe 直接位于软件目录
        - macOS: update.app/Contents/MacOS/update,需回退三级到 .app 所在目录

    参数:
        无

    返回值:
        str: 软件安装目录的绝对路径字符串

    异常:
        无显式抛出异常;os.path 操作在极端情况下可能抛出 OSError,但正常环境不会发生.

    示例:
        >>> detect_app_dir()
        'C:\\Program Files\\WugeTools'
    """
    # 判断是否为 PyInstaller 等工具打包的 frozen 可执行文件
    if getattr(sys, "frozen", False):
        # frozen 模式下: sys.executable 是可执行文件的完整路径
        exe_path = os.path.abspath(sys.executable)
        # macOS: 可执行文件位于 xxx.app/Contents/MacOS/ 下,需回退到软件目录
        # 例如: /App/个人小工具.app/Contents/MacOS/update -> /App
        # 注意: update.app 可能嵌入主程序 .app 包内(Contents/Resources/update.app),
        #       此时路径含两个 .app,必须取最外层(最靠前)的 .app 的父目录
        if sys.platform == "darwin":
            # 逐级拆分路径
            parts = exe_path.split(os.sep)
            # 收集所有以 .app 结尾的目录索引(从前往后即从外到内)
            app_indices = [i for i, p in enumerate(parts) if p.endswith(".app")]
            if app_indices:
                # 取最外层(第一个)的 .app,其父目录即为软件安装目录
                outermost = app_indices[0]
                if outermost > 0:
                    return os.sep.join(parts[:outermost])
            # 兜底:回退三级(MacOS -> Contents -> xxx.app -> 上级)
            return os.path.dirname(os.path.dirname(os.path.dirname(exe_path)))
        # 非 macOS: 获取可执行文件所在的目录
        exe_dir = os.path.dirname(exe_path)
        # 检查当前目录名是否为 "update"(onedir 模式下 update 在 update/ 子目录中)
        if os.path.basename(exe_dir).lower() == "update":
            # 获取上一级目录(即软件主目录)
            parent = os.path.dirname(exe_dir)
            # 确保父目录存在(防止根目录再往上取导致空串)
            if parent:
                # 返回软件主目录
                return parent
        # 如果不在 update 子目录中,直接返回 exe 所在目录
        return exe_dir
    # 源码运行模式下: 本文件在 updater_app/ 目录下,上两级是项目根目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==============================================================================
# 函数: _read_data_pointer
# ==============================================================================
def _read_data_pointer(app_dir: str) -> str:
    """
    读取程序目录的数据目录指针文件(标准库复刻 app_config 同口径).

    详细说明:
        从软件目录下的 .data_dir_pointer.json 文件中读取数据目录路径.
        支持绝对路径和相对路径(相对路径以 app_dir 为基准).
        文件损坏、缺失或内容为空时返回空字符串.
        这是模块内部私有函数,以下划线开头命名.

    参数:
        app_dir (str): 软件安装目录的绝对路径,用于解析相对路径

    返回值:
        str: 数据目录的绝对路径;指针文件不存在/损坏/为空时返回空字符串

    异常:
        不向外抛出异常,所有异常都被捕获并返回空串
        可能捕获的异常: OSError(文件读写错误)、json.JSONDecodeError(JSON格式错误)、
        ValueError(数据类型错误)
    """
    # 拼接指针文件的完整路径
    # macOS: 主程序指针文件位于 Application Support 下(程序目录通常无写权限)
    # Windows 安装版: %LOCALAPPDATA%/wuge_tools 下(Program Files 只读)
    # 其他平台: 软件目录 + 指针文件名
    if sys.platform == "darwin":
        pointer_path = os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", 
            "wuge_tools", _DATA_POINTER_FILE)
    elif sys.platform.startswith("win") and os.path.isfile(os.path.join(app_dir, "installed.flag")):
        pointer_path = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), 
            "wuge_tools", _DATA_POINTER_FILE)
    else:
        pointer_path = os.path.join(app_dir, _DATA_POINTER_FILE)
    try:
        # 以只读模式、UTF-8 编码打开指针文件
        with open(pointer_path, "r", encoding="utf-8") as f:
            # 解析 JSON 内容为字典
            data = json.load(f)
        # 从字典中提取 data_dir 字段,去除首尾空白和可能的引号
        pointed = str(data.get("data_dir", "")).strip().strip('"')
        # 如果路径为空,返回空字符串
        if not pointed:
            return ""
        # 展开用户目录(如 ~/xxx 展开为 /home/user/xxx)
        pointed = os.path.expanduser(pointed)
        # 判断是否为绝对路径
        if os.path.isabs(pointed):
            # 绝对路径直接规范化后返回
            return os.path.normpath(pointed)
        # 相对路径以 app_dir 为基准拼接后规范化返回
        return os.path.normpath(os.path.join(app_dir, pointed))
    # 捕获所有可能的文件读取/解析异常,返回空串
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


# ==============================================================================
# 函数: detect_data_store_root
# ==============================================================================
def detect_data_store_root(app_dir: str, explicit: str = "") -> str:
    """
    解析数据存储根目录路径,优先级从高到低依次为:
    显式传入 > SERVICE_DATA_DIR 环境变量 > 指针文件 > 出厂默认 app_dir/data_store.

    详细说明:
        按照四级优先级策略确定数据存储目录的绝对路径,
        确保与主程序 runtime.model.app_config._resolve_data_store_root 的解析口径完全一致.
        数据目录用于存放配置文件、下载文件、日志等用户数据.

    参数:
        app_dir (str): 软件安装目录的绝对路径,作为相对路径的基准
        explicit (str): 显式指定的数据目录路径,优先级最高;默认为空串

    返回值:
        str: 数据存储根目录的绝对路径字符串

    异常:
        无显式抛出异常
    """
    # 第一优先级: 显式传入的路径(转字符串并去除首尾空白)
    explicit = str(explicit or "").strip()
    # 如果显式路径非空,转为绝对路径后返回
    if explicit:
        return os.path.abspath(explicit)
    # 第二优先级: 从环境变量 SERVICE_DATA_DIR 读取
    env_dir = os.environ.get("SERVICE_DATA_DIR", "").strip()
    # 如果环境变量非空,转为绝对路径后返回
    if env_dir:
        return os.path.abspath(env_dir)
    # 第三优先级: 读取数据目录指针文件
    pointed = _read_data_pointer(app_dir)
    # 如果指针文件返回了有效路径,直接返回(内部已转绝对路径)
    if pointed:
        return pointed
    # 第四优先级(兜底): 出厂默认数据目录
    if sys.platform == "darwin":
        # macOS: 遵循系统惯例,数据放 ~/Library/Application Support/wuge_tools
        # (程序包目录只读,且更新时整包替换,数据不能放程序目录)
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "wuge_tools")
    if sys.platform.startswith("win") and os.path.isfile(os.path.join(app_dir, "installed.flag")):
        # Windows 安装版: 程序目录(Program Files)只读,数据放 %LOCALAPPDATA%/wuge_tools
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "wuge_tools")
    # 其他平台/绿色版: 使用软件目录下的 data_store 子目录作为出厂默认
    return os.path.join(app_dir, "data_store")


# ==============================================================================
# 函数: detect_main_exe
# ==============================================================================
def detect_main_exe(app_dir: str, main_exe_name: str = "") -> str:
    """
    在软件目录中定位主程序可执行文件.

    详细说明:
        优先按指定/默认主程序名精确匹配;找不到时自动扫描目录:
        - Windows: 取除 update.exe 外的第一个 .exe 文件
        - Linux: 取除 update 外的第一个无扩展名可执行文件
        - macOS: 取除 update.app 外的第一个 .app 包,返回其 Contents/MacOS/ 下的可执行文件
        自动扫描使更新助手不依赖硬编码的主程序名,可复用于任意项目.
        文件不存在时返回空串,由安装阶段的前置校验拦截报错,
        确保不会误写其他目录的文件.

    参数:
        app_dir (str): 软件安装目录的绝对路径,搜索范围
        main_exe_name (str): 优先匹配的主程序文件名;为空时使用默认 MAIN_EXE_NAME

    返回值:
        str: 主程序可执行文件的绝对路径;未找到时返回空字符串

    异常:
        不向外抛出异常;目录列表失败时返回空串
    """
    # 确定要匹配的主程序名: 传入值优先,否则使用默认常量
    name = main_exe_name or MAIN_EXE_NAME

    # macOS: 主程序是 .app 包,可执行文件在 Contents/MacOS/ 下
    if sys.platform == "darwin":
        # 优先按 .app 包名精确匹配(如 个人小工具.app)
        app_bundle = os.path.join(app_dir, name + ".app")
        if os.path.isdir(app_bundle):  # .app 包目录存在
            # 拼接包内可执行文件路径: xxx.app/Contents/MacOS/xxx
            macos_exe = os.path.join(app_bundle, "Contents", "MacOS", name)
            if os.path.isfile(macos_exe):  # 可执行文件存在
                return macos_exe  # 返回 .app 包内的可执行文件路径
        # 精确匹配失败,启动自动扫描兜底逻辑
        try:
            entries = os.listdir(app_dir)  # 列出软件目录下的所有条目
        except OSError:
            return ""  # 目录读取失败,返回空串
        skip = {"update.app", "Update.app"}  # 跳过更新助手自身的 .app 包
        for fname in entries:
            if fname in skip:  # 跳过更新助手
                continue
            if fname.endswith(".app"):  # 找到第一个非 update 的 .app 包
                app_bundle = os.path.join(app_dir, fname)
                if os.path.isdir(app_bundle):
                    # 可执行文件名通常与 .app 包名(去掉 .app)一致
                    exe_name = fname[:-4]  # 去掉 .app 后缀得到可执行文件名
                    macos_exe = os.path.join(app_bundle, "Contents", "MacOS", exe_name)
                    if os.path.isfile(macos_exe):
                        return macos_exe
        return ""  # 未找到任何 .app 包

    # 非 macOS 平台:按文件名匹配
    candidate = os.path.join(app_dir, name)  # 拼接候选主程序的完整路径
    if os.path.isfile(candidate):  # 检查该文件是否存在且为普通文件
        return candidate  # 找到精确匹配,直接返回

    # 精确匹配失败,启动自动扫描兜底逻辑
    # 排除更新助手自身,取目录下第一个候选可执行文件
    try:
        entries = os.listdir(app_dir)  # 列出软件目录下的所有文件和子目录名
    except OSError:
        return ""  # 目录读取失败(权限不足等),返回空串

    # 构建需要跳过的文件名集合(更新助手自身的各种可能名称)
    skip = {UPDATER_EXE_NAME, "update"}
    # 根据平台确定可执行文件扩展名(Windows 为 .exe,其他为空)
    exe_ext = ".exe" if sys.platform.startswith("win") else ""

    # 遍历目录条目,寻找符合条件的可执行文件
    for fname in entries:
        if fname in skip or fname.lower() in {s.lower() for s in skip}:  # 跳过更新助手自身
            continue
        fp = os.path.join(app_dir, fname)  # 拼接完整路径
        if not os.path.isfile(fp):  # 只考虑普通文件,跳过子目录
            continue
        if exe_ext:  # Windows 平台: 检查文件名是否以 .exe 结尾
            if fname.lower().endswith(exe_ext):
                return fp
        else:  # Linux 平台: 取第一个无扩展名的可执行文件(排除隐藏文件)
            if not fname.startswith(".") and "." not in fname:
                return fp
    return ""  # 扫描完所有条目仍未找到,返回空串


# ==============================================================================
# 函数: build_self_task
# ==============================================================================
def build_self_task(app_dir: str) -> Dict[str, Any]:
    """
    构造独立模式(自给自足)的默认任务字典.

    详细说明:
        当更新助手以独立模式启动(无任务 JSON 参数)时,自行构造任务信息.
        config/local_version/remote_version 刻意留空:
        Model 层具备读盘能力后从 data_store 配置自行加载,
        保证"磁盘配置"是唯一事实来源,避免与主程序配置不一致.

    参数:
        app_dir (str): 软件安装目录的绝对路径

    返回值:
        Dict[str, Any]: 任务字典,包含以下字段:
            - app_exe: 主程序可执行文件路径
            - app_dir: 软件目录路径
            - updater_exe: 更新助手自身可执行文件路径
            - old_pid: 旧主程序 PID(独立模式为 0)
            - local_version: 本地版本号(留空,Model 从磁盘读取)
            - remote_version: 远程版本号(留空,检测后回填)
            - data_store_root: 数据存储根目录
            - config: 配置快照(留空,Model 从磁盘读取)
            - launch_mode: 启动模式,固定为 "standalone"

    异常:
        无显式抛出异常
    """
    # 解析数据存储根目录路径
    data_root = detect_data_store_root(app_dir)
    # frozen 时 sys.executable 即更新助手自身(onedir 下为 update/update.exe)
    # 构造更新助手自身的可执行文件路径
    self_exe = (
        # frozen 模式: 使用 sys.executable(即当前运行的 exe 路径)
        os.path.abspath(sys.executable)
        if getattr(sys, "frozen", False)
        # 源码模式: 假设软件目录下存在 update 可执行文件
        else os.path.join(app_dir, UPDATER_EXE_NAME)
    )
    # 返回构造好的任务字典
    return {
        # 主程序可执行文件路径(自动检测)
        "app_exe": detect_main_exe(app_dir),
        # 软件安装目录
        "app_dir": app_dir,
        # 更新助手自身可执行文件路径
        "updater_exe": self_exe,
        # 旧主程序 PID(独立模式为 0,安装时按进程名等待)
        "old_pid": 0,
        # 本地版本号(留空,Model 层从 version.json 读取)
        "local_version": "",
        # 远程版本号(留空,检测后由 Controller 回填)
        "remote_version": "",
        # 数据存储根目录
        "data_store_root": data_root,
        # 实际配置目录与版本配置文件名,独立模式采用标准布局
        "config_root": os.path.join(data_root, "config"),
        "manifest_filename": "config_manifest.json",  # 配置模块定位清单文件名
        # 配置快照仅为旧任务兼容保留;新流程直接按config_files从磁盘读取
        "config": {},
        # 标记来源: 独立双击启动,取消/失败时不替用户拉起主程序
        "launch_mode": "standalone",
    }


# ==============================================================================
# 函数: _load_task_file
# ==============================================================================
def _load_task_file(task_path: str) -> Dict[str, Any]:
    """
    读取主程序写入的任务 JSON 文件.

    详细说明:
        从指定路径读取任务 JSON 文件并解析为字典.
        任何异常(文件不存在、格式错误、权限不足等)都返回空字典,
        调用方会根据空字典判断降级为独立模式.
        这是模块内部私有函数,以下划线开头命名.

    参数:
        task_path (str): 任务 JSON 文件的路径

    返回值:
        Dict[str, Any]: 解析后的任务字典;读取失败或内容非字典时返回空字典

    异常:
        不向外抛出异常
        可能捕获的异常: OSError(文件读写错误)、json.JSONDecodeError(JSON格式错误)、
        ValueError(数据类型错误)
    """
    try:
        # 以只读模式、UTF-8 编码打开任务文件
        with open(task_path, "r", encoding="utf-8") as f:
            # 解析 JSON 内容
            data = json.load(f)
        # 确保返回值为字典类型,不是字典则返回空字典
        return data if isinstance(data, dict) else {}
    # 捕获所有可能的文件读取/解析异常,返回空字典
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


# ==============================================================================
# 函数: early_bootstrap
# ==============================================================================
def early_bootstrap(argv: Tuple[str, ...] = ()) -> Tuple[Dict[str, Any], str, str]:
    """
    进程最早期入口函数,必须在 import config_manager 之前调用.

    详细说明:
        这是更新助手的"第零步"操作:
        1. 解析命令行参数,判断是任务模式还是独立模式
        2. 任务模式: 读取任务 JSON,以任务字段为准,缺失时按自身位置兜底
        3. 独立模式: 全部自行推断(软件目录、数据目录、主程序等)
        4. 将解析出的数据目录写入 SERVICE_DATA_DIR 环境变量,
           使后续 config_manager 导入时使用与主程序一致的路径

        此函数必须在任何 config_manager 导入之前调用,
        因为 config_manager 在 import 期就会固化数据目录路径.

    参数:
        argv (Tuple[str, ...]): 命令行参数元组(不含解释器/脚本名),
            首参可为任务 JSON 文件路径;默认为空元组

    返回值:
        Tuple[Dict[str, Any], str, str]: 三元组,包含:
            - task: 任务字典(任务模式来自 JSON,独立模式为自举构造)
            - task_path: 任务 JSON 文件路径(独立模式为空串)
            - mode: 启动模式,"task"=主程序移交,"standalone"=自给自足

    异常:
        无显式抛出异常;所有错误都通过降级为独立模式处理
    """
    # 获取命令行第一个参数作为任务文件路径(去除首尾空白)
    task_path = str(argv[0]).strip() if argv else ""
    # 如果任务路径非空且文件存在,则加载任务 JSON;否则任务字典为空
    task = _load_task_file(task_path) if task_path and os.path.isfile(task_path) else {}

    # 判断是否成功加载了任务(任务字典非空)
    if task:
        # ==================== 任务模式: 主程序移交 ====================
        # 任务 JSON 优先,app_dir 缺失时按自身位置兜底
        # 从任务字典获取 app_dir,为空则自动检测
        app_dir = str(task.get("app_dir", "")).strip() or detect_app_dir()
        # 转为绝对路径后写回任务字典
        task["app_dir"] = os.path.abspath(app_dir)
        # 如果任务中没有 app_exe,则自动检测主程序路径
        if not str(task.get("app_exe", "")).strip():
            task["app_exe"] = detect_main_exe(task["app_dir"])
        # 解析数据存储根目录(任务中显式指定 > 环境变量 > 指针文件 > 默认)
        data_root = detect_data_store_root(task["app_dir"], str(task.get("data_store_root", "")))
        # 写回任务字典
        task["data_store_root"] = data_root
        # 规范化实际配置目录:任务显式值优先,缺失时回退数据目录/config
        config_root = str(task.get("config_root", "")).strip()
        task["config_root"] = os.path.abspath(config_root) if config_root \
            else os.path.join(data_root, "config")
        # 新任务只传配置清单文件名;更新助手从清单读取实际模块文件名
        manifest_filename = os.path.basename(
            str(task.get("manifest_filename", "config_manifest.json")).strip()
        )
        task["manifest_filename"] = manifest_filename or "config_manifest.json"
        # 保留旧任务config_files/version_config_filename供config_manager兼容读取
        # 标记启动模式为任务模式
        task["launch_mode"] = "task"
        mode = "task"
    else:
        # ==================== 独立模式: 全部自行推断 ====================
        # 自动检测软件目录
        app_dir = detect_app_dir()
        # 构造独立模式的默认任务字典
        task = build_self_task(app_dir)
        # 独立模式无任务文件路径
        task_path = ""
        # 从任务字典中获取数据根目录
        data_root = task["data_store_root"]
        # 独立模式按标准布局补齐实际配置目录与版本配置文件名
        task["config_root"] = os.path.join(data_root, "config")
        task["manifest_filename"] = "config_manifest.json"
        # 标记启动模式为独立模式
        mode = "standalone"

    # 必须在 import config_manager 之前注入环境变量,
    # config_manager 在 import 期就会固化数据目录路径
    os.environ["SERVICE_DATA_DIR"] = data_root
    os.environ["SERVICE_CONFIG_DIR"] = task["config_root"]  # 注入主程序实际配置目录
    os.environ["SERVICE_CONFIG_MANIFEST_FILENAME"] = task["manifest_filename"]  # 注入配置清单文件名
    # 旧任务的config_files/version_config_filename仍由config_manager直接兼容环境变量缺失场景
    # 返回任务字典、任务文件路径、启动模式
    return task, task_path, mode
