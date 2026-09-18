# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: config_manager.py
# 归属: updater_app/model —— 更新助手配置管理模块(Config Manager)
# ------------------------------------------------------------------------------
# 文件用途:
#   更新助手专用配置管理模块(精简版,不依赖主程序 runtime 层).
#   从 comm_tools.app_config 提取更新助手所需的配置 IO/版本/代理/下载功能,
#   路径管理由 updater_app.bootstrap 通过 SERVICE_DATA_DIR 环境变量注入,
#   因此本模块只需读取环境变量确定数据目录,无需完整的三级路径解析.
#   与主程序 runtime.model.app_config 功能对齐但更精简,
#   保证更新助手独立打包运行.
# ------------------------------------------------------------------------------
# 架构定位:
#   Model 层的基础设施模块,提供配置文件的读写和版本/代理/下载相关的工具函数.
#   被 UpdaterModel 调用,为整个更新助手提供配置数据的持久化能力.
#
#   关联组件:
#     - 上游: UpdaterModel(数据模型,调用本模块读写配置)
#     - 下游: 数据目录下的 JSON 配置文件(version.json、github.json、proxy.json 等)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 配置模块映射(MODULE_FILE_MAP)和内置默认值(BUILTIN_DEFAULTS)
#   2. 数据目录/配置目录路径解析
#   3. 原子写入 JSON(防止写入中断导致文件损坏)
#   4. 配置模块读写(_read_module_json / _write_module_json)
#   5. 全量配置加载与合并(load_and_merge_all_config)
#   6. 各模块便捷读写函数(load_user_info / save_github_config 等)
#   7. 版本号比较(semver_compare)
#   8. 更新下载目录/文件名模板/Release 页面 URL 渲染
#   9. 代理解析(resolve_effective_proxy)
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只负责配置文件的读写和相关工具函数
#   - 不涉及 UI、不涉及网络、不涉及业务流程
#   - 配置文件使用线程锁保护,保证多线程安全
# ------------------------------------------------------------------------------
# 线程模型:
#   配置文件的写入操作使用全局 _IO_LOCK 线程锁保护,
#   防止多线程同时写入导致文件损坏.
#   读取操作不加锁(JSON 文件读取是原子的,最坏情况读到旧版本).
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: json, logging, os, sys, threading, typing
#   - 第三方: 无
#   - 项目内: infrastructure.system_proxy(系统代理检测,延迟导入)
# ==============================================================================

# 导入 JSON 解析库,用于配置文件的读写
import json

# 导入日志记录库,用于记录配置操作日志
import logging

# 导入操作系统接口库,用于路径操作和文件操作
import os

# 导入系统相关库,用于平台判断和 frozen 检测
import sys

# 导入线程库,用于配置写入时的线程锁
import threading

# 导入类型提示库,用于函数签名的类型注解
from typing import Any, Dict
from urllib.parse import urlsplit  # 解析完整代理URL,避免重复追加端口

# 创建模块级日志器实例,命名空间为 Updater.Config
_logger = logging.getLogger("Updater.Config")

# ------------------------------------------------------------------------------
# 线程安全: 进程内写锁
# ------------------------------------------------------------------------------

# 全局 IO 锁,用于保护配置文件的写入操作,防止多线程并发写入导致文件损坏
_IO_LOCK = threading.Lock()

# ------------------------------------------------------------------------------
# 配置模块与文件名映射
# ------------------------------------------------------------------------------

# 更新助手只加载更新流程所需的五个模块;真实文件名优先来自config_manifest.json
_DEFAULT_FILE_MAP = {
    "version": "version.json", "github": "github.json", "proxy": "proxy.json",
    "file_io": "file_io.json", "user_info": "user_info.json",
}


def _load_manifest_file_map() -> Dict[str, str]:
    """从主程序维护的配置清单读取更新所需模块文件名,缺失或损坏时回退默认."""
    config_root = os.environ.get("SERVICE_CONFIG_DIR", "").strip()
    if not config_root:
        config_root = os.path.join(os.environ.get("SERVICE_DATA_DIR", "").strip(), "config")
    manifest_name = os.path.basename(
        os.environ.get("SERVICE_CONFIG_MANIFEST_FILENAME", "config_manifest.json").strip()
    ) or "config_manifest.json"
    raw_modules: Dict[str, Any] = {}
    try:
        with open(os.path.join(config_root, manifest_name), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("modules"), dict):
            raw_modules = raw["modules"]
    except (OSError, json.JSONDecodeError, ValueError):
        raw_modules = {}
    return {
        name: os.path.basename(str(raw_modules.get(name, default_name)).strip()) or default_name
        for name, default_name in _DEFAULT_FILE_MAP.items()
    }


MODULE_FILE_MAP: Dict[str, str] = _load_manifest_file_map()

# ------------------------------------------------------------------------------
# 内置默认值(精简版,仅含更新助手需要的字段)
# ------------------------------------------------------------------------------

# 各配置模块的内置默认值字典
# 文件不存在时使用这些默认值生成模板文件
BUILTIN_DEFAULTS: Dict[str, Dict[str, Any]] = {
    # 版本配置默认值
    "version": {
        "APP_VERSION": "0.0.0",                              # 当前应用版本号
        "AUTO_CHECK_UPDATE_ON_START": True,                   # 启动时自动检查更新
        "UPDATE_RELEASE_PAGE_TPL": "https://github.com/{owner}/{repo}/releases",  # Release 页面 URL 模板
        "UPDATE_SAVE_FILENAME_TPL": "wuge_tools-{platform}.{archive_ext}",  # Windows=zip,Linux=tar.gz,macOS=dmg
        "UPDATE_SETUP_FILENAME_TPL": "wuge_tools-{platform}-setup.exe",  # Windows安装版更新包(Inno Setup,仅安装版使用)
    },
    # GitHub 配置默认值
    "github": {
        "GITHUB_API_BASE_URL": "https://api.github.com",  # GitHub API 基础地址
        "GITHUB_TIMEOUT": 15,                             # API 请求超时时间(秒)
        "GITHUB_REPO_OWNER": "itwuge",                          # 仓库所有者
        "GITHUB_REPO_NAME": "wuge_tools",                           # 仓库名称
        "GITHUB_TOKEN": "",                               # GitHub 访问令牌
    },
    # 代理配置默认值
    "proxy": {
        "AUTO_DETECT_SYSTEM_PROXY": True,        # 是否自动检测系统代理
        "DEFAULT_HTTP_PROXY": "127.0.0.1",       # 默认 HTTP 代理地址
        "DEFAULT_HTTPS_PROXY": "127.0.0.1",      # 默认 HTTPS 代理地址
        "DEFAULT_PROXY_PORT": 38457,              # 默认代理端口
        "SERVICE_DEFAULT_USE_PROXY": False,      # 服务层是否默认使用代理
    },
    # 文件 IO 配置默认值
    "file_io": {
        "DOWNLOAD_CHUNK_SIZE": 2 * 1024 * 1024,   # 下载分片大小(2MB,减少慢启动)
        "DOWNLOAD_MAX_WORKERS": 16,                 # 下载最大并发数(16,多连接提速)
        "UPLOAD_CHUNK_SIZE": 5 * 1024 * 1024,     # 上传分片大小(5MB)
        "UPLOAD_MAX_WORKERS": 3,                   # 上传最大并发数
    },
    # 用户配置只保留更新流程需要的下载目录字段
    "user_info": {
        "download_dir": "downloads",  # 相对数据目录的更新包下载目录
    },
}


# ==============================================================================
# 函数: _to_abs_path
# ==============================================================================
def _to_abs_path(raw: str) -> str:
    """
    将原始路径转换为绝对路径.

    详细说明:
        处理路径字符串,去除首尾空白和引号,展开用户目录(~),
        相对路径以应用基准目录(_get_app_base_dir)为基准转为绝对路径.

    参数:
        raw (str): 原始路径字符串

    返回值:
        str: 转换后的绝对路径字符串;空输入返回空串

    异常:
        无
    """
    # 转字符串,去除首尾空白和可能的引号
    p = str(raw or "").strip().strip('"')
    # 空路径返回空串
    if not p:
        return ""
    # 展开用户目录(如 ~/xxx 展开为 /home/user/xxx)
    p = os.path.expanduser(p)
    # 如果已经是绝对路径,规范化后返回
    if os.path.isabs(p):
        return os.path.normpath(p)
    # 相对路径以应用基准目录为基准拼接后规范化返回
    return os.path.normpath(os.path.join(_get_app_base_dir(), p))


# ==============================================================================
# 函数: _get_app_base_dir
# ==============================================================================
def _get_app_base_dir() -> str:
    """
    获取应用基准目录.

    详细说明:
        frozen 模式下返回可执行文件所在目录;
        源码模式下返回项目根目录(本文件的上两级).

    参数:
        无

    返回值:
        str: 应用基准目录的绝对路径

    异常:
        无
    """
    # 判断是否为 PyInstaller 打包的 frozen 可执行文件
    if getattr(sys, "frozen", False):
        # frozen 模式: 返回可执行文件所在目录
        return os.path.dirname(os.path.abspath(sys.executable))
    # 源码模式: 返回项目根目录(本文件在 model/ 目录下,上两级是项目根)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==============================================================================
# 函数: get_data_store_root
# ==============================================================================
def get_data_store_root() -> str:
    """
    获取数据存储根目录路径.

    详细说明:
        优先级: SERVICE_DATA_DIR 环境变量 > 默认 app_dir/data_store.
        环境变量由 bootstrap 模块在 import 本模块之前注入,
        确保与主程序使用同一路径口径.

    参数:
        无

    返回值:
        str: 数据存储根目录的绝对路径

    异常:
        无
    """
    # 从环境变量读取数据目录路径,去除首尾空白
    env_dir = os.environ.get("SERVICE_DATA_DIR", "").strip()
    # 如果环境变量非空,转为绝对路径后返回
    if env_dir:
        return os.path.abspath(env_dir)
    # 环境变量为空时,使用应用基准目录下的 data_store 子目录
    return os.path.join(_get_app_base_dir(), "data_store")


# ==============================================================================
# 函数: get_config_root
# ==============================================================================
def get_config_root() -> str:
    """
    获取配置文件根目录路径.

    详细说明:
        优先使用主程序任务通过SERVICE_CONFIG_DIR注入的实际配置目录;
        环境变量缺失时回退到数据目录下的config子目录.

    参数:
        无

    返回值:
        str: 配置文件根目录的绝对路径

    异常:
        无
    """
    # 主程序移交时优先使用任务显式注入的实际配置目录
    env_root = os.environ.get("SERVICE_CONFIG_DIR", "").strip()
    if env_root:
        return os.path.abspath(env_root)
    # 旧任务或独立启动时回退数据根目录下的标准config子目录
    return os.path.join(get_data_store_root(), "config")


# ==============================================================================
# 函数: _strip_comment
# ==============================================================================
def _strip_comment(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    去除配置字典中的 _comment 注释字段.

    详细说明:
        配置文件中可能包含 _comment 字段用于说明,
        实际使用时需要去除该字段,避免污染业务数据.

    参数:
        d (Dict[str, Any]): 原始配置字典

    返回值:
        Dict[str, Any]: 去除 _comment 后的新字典

    异常:
        无
    """
    # 复制原字典(浅拷贝),避免修改原字典
    out = d.copy()
    # 弹出 _comment 键(如果存在),不抛出异常
    out.pop("_comment", None)
    # 返回去除注释后的字典
    return out


# ==============================================================================
# 函数: _merge_dict
# ==============================================================================
def _merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    合并两个字典,override 覆盖 base 中同名键的值.

    详细说明:
        只合并 base 中已存在的键,override 中新增的键不会被加入,
        保证配置结构的稳定性(用户不能随意添加新字段).
        以 base 为基础进行浅拷贝,然后用 override 中的值覆盖.

    参数:
        base (Dict[str, Any]): 基础字典(默认值)
        override (Dict[str, Any]): 覆盖字典(用户配置)

    返回值:
        Dict[str, Any]: 合并后的新字典

    异常:
        无
    """
    # 以 base 为基础进行浅拷贝
    res = base.copy()
    # 遍历 override 的键值对
    for k, v in override.items():
        # 只合并 base 中已存在的键(忽略新增键)
        if k in res:
            res[k] = v
    # 返回合并后的字典
    return res


# ==============================================================================
# 函数: _atomic_write_json
# ==============================================================================
def _atomic_write_json(fp: str, data: Dict[str, Any]) -> None:
    """
    原子写入 JSON 文件,防止写入中断导致文件损坏.

    详细说明:
        先写入临时文件(.tmp.{pid}),写入完成并 fsync 后,
        使用 os.replace 原子替换目标文件.
        这样即使写入过程中程序崩溃,原文件也不会损坏.

    参数:
        fp (str): 目标文件路径
        data (Dict[str, Any]): 要写入的数据字典

    返回值:
        None

    异常:
        OSError: 文件写入或替换失败时抛出
    """
    # 获取文件所在的父目录
    parent = os.path.dirname(fp)
    # 如果父目录非空,确保目录存在(不存在则创建)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 构造临时文件路径(加上进程 ID 后缀,避免多进程冲突)
    tmp_fp = f"{fp}.tmp.{os.getpid()}"
    # 以写入模式、UTF-8 编码打开临时文件
    with open(tmp_fp, "w", encoding="utf-8") as f:
        # 将数据序列化为 JSON 写入文件(不转义 ASCII、缩进 2 空格)
        json.dump(data, f, ensure_ascii=False, indent=2)
        # 刷新 Python 缓冲区,确保数据写入操作系统
        f.flush()
        # 调用 fsync 强制写入磁盘,防止系统缓存导致的数据丢失
        os.fsync(f.fileno())
    # 原子替换:将临时文件重命名为目标文件
    # os.replace 在 Windows 和 Linux 上都是原子操作
    os.replace(tmp_fp, fp)


# ==============================================================================
# 函数: _read_module_json
# ==============================================================================
def _read_module_json(module_name: str) -> dict:
    """
    读取指定模块的 JSON 配置文件.

    详细说明:
        更新助手只读真实配置:配置文件不存在时直接返回内存默认值,不创建主程序配置;
        文件损坏(JSON解析失败)时同样返回默认值且不覆盖损坏文件.
        返回的字典会去除 _comment 字段.

    参数:
        module_name (str): 配置模块名,必须在 MODULE_FILE_MAP 中

    返回值:
        dict: 配置字典(去除了 _comment 字段)

    异常:
        KeyError: 模块名不在 MODULE_FILE_MAP 中时抛出
    """
    # 检查模块名是否合法
    if module_name not in MODULE_FILE_MAP:
        raise KeyError(f"非固定配置模块:{module_name}")
    # 构造配置文件的完整路径
    fp = os.path.join(get_config_root(), MODULE_FILE_MAP[module_name])
    # 获取该模块的默认配置
    default_cfg = BUILTIN_DEFAULTS.get(module_name, {})
    # 配置文件不存在时仅返回内存默认值,更新助手不得替主程序创建配置文件
    if not os.path.exists(fp):
        return _strip_comment(default_cfg)
    # 文件存在,尝试读取并解析
    try:
        # 以只读模式、UTF-8 编码打开文件
        with open(fp, "r", encoding="utf-8") as f:
            # 解析 JSON 内容
            raw = json.load(f)
        # 返回去除注释后的配置
        return _strip_comment(raw)
    except Exception:
        # 读取或解析失败,返回默认配置(不覆盖损坏文件)
        return _strip_comment(default_cfg)


# ==============================================================================
# 函数: _write_module_json
# ==============================================================================
def _write_module_json(module_name: str, data: dict):
    """
    写入指定模块的 JSON 配置文件.

    详细说明:
        使用原子写入保证文件安全,写入前添加 _comment 注释字段(如果内置默认值有且当前没有).
        使用全局 _IO_LOCK 线程锁保护,防止多线程并发写入.

    参数:
        module_name (str): 配置模块名,必须在 MODULE_FILE_MAP 中
        data (dict): 要写入的数据字典

    返回值:
        None

    异常:
        KeyError: 模块名不在 MODULE_FILE_MAP 中时抛出
        OSError: 文件写入失败时抛出
    """
    # 检查模块名是否合法
    if module_name not in MODULE_FILE_MAP:
        raise KeyError(f"非固定配置模块:{module_name}")
    # 构造配置文件的完整路径
    fp = os.path.join(get_config_root(), MODULE_FILE_MAP[module_name])
    # 复制数据字典,避免修改原字典
    payload = dict(data)
    # 获取内置默认值中的注释
    builtin_comment = BUILTIN_DEFAULTS.get(module_name, {}).get("_comment")
    # 如果内置有注释且当前数据没有注释,则添加注释
    if builtin_comment and not payload.get("_comment"):
        payload = {"_comment": builtin_comment, **payload}
    # 使用线程锁保护写入操作
    with _IO_LOCK:
        # 原子写入 JSON 文件
        _atomic_write_json(fp, payload)


# ==============================================================================
# 函数: load_and_merge_all_config
# ==============================================================================
def load_and_merge_all_config() -> Dict[str, Dict[str, Any]]:
    """
    加载更新所需模块配置(逐个只读加载合并,文件缺失或损坏仅回退内存默认).

    详细说明:
        遍历 MODULE_FILE_MAP 中的所有模块,对每个模块:
        1. 如果配置文件不存在,仅使用内存默认值,不创建文件
        2. 如果文件存在,读取用户配置并与默认值合并
        3. 如果文件损坏,回退到内存默认值且不覆盖文件
        返回以模块名为键、配置字典为值的嵌套字典.

    参数:
        无

    返回值:
        Dict[str, Dict[str, Any]]: 全量配置字典,结构为 {模块名: {配置键: 值}}

    异常:
        不向外抛出异常;文件损坏时静默回退到默认值
    """
    # 确保配置根目录存在
    os.makedirs(get_config_root(), exist_ok=True)
    # 初始化最终配置字典
    final: Dict[str, Dict[str, Any]] = {}
    # 遍历所有配置模块
    for mod_name in MODULE_FILE_MAP:
        # 获取该模块的默认配置(去除注释)
        default_clean = _strip_comment(BUILTIN_DEFAULTS.get(mod_name, {}))
        # 构造配置文件路径
        fp = os.path.join(get_config_root(), MODULE_FILE_MAP[mod_name])
        # 配置文件不存在时仅使用内存默认值,不替主程序生成任何业务配置文件
        if not os.path.exists(fp):
            final[mod_name] = default_clean
            continue
        # 文件存在,尝试读取和合并
        try:
            # 以只读模式、UTF-8 编码打开文件
            with open(fp, "r", encoding="utf-8") as f:
                # 读取用户配置并去除注释
                user_clean = _strip_comment(json.load(f))
            # 合并默认值和用户配置(用户配置覆盖默认值)
            final[mod_name] = _merge_dict(default_clean, user_clean)
        except (json.JSONDecodeError, OSError):
            # JSON 解析错误或文件读取错误,使用默认值
            final[mod_name] = default_clean
    # 返回全量配置字典
    return final


# ---------------- 固定模块便捷读写函数 ----------------

# ==============================================================================
# 函数: load_user_info
# ==============================================================================
def load_user_info() -> dict:
    """
    读取用户信息配置.

    参数:
        无

    返回值:
        dict: 用户信息配置字典

    异常:
        KeyError: 模块名不合法时抛出(实际不会发生)
    """
    # 调用通用读取函数,指定模块名为 user_info
    return _read_module_json("user_info")


# ==============================================================================
# 函数: save_user_info
# ==============================================================================
def save_user_info(payload: dict):
    """
    保存用户信息配置(增量更新).

    详细说明:
        先读取当前配置,然后用 payload 中的键值更新,最后写回文件.
        只更新 payload 中存在的键,其他键保持不变.

    参数:
        payload (dict): 要更新的键值对

    返回值:
        None

    异常:
        KeyError: 模块名不合法时抛出
        OSError: 文件写入失败时抛出
    """
    # 先读取当前配置
    cfg = _read_module_json("user_info")
    # 用 payload 更新配置字典
    cfg.update(payload)
    # 写回配置文件
    _write_module_json("user_info", cfg)


# ==============================================================================
# 函数: load_github_config
# ==============================================================================
def load_github_config() -> dict:
    """
    读取 GitHub 配置.

    参数:
        无

    返回值:
        dict: GitHub 配置字典

    异常:
        KeyError: 模块名不合法时抛出(实际不会发生)
    """
    # 调用通用读取函数,指定模块名为 github
    return _read_module_json("github")


# ==============================================================================
# 函数: save_github_config
# ==============================================================================
def save_github_config(payload: dict):
    """
    保存 GitHub 配置(增量更新).

    详细说明:
        先读取当前配置,然后用 payload 中的键值更新,最后写回文件.

    参数:
        payload (dict): 要更新的键值对

    返回值:
        None

    异常:
        KeyError: 模块名不合法时抛出
        OSError: 文件写入失败时抛出
    """
    # 先读取当前配置
    cfg = _read_module_json("github")
    # 用 payload 更新配置字典
    cfg.update(payload)
    # 写回配置文件
    _write_module_json("github", cfg)


# ==============================================================================
# 函数: load_proxy_config
# ==============================================================================
def load_proxy_config() -> dict:
    """
    读取代理配置.

    参数:
        无

    返回值:
        dict: 代理配置字典

    异常:
        KeyError: 模块名不合法时抛出(实际不会发生)
    """
    # 调用通用读取函数,指定模块名为 proxy
    return _read_module_json("proxy")


# ==============================================================================
# 函数: save_proxy_config
# ==============================================================================
def save_proxy_config(payload: dict):
    """
    保存代理配置(增量更新).

    详细说明:
        先读取当前配置,然后用 payload 中的键值更新,最后写回文件.

    参数:
        payload (dict): 要更新的键值对

    返回值:
        None

    异常:
        KeyError: 模块名不合法时抛出
        OSError: 文件写入失败时抛出
    """
    # 先读取当前配置
    cfg = _read_module_json("proxy")
    # 用 payload 更新配置字典
    cfg.update(payload)
    # 写回配置文件
    _write_module_json("proxy", cfg)


# ==============================================================================
# 函数: save_file_io_config
# ==============================================================================
def save_file_io_config(payload: dict):
    """
    保存文件 IO 配置(增量更新).

    详细说明:
        先读取当前配置,然后用 payload 中的键值更新,最后写回文件.

    参数:
        payload (dict): 要更新的键值对

    返回值:
        None

    异常:
        KeyError: 模块名不合法时抛出
        OSError: 文件写入失败时抛出
    """
    # 先读取当前配置
    cfg = _read_module_json("file_io")
    # 用 payload 更新配置字典
    cfg.update(payload)
    # 写回配置文件
    _write_module_json("file_io", cfg)


# ---------------- 版本/下载相关工具函数 ----------------

# ==============================================================================
# 函数: semver_compare
# ==============================================================================
def semver_compare(v1: str, v2: str) -> int:
    """
    比较两个语义化版本号的大小.

    详细说明:
        支持带 v/V 前缀的版本号(如 v1.2.3),比较前自动去除前缀.
        版本号按点号分割为多个部分,逐个部分比较(转整数比较).
        位数不足时补 0 比较.

    参数:
        v1 (str): 版本号 1
        v2 (str): 版本号 2

    返回值:
        int: 比较结果:
            - 1: v1 > v2
            - -1: v1 < v2
            - 0: v1 == v2

    异常:
        无;无法解析的部分按 0 处理
    """
    # 内部辅助函数: 解析版本号为整数列表
    def _parse(ver: str):
        # 初始化版本部分列表
        parts = []
        # 去除首尾空白
        ver = ver.strip()
        # 去除 v/V 前缀
        if ver.startswith(("v", "V")):
            ver = ver[1:]
        # 按点号分割版本号
        for s in ver.split("."):
            try:
                # 尝试转为整数
                parts.append(int(s))
            except ValueError:
                # 转整数失败,按 0 处理
                parts.append(0)
        # 返回整数列表
        return parts

    # 解析两个版本号
    a, b = _parse(v1), _parse(v2)
    # 遍历比较(取最大长度,不足补 0)
    for i in range(max(len(a), len(b))):
        # 取第 i 部分,超出长度则为 0
        ai = a[i] if i < len(a) else 0
        bi = b[i] if i < len(b) else 0
        # 比较大小
        if ai > bi:
            return 1
        elif ai < bi:
            return -1
    # 所有部分都相等
    return 0


# ==============================================================================
# 函数: is_installed_edition
# ==============================================================================
def is_installed_edition(app_dir: str) -> bool:
    """检测软件目录是否为安装版(Inno Setup 安装后释放 installed.flag).

    :param app_dir: 软件安装目录绝对路径
    :type app_dir: str
    :return: True=安装版;False=绿色版/源码运行
    :rtype: bool
    """
    return os.path.isfile(os.path.join(app_dir, "installed.flag"))


# ==============================================================================
# 函数: get_update_download_dir
# ==============================================================================
def get_update_download_dir(version_cfg: Dict[str, Any]) -> str:
    """
    获取更新包的保存目录.

    详细说明:
        优先级: user_info.download_dir > 默认数据目录/downloads.
        返回绝对路径.

    参数:
        version_cfg (Dict[str, Any]): 版本配置字典(预留参数,当前未使用)

    返回值:
        str: 更新包保存目录的绝对路径

    异常:
        无
    """
    # 读取用户信息配置
    user_cfg = load_user_info()
    # 获取下载目录配置值,去除首尾空白
    raw = str(user_cfg.get("download_dir", "")).strip()
    # 如果配置了下载目录:绝对路径原样使用,相对路径以数据目录为唯一根拼接
    if raw:
        return os.path.normpath(raw) if os.path.isabs(raw) \
            else os.path.abspath(os.path.join(get_data_store_root(), raw))
    # 未配置时使用数据目录下的 downloads 子目录
    return os.path.join(get_data_store_root(), "downloads")


# ==============================================================================
# 函数: render_update_save_filename
# ==============================================================================
def render_update_save_filename(version_cfg: Dict[str, Any], tag: str = "", installed: bool = False) -> str:
    """
    渲染更新包的保存文件名(根据当前平台).

    详细说明:
        使用 version_cfg 中的 UPDATE_SAVE_FILENAME_TPL 模板,
        替换 {platform} 和 {tag} 占位符.
        平台名: Windows / Linux / macOS-arm64 / macOS-x86_64.
        macOS 区分 CPU 架构以匹配对应架构的更新包资产.

    参数:
        version_cfg (Dict[str, Any]): 版本配置字典,包含文件名模板
        tag (str): 版本标签,用于替换 {tag} 占位符;默认为空串
        installed (bool): 是否安装版(仅 Windows 生效);安装版匹配 setup.exe 安装包

    返回值:
        str: 渲染后的文件名

    异常:
        无
    """
    # 导入 platform 模块(局部导入,避免模块级依赖)
    import platform as _pf

    # 获取当前操作系统名称
    system = _pf.system()
    # 映射为统一的平台名;macOS 区分 CPU 架构(arm64/x86_64)以匹配对应更新包
    if system == "Windows":
        plat = "Windows"
    elif system == "Darwin":
        # platform.machine() 返回 "arm64"(Apple Silicon)或 "x86_64"(Intel/Rosetta)
        plat = f"macOS-{_pf.machine()}"  # 如 macOS-arm64 / macOS-x86_64
    else:
        plat = "Linux"
    # 平台更新包格式契约: Windows=zip, Linux=tar.gz, macOS=dmg(原生磁盘镜像)
    # 注意: macOS 必须用 dmg 而非 tar.gz,否则与 Release 资产名(如 wuge_tools-macOS-arm64.dmg)匹配不上
    if system == "Windows":
        archive_ext = "zip"
    elif system == "Darwin":
        archive_ext = "dmg"
    else:
        archive_ext = "tar.gz"
    tpl = version_cfg.get("UPDATE_SAVE_FILENAME_TPL", "wuge_tools-{platform}.{archive_ext}")
    # Windows 安装版: 改用 setup.exe 安装包模板(与绿色版 zip 区分)
    if installed and system == "Windows":
        tpl = version_cfg.get("UPDATE_SETUP_FILENAME_TPL", "wuge_tools-{platform}-setup.exe")
    # 格式化模板,替换平台、版本和归档格式占位符后返回
    return tpl.format(tag=tag, platform=plat, archive_ext=archive_ext)


# ==============================================================================
# 函数: render_release_page_url
# ==============================================================================
def render_release_page_url(version_cfg: Dict[str, Any], github_cfg: Dict[str, Any]) -> str:
    """
    渲染 Release 页面的 URL.

    详细说明:
        使用 version_cfg 中的 UPDATE_RELEASE_PAGE_TPL 模板,
        替换 {owner} 和 {repo} 占位符.

    参数:
        version_cfg (Dict[str, Any]): 版本配置字典,包含 URL 模板
        github_cfg (Dict[str, Any]): GitHub 配置字典,包含仓库所有者和名称

    返回值:
        str: 渲染后的 Release 页面 URL

    异常:
        无
    """
    # 从配置中获取 URL 模板,默认值为 GitHub Release 页面模板
    tpl = version_cfg.get("UPDATE_RELEASE_PAGE_TPL", "https://github.com/{owner}/{repo}/releases")
    # 格式化模板,替换仓库所有者和名称后返回
    return tpl.format(owner=github_cfg.get("GITHUB_REPO_OWNER", ""), repo=github_cfg.get("GITHUB_REPO_NAME", ""))


# ---------------- 代理解析相关函数 ----------------

# ==============================================================================
# 函数: _with_proxy_scheme
# ==============================================================================
def _with_proxy_scheme(addr: str) -> str:
    """
    确保代理地址包含协议前缀(http://).

    详细说明:
        如果代理地址已经包含 ://,则原样返回;
        否则添加 http:// 前缀.

    参数:
        addr (str): 代理地址

    返回值:
        str: 带协议前缀的代理地址

    异常:
        无
    """
    # 去除首尾空白
    addr = (addr or "").strip()
    # 如果已经包含协议前缀,直接返回;否则添加 http:// 前缀
    return addr if "://" in addr else f"http://{addr}"


# ==============================================================================
# 函数: resolve_effective_proxy
# ==============================================================================
def resolve_effective_proxy(proxy_cfg: dict):
    """
    统一解析实际生效的代理配置.

    详细说明:
        优先级从高到低:
        1. 自动跟随系统代理(AUTO_DETECT_SYSTEM_PROXY=True)
        2. 手动配置代理(SERVICE_DEFAULT_USE_PROXY=True)
        3. 不使用代理

        SOCKS 代理需要 PySocks 库支持,缺少时降级为直连并给出提示.

    参数:
        proxy_cfg (dict): 代理配置字典

    返回值:
        Tuple[Optional[dict], str]: 二元组,包含:
            - proxies: 代理字典(http/https),不使用代理时为 None
            - source: 代理来源描述字符串(用于日志显示)

    异常:
        不向外抛出异常
    """
    # 配置为空字典时的安全处理
    proxy_cfg = proxy_cfg or {}
    # 检查是否启用了自动检测系统代理
    if proxy_cfg.get("AUTO_DETECT_SYSTEM_PROXY", False):
        # 从基础设施层导入系统代理检测函数(延迟导入,避免不需要时的依赖)
        from infrastructure.system_proxy import detect_system_proxy

        # 检测系统代理
        proxies, source = detect_system_proxy()
        # 如果检测到了系统代理
        if proxies:
            # 获取第一个代理地址
            proxy_url = list(proxies.values())[0] if proxies else ""
            # 如果是 SOCKS 代理
            if proxy_url.startswith("socks"):
                try:
                    # 尝试导入 socks 库,检查是否有 PySocks 支持
                    import socks  # noqa: F401
                except ImportError:
                    # 缺少 PySocks 库,降级为直连
                    return None, f"自动跟随-{source},SOCKS缺少PySocks降级直连"
            # 返回代理配置和来源描述
            return proxies, f"自动跟随-{source}"
        # 未检测到系统代理,返回直连
        return None, f"自动跟随-{source},系统未开启代理"

    # 检查是否启用了手动代理
    if proxy_cfg.get("SERVICE_DEFAULT_USE_PROXY", False):
        # 获取 HTTP 代理地址
        http_addr = proxy_cfg.get("DEFAULT_HTTP_PROXY", "127.0.0.1")
        # 获取 HTTPS 代理地址
        https_addr = proxy_cfg.get("DEFAULT_HTTPS_PROXY", "127.0.0.1")
        # 获取代理端口
        port = proxy_cfg.get("DEFAULT_PROXY_PORT", 38457)
        def with_port(address: str) -> str:
            """完整代理URL已有端口时原样返回,只有裸主机才追加独立端口."""
            url = _with_proxy_scheme(address)
            try:
                return url if urlsplit(url).port else f"{url}:{port}"
            except ValueError:
                return f"{url}:{port}"

        # 构造代理字典,保留SOCKS协议且避免形成host:port:port
        proxies = {
            "http": with_port(http_addr),
            "https": with_port(https_addr),
        }
        # 获取第一个代理地址用于判断类型
        proxy_url = list(proxies.values())[0] if proxies else ""
        # 如果是 SOCKS 代理
        if proxy_url.startswith("socks"):
            try:
                # 尝试导入 socks 库
                import socks  # noqa: F401
            except ImportError:
                # 缺少 PySocks 库,降级为直连
                return None, "手动SOCKS代理缺少PySocks降级直连"
        # 返回手动代理配置和来源描述
        return proxies, "手动配置代理"

    # 代理已关闭,返回直连
    return None, "代理已关闭"


# ------------------------------------------------------------------------------
# 对外导出符号列表
# ------------------------------------------------------------------------------
__all__ = [
    "MODULE_FILE_MAP",           # 配置模块-文件名映射
    "get_data_store_root",       # 获取数据存储根目录
    "get_config_root",           # 获取配置根目录
    "load_and_merge_all_config", # 加载并合并全部配置
    "_read_module_json",         # 读取单个模块配置
    "_write_module_json",        # 写入单个模块配置
    "load_user_info",            # 读取用户信息配置
    "save_user_info",            # 保存用户信息配置
    "load_github_config",        # 读取 GitHub 配置
    "save_github_config",        # 保存 GitHub 配置
    "load_proxy_config",         # 读取代理配置
    "save_proxy_config",         # 保存代理配置
    "save_file_io_config",       # 保存文件 IO 配置
    "is_installed_edition",      # 安装版识别
    "semver_compare",            # 版本号比较
    "get_update_download_dir",   # 获取更新下载目录
    "render_update_save_filename",  # 渲染更新包文件名
    "render_release_page_url",   # 渲染 Release 页面 URL
    "resolve_effective_proxy",   # 解析实际生效的代理
]
