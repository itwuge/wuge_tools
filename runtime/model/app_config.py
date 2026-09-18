# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: app_config.py
# 归属: runtime/model 业务模型层(MVC-M) —— 通用配置管理模块
# ------------------------------------------------------------------------------
# 文件用途:
#   通用配置管理模块——负责程序路径三级解析、多个 JSON 配置文件的加载/合并/
#   原子落盘、版本更新辅助、GitHub/SMTP/代理/文件IO 等出厂默认配置,以及
#   运行时有效业务路径的维护.
# ------------------------------------------------------------------------------
# 架构定位:
#   MVC 中的 Model 基础配置层,被 MainModel/controller 及更新助手调用,管理
#   配置/路径数据;不依赖任何 view/Qt 模块;infrastructure 底层禁止反向
#   import 本模块(单向依赖).
#
#   关联组件:
#     - 上游: MainModel、controller、更新助手
#     - 下游: infrastructure.system_proxy(函数内延迟导入)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 程序根目录/数据目录/配置目录的三级解析与指针文件管理
#   2. 七个 JSON 模块的出厂默认、缺失自动导出模板、损坏单独回退、用户键覆盖合并
#   3. 运行时业务路径(cookie/账号/记录/下载/配置/邮件/日志)的默认值计算与界面配置刷新
#   4. 数据目录切换后的启动期一次性整体复制迁移(旧目录保留)
#   5. 语义化版本比较、release 页面地址与更新包文件名渲染、请求头组装
#   6. 统一代理决策(系统代理自动跟随/手动代理/SOCKS 缺依赖降级直连)
#   7. 所有 JSON 落盘走"同目录临时文件 + os.replace"原子写
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责配置与路径管理,不涉及 UI 或网络请求
#   - JSON 配置文件(version/github/smtp/proxy/file_io/user_info)存放于配置目录
#   - 数据目录由程序目录指针 .data_dir_pointer.json 承载,配置目录由总目录内
#     .path_pointer.json 承载;迁移完成标记为 .data_migrated;全部写操作为原子写
#   - config 目录全部 json 包含明文密钥,务必加入 .gitignore
# ------------------------------------------------------------------------------
# 线程模型:
#   配置读写通过全局 IO 锁(_IO_LOCK)保护,确保多线程并发安全;
#   路径解析为纯函数无状态,可在任意线程调用;
#   系统代理检测涉及阻塞 IO,建议在工作线程调用.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: json、logging、os、shutil、sys、threading、pathlib、typing
#   - 第三方: 无(函数内按需尝试导入 socks 以探测 PySocks)
#   - 项目内: infrastructure.system_proxy(函数内延迟导入,仅单向引用)
# ==============================================================================
# =========** [AppConfig]Model =========
import json  # 标准库:JSON 文件解析与序列化
import logging  # 标准库:配置层运行日志
import os  # 标准库:路径拼接、环境变量、目录/文件操作
import shutil  # 标准库:目录树整体复制(数据目录迁移)
import sys  # 标准库:判断 frozen 打包模式、取 exe 路径
import threading  # 标准库:运行时路径锁与落盘锁
from typing import Any, Dict, Optional  # 标准库:类型注解
from urllib.parse import urlsplit  # 标准库:解析完整代理URL,判断是否已包含端口

# 配置层运行日志:App启动后root的app.log FileHandler自动接管
_logger = logging.getLogger("AppConfig")  # 本模块专用logger,名称AppConfig

# 导入期清单加载也会执行落盘,因此写锁和原子写函数必须先于MODULE_FILE_MAP初始化
_IO_LOCK = threading.Lock()  # 所有配置及清单落盘共用的进程内锁


def _atomic_write_json(fp: str, data: Dict[str, Any]) -> None:
    """以同目录临时文件+os.replace方式原子写入JSON."""
    parent = os.path.dirname(fp)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_fp = f"{fp}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_fp, fp)
    finally:
        if os.path.exists(tmp_fp):
            try:
                os.remove(tmp_fp)
            except OSError:
                pass


# ==============================================================================
# [固定锚点:程序自身所在目录;不依赖启动时CWD,从快捷方式/任意目录启动都一致]
# ==============================================================================
def _detect_app_base_dir() -> str:
    """检测程序自身所在的固定锚点目录.

    打包模式下:
        - Windows/Linux: 取可执行文件 sys.executable 所在目录
        - macOS: 可执行文件位于 xxx.app/Contents/MacOS/ 下,需回退到 .app 包所在目录
    脚本模式:本文件位于 runtime/model/,向上回溯两级得到项目根目录.

    :return: 程序根目录绝对路径
    :rtype: str
    """
    # PyInstaller 等打包后 sys.frozen 为 True,据此区分运行形态
    if getattr(sys, "frozen", False):
        exe_path = os.path.abspath(sys.executable)
        # macOS: 可执行文件位于 xxx.app/Contents/MacOS/ 下
        # 需找到 .app 包目录并返回其父目录作为程序根目录
        if sys.platform == "darwin":
            parts = exe_path.split(os.sep)
            # 从后往前查找 .app 目录
            for i in range(len(parts) - 1, -1, -1):
                if parts[i].endswith(".app"):
                    # .app 包的父目录即为程序根目录
                    if i > 0:
                        return os.sep.join(parts[:i])
                    break
            # 兜底:回退三级(MacOS -> Contents -> xxx.app -> 父目录)
            return os.path.dirname(os.path.dirname(os.path.dirname(exe_path)))
        # 非 macOS: 锚定为可执行文件所在目录
        return os.path.dirname(exe_path)
    # 脚本模式:本文件位于 runtime/model/,向上回溯两级(dirname×3)得到项目根目录
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


APP_BASE_DIR = _detect_app_base_dir()  # 程序根目录锚点,模块导入时解析一次,全进程固定

def _macos_default_data_root() -> str:
    """macOS 出厂默认数据根:~/Library/Application Support/wuge_tools.

    遵循 macOS 系统惯例:用户数据统一放 Application Support 目录.
    程序包(.app)所在目录(如 /Applications)通常对普通用户只读,
    且软件更新时整个 .app 包会被替换,数据放程序目录既不安全也易丢失.

    :return: macOS 默认数据根绝对路径
    :rtype: str
    """
    return os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "wuge_tools")


def _is_installed_edition() -> bool:
    """检测是否为安装版(Inno Setup 安装后在程序目录释放 installed.flag).

    安装版程序目录位于 Program Files 等系统目录,普通用户只读,
    数据目录必须外置到用户可写位置;绿色版无该标记,保持程序目录/data_store.

    :return: True=安装版;False=绿色版/源码运行
    :rtype: bool
    """
    return os.path.isfile(os.path.join(APP_BASE_DIR, "installed.flag"))


def _win_installed_default_data_root() -> str:
    """Windows 安装版默认数据根:%LOCALAPPDATA%/wuge_tools(用户可写,升级/卸载不丢数据)."""
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "wuge_tools")


def _default_data_store_dir() -> str:
    """按运行形态返回出厂默认数据目录."""
    if sys.platform == "darwin":
        return _macos_default_data_root()
    if sys.platform.startswith("win") and _is_installed_edition():
        return _win_installed_default_data_root()
    return os.path.join(APP_BASE_DIR, "data_store")


def _default_pointer_file() -> str:
    """按运行形态返回数据目录指针文件位置(不能放在数据目录内部,鸡生蛋)."""
    if sys.platform == "darwin":
        return os.path.join(_macos_default_data_root(), ".data_dir_pointer.json")
    if sys.platform.startswith("win") and _is_installed_edition():
        return os.path.join(_win_installed_default_data_root(), ".data_dir_pointer.json")
    return os.path.join(APP_BASE_DIR, ".data_dir_pointer.json")


# 出厂默认数据目录(也是首次自定义总目录时的迁移源)
# macOS: ~/Library/Application Support/wuge_tools;Windows安装版: %LOCALAPPDATA%/wuge_tools
# 其他(绿色版): <程序目录>/data_store
BUILTIN_STORE_DIR = _default_data_store_dir()
# 数据目录指针:位于程序目录(不能放在数据目录内部,鸡生蛋);删除它即恢复出厂目录
DATA_ROOT_POINTER_FILE = _default_pointer_file()


def _to_abs_path(raw: str) -> str:
    """把配置中的路径值规范化为绝对路径(相对路径锚定APP_BASE_DIR,绝对路径原样规范化).

    处理步骤:去空白与首尾引号 -> 展开~用户目录 -> 绝对路径直接规范化,
    相对路径拼接 APP_BASE_DIR,使路径不再受启动时 CWD 影响.

    :param raw: 配置中的原始路径值(可为相对路径/绝对路径/空值)
    :type raw: str
    :return: 规范化后的绝对路径;入参为空时返回空串
    :rtype: str
    """
    p = str(raw or "").strip().strip('"')  # 转字符串、去首尾空白、去掉可能被误写入的双引号
    if not p:  # 空路径直接返回空串,表示该路径未配置
        return ""
    p = os.path.expanduser(p)  # 展开类 Unix 的 ~ 用户目录写法
    if os.path.isabs(p):  # 已是绝对路径:仅做规范化(消除../、重复分隔符等)
        return os.path.normpath(p)
    # 相对路径:锚定程序根目录拼接后再规范化,保证任意 CWD 启动结果一致
    return os.path.normpath(os.path.join(APP_BASE_DIR, p))


def _resolve_data_store_root() -> str:
    """解析当前生效的数据总目录.

    解析优先级:环境变量 SERVICE_DATA_DIR > 程序目录指针文件 .data_dir_pointer.json
    中的 data_dir > 出厂默认 data_store.指针文件损坏时静默回退,避免配置系统无法启动.

    :return: 数据总目录绝对路径
    :rtype: str
    """
    env_dir = os.environ.get("SERVICE_DATA_DIR", "").strip()  # 第一优先级:环境变量覆盖(运维/服务化部署用)
    if env_dir:  # 环境变量非空则直接采用(同样经相对路径锚定转换)
        return _to_abs_path(env_dir)
    try:  # 第二优先级:读取程序目录下的数据目录指针文件
        if os.path.exists(DATA_ROOT_POINTER_FILE):  # 指针文件存在才读取(界面切换过数据目录才会有)
            with open(DATA_ROOT_POINTER_FILE, "r", encoding="utf-8") as f:  # UTF-8 打开指针 JSON
                pointer = json.load(f)  # 解析指针内容
            pointed = str(pointer.get("data_dir", "")).strip()  # 取 data_dir 字段并去空白
            if pointed:  # 字段有效则转换为绝对路径返回
                return _to_abs_path(pointed)
    except (json.JSONDecodeError, OSError, ValueError):  # 捕获JSON解析错误/文件IO错误/值错误等全部异常
        # 指针损坏时静默回退出厂目录,避免配置系统无法启动
        pass  # 异常时不做任何操作,继续执行到兜底返回
    return BUILTIN_STORE_DIR  # 兜底:出厂默认数据目录


# 当前生效的数据目录,解析一次全进程生效
DATA_STORE_ROOT = _resolve_data_store_root()  # 模块导入时定型,运行期不再变动(更改需重启)
# 兼容性别名:历史代码中的DEFAULT_STORE_DIR即当前生效总目录
DEFAULT_STORE_DIR = DATA_STORE_ROOT  # 旧代码引用名,与 DATA_STORE_ROOT 完全等价
# 配置目录指针文件:界面单独切换"配置目录"后生成,删除它即跟随数据目录默认
CONFIG_ROOT_POINTER_FILE = os.path.join(DATA_STORE_ROOT, ".path_pointer.json")  # 位于数据总目录内


def _resolve_config_subdir(raw: Any, data_root: str) -> str:
    """启动期解析配置子目录,保证它始终位于指定数据目录内部."""
    value = os.path.expanduser(str(raw or "").strip().strip('"'))  # 清理指针中的路径文本
    if not value:
        return ""
    root = os.path.abspath(data_root)
    if os.path.isabs(value):
        try:
            relative = os.path.relpath(os.path.normpath(value), root)
        except ValueError:
            relative = os.path.basename(os.path.normpath(value))
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            relative = os.path.basename(os.path.normpath(value))
    else:
        relative = os.path.normpath(value)
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    if parts and parts[0].lower() == "data_store":       # 兼容旧版./data_store/config写法
        parts = parts[1:]
    safe_relative = os.path.join(*parts) if parts else ""
    if not safe_relative or safe_relative == os.pardir or safe_relative.startswith(os.pardir + os.sep):
        return ""
    return os.path.abspath(os.path.join(root, safe_relative))


def _resolve_config_root() -> str:
    """解析当前生效的配置目录.

    解析优先级:环境变量 SERVICE_CONFIG_DIR > 数据总目录内指针文件 .path_pointer.json
    中的 config_root > 数据总目录/config.指针损坏时静默回退默认.

    :return: 配置目录绝对路径(各业务 JSON 均存放于此)
    :rtype: str
    """
    env_root = os.environ.get("SERVICE_CONFIG_DIR", "").strip()  # 第一优先级:环境变量覆盖
    if env_root:  # 环境变量非空也必须约束在当前数据目录下
        resolved = _resolve_config_subdir(env_root, DATA_STORE_ROOT)
        if resolved:
            return resolved
    try:  # 第二优先级:读取总目录内的配置目录指针
        if os.path.exists(CONFIG_ROOT_POINTER_FILE):  # 仅当界面单独切换过配置目录时该文件存在
            with open(CONFIG_ROOT_POINTER_FILE, "r", encoding="utf-8") as f:  # UTF-8 打开指针文件
                pointer = json.load(f)  # 解析指针 JSON
            pointed = str(pointer.get("config_root", "")).strip()  # 取 config_root 字段
            if pointed:  # 字段有效则按当前数据目录约束解析
                resolved = _resolve_config_subdir(pointed, DATA_STORE_ROOT)  # 配置目录不得脱离数据目录
                if resolved:
                    return resolved
    except (json.JSONDecodeError, OSError, ValueError):  # 捕获指针文件读取/解析的全部异常
        # 指针损坏时静默回退默认,避免配置系统无法启动
        pass  # 异常时静默跳过,继续到兜底返回
    return os.path.join(DATA_STORE_ROOT, "config")  # 兜底:总目录下的 config 子目录


# 解析一次,全进程生效
DATA_CONFIG_ROOT = _resolve_config_root()  # 启动期固定的配置目录(界面更改需重启)

# ==============================================================================
# [运行时有效路径]初始=默认;加载user_info.json后由apply_path_settings()覆盖
# 业务层任何时刻调get_store_sub_paths()拿到的都是最新有效路径
# ==============================================================================
_PATH_LOCK = threading.Lock()  # 保护 _RUNTIME_PATHS 的线程锁(UI线程与后台线程可能并发读写)


def _default_paths() -> Dict[str, str]:
    """构建出厂默认的各业务存储路径字典(全部位于数据总目录标准布局下).

    :return: 路径槽位 -> 绝对路径 的默认字典
    :rtype: Dict[str, str]
    """
    return {
        "COOKIE_DIR": os.path.join(DEFAULT_STORE_DIR, "cookies"),  # Cookie 存储目录
        "ACCOUNT_DIR": os.path.join(DEFAULT_STORE_DIR, "accounts"),  # 账号详情/凭证目录
        "CHECKIN_RECORD_DIR": os.path.join(DEFAULT_STORE_DIR, "checkin_record"),  # 签到记录目录
        "DOWNLOAD_SAVE_ROOT": os.path.join(DEFAULT_STORE_DIR, "downloads"),  # 文件/更新包下载根目录
        "CONFIG_ROOT": DATA_CONFIG_ROOT,  # 配置目录(启动期解析,不随相对路径默认走总目录拼接)
        "MAIL_DIR": os.path.join(DEFAULT_STORE_DIR, "mail_result"),  # 邮件结果目录(文件名固定)
        "LOG_DIR": os.path.join(DEFAULT_STORE_DIR, "logs"),  # 日志目录(日志文件名固定 app.log)
    }


_RUNTIME_PATHS: Dict[str, str] = _default_paths()  # 运行时当前生效路径,初始化为出厂默认

# user_info.json路径key -> 运行时路径槽位 映射(目录类)
_PATH_KEY_MAP = {
    "cookie_dir": "COOKIE_DIR",  # 界面 cookie_dir 键 -> COOKIE_DIR 槽位
    "account_dir": "ACCOUNT_DIR",  # 界面 account_dir 键 -> ACCOUNT_DIR 槽位
    "checkin_record_dir": "CHECKIN_RECORD_DIR",  # 签到记录目录键槽映射
    "download_dir": "DOWNLOAD_SAVE_ROOT",  # 下载目录键名不同,需显式映射到下载根槽位
    "mail_dir": "MAIL_DIR",  # 邮件目录键槽映射
    "log_dir": "LOG_DIR",  # 日志目录键槽映射
}
# ==============================================================================
# [1. 各个模块内置出厂默认字典,对应json文件]
# ==============================================================================
BUILTIN_MODULE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    # --------[基础设施模块:版本/GitHub/SMTP/代理/文件IO]--------
    "version": {  # 版本&自动更新模块,对应 version.json
        "_comment": "【版本&自动更新配置】本地程序版本,github更新检测模板;更新包下载目录由用户在路径配置中设置的download_dir决定;线上版本运行时API拉取,不要写死在这里;AUTO_CHECK_UPDATE_ON_START控制主窗口启动后是否自动静默检测新版本",  # 人读注释字段,加载时由_strip_comment剔除
        "APP_VERSION": "1.0.0",  # 本地程序版本号(语义化 x.y.z),更新成功后回写
        "AUTO_CHECK_UPDATE_ON_START": True,  # 主窗口启动后是否自动静默检测新版本,默认开
        "UPDATE_RELEASE_PAGE_TPL": "https://github.com/{owner}/{repo}/releases",  # release 页面地址模板,占位符 owner/repo
        "UPDATE_SAVE_FILENAME_TPL": "wuge_tools-{platform}.{archive_ext}",  # Windows=zip,Linux=tar.gz,macOS=dmg
        "UPDATE_SETUP_FILENAME_TPL": "wuge_tools-{platform}-setup.exe"  # Windows安装版更新包(Inno Setup,仅安装版使用)
    },
    "github": {  # GitHub API 模块,对应 github.json
        "_comment": "【GitHub Api配置】API域名、超时、仓库元信息(owner/name)、github token;留空=匿名访问,token泄露会有权限风险",  # 人读注释字段,加载时剔除
        "GITHUB_API_BASE_URL": "https://api.github.com",  # GitHub API 基础域名(主程序不暴露,供更新助手)
        "GITHUB_TIMEOUT": 15,  # API 请求超时秒数
        "GITHUB_REPO_OWNER": "itwuge",  # 仓库所有者(owner)
        "GITHUB_REPO_NAME": "wuge_tools",  # 仓库名(repo)
        "GITHUB_TOKEN": "",  # 访问令牌,留空=匿名访问(敏感信息,禁止上传 git)
    },
    "transfer_github": {  # 文件传输Tab独立的GitHub配置,对应 transfer_github.json
        "_comment": "【文件传输下载器专用GitHub配置】与自更新github.json完全独立;仅Release资产拉取/下载使用,不影响自更新检测;留空=匿名访问",  # 人读注释,加载时剔除
        "GITHUB_API_BASE_URL": "https://api.github.com",  # GitHub API 基础域名
        "GITHUB_REPO_OWNER": "",  # 仓库所有者(owner),下载器专用
        "GITHUB_REPO_NAME": "",  # 仓库名(repo),下载器专用
        "GITHUB_TOKEN": "",  # 访问令牌,留空=匿名访问(敏感信息,禁止上传 git)
    },
    "smtp": {  # SMTP 邮件模块,对应 smtp.json
        "_comment": "【SMTP邮件配置】发件服务器、端口、发件账号、授权码、默认收件、抄送列表;授权码属于敏感密钥",  # 人读注释字段,加载时剔除
        "SMTP_DEFAULT_HOST": "smtp.qq.com",  # 发件服务器主机,默认 QQ 邮箱
        "SMTP_DEFAULT_PORT": 465,  # SMTP SSL 端口,默认 465
        "SMTP_SENDER_EMAIL": "enghin110@qq.com",  # 发件邮箱账号
        "SMTP_AUTH_CODE": "",  # 发件授权码(敏感密钥,默认留空,由用户在界面配置填写;严禁硬编码/上传)
        "SMTP_DEFAULT_TO_LIST": ["enghin110@qq.com"],  # 默认收件人列表(界面取第一个回填)
        "SMTP_DEFAULT_CC_LIST": []  # 默认抄送列表,出厂为空
    },
    "wecom": {  # 企业微信推送模块,对应 wecom.json
        "_comment": "【企业微信推送配置】群机器人Webhook完整地址(含key=鉴权参数,只发不收,泄露后需在群设置删除重建)、消息类型;webhook地址属于敏感凭据",  # 人读注释字段,加载时剔除
        "WECOM_ENABLE": True,  # 企业微信推送总开关,默认开
        "WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=7a71bb18-97df-4414-a0b5-a23b643c9aa4",  # 群机器人 Webhook 完整地址(https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx,敏感凭据,默认留空)
        "WECOM_MSG_TYPE": "text"  # 推送消息类型:text=纯文本(最兼容,个人微信可显示)/markdown=仅企业微信内渲染
    },
    "proxy": {  # 代理模块,对应 proxy.json
        "_comment": "【代理配置】AUTO_DETECT_SYSTEM_PROXY=true时自动跟随系统代理(Win/macOS/Linux),无需手填;关闭后按SERVICE_DEFAULT_USE_PROXY与手动地址端口生效",  # 人读注释字段,加载时剔除
        "AUTO_DETECT_SYSTEM_PROXY": True,  # 是否自动跟随系统代理,优先级最高
        "DEFAULT_HTTP_PROXY": "127.0.0.1",  # 手动 HTTP 代理地址
        "DEFAULT_HTTPS_PROXY": "127.0.0.1",  # 手动 HTTPS 代理地址
        "DEFAULT_PROXY_PORT": 38457,  # 手动代理端口
        "SERVICE_DEFAULT_USE_PROXY": False  # 自动检测关闭后,是否启用手动代理的总开关
    },
    "file_io": {  # 文件上传下载模块,对应 file_io.json
        "_comment": "【文件上传下载配置】分片大小、并发线程数;FileDownloader / FileUploader底层使用",  # 人读注释字段,加载时剔除
        "DOWNLOAD_CHUNK_SIZE": 2 * 1024 * 1024,  # 下载单片大小:2MB(调大减少慢启动)
        "DOWNLOAD_MAX_WORKERS": 16,  # 下载并发线程数:16(多连接吃满带宽)
        "UPLOAD_CHUNK_SIZE": 5 * 1024 * 1024,  # 上传单片大小:5MB
        "UPLOAD_MAX_WORKERS": 3  # 上传并发线程数:3
    },
    # --------[用户偏好:路径配置 + 业务偏好(账号/邮件开关由具体项目使用)]--------
    "user_info": {  # 用户偏好模块,对应 user_info.json
        "_comment": "【用户偏好配置】所有子目录均相对当前数据目录解析;绝对路径也会归一为数据目录下的相对路径;mail_dir/log_dir只配目录,文件名固定(mail_body.txt/app.log);enable_mail邮件总开关;accounts账号列表",  # 人读注释字段,加载时剔除
        # ---- 可持久化存储路径(全部相对当前数据目录;配置目录/日志目录改动需重启) ----
        "cookie_dir": "cookies",  # Cookie 目录:<数据目录>/cookies
        "account_dir": "accounts",  # 账号目录:<数据目录>/accounts
        "checkin_record_dir": "checkin_record",  # 签到记录目录:<数据目录>/checkin_record
        "download_dir": "downloads",  # 下载目录:<数据目录>/downloads
        "config_root_dir": "config",  # 配置目录:<数据目录>/config
        "mail_dir": "mail_result",  # 邮件目录:<数据目录>/mail_result
        "log_dir": "logs",  # 日志目录:<数据目录>/logs
        # ---- 业务偏好(由具体项目使用,配置层只负责透传) ----
        "enable_mail": True,  # 邮件通知总开关,默认开启
        "accounts": []  # 账号邮箱列表,界面增删后整体回写
    },
}

# 配置模块定位清单工厂模板:清单文件名固定,模块文件名由清单统一管理
CONFIG_MANIFEST_FILENAME = "config_manifest.json"
BUILTIN_CONFIG_MANIFEST: Dict[str, Any] = {
    "_comment": "配置模块定位清单;主程序负责生成维护,更新助手只读",
    "schema_version": 1,
    "modules": {
        "version": "version.json", "github": "github.json", "smtp": "smtp.json",
        "wecom": "wecom.json", "proxy": "proxy.json", "file_io": "file_io.json",
        "user_info": "user_info.json",
        "transfer_github": "transfer_github.json",  # 文件传输下载器专用GitHub配置(与自更新分离)
    },
}
_CONFIG_MANIFEST_DEFAULTS: Dict[str, str] = dict(BUILTIN_CONFIG_MANIFEST["modules"])


def _load_or_create_config_manifest() -> Dict[str, str]:
    """读取或创建配置模块定位清单,返回经过安全校验的模块文件映射."""
    os.makedirs(DATA_CONFIG_ROOT, exist_ok=True)
    manifest_path = os.path.join(DATA_CONFIG_ROOT, CONFIG_MANIFEST_FILENAME)
    raw_modules: Dict[str, Any] = {}
    try:
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("modules"), dict):
                raw_modules = raw["modules"]
    except (OSError, json.JSONDecodeError, ValueError):
        raw_modules = {}
    modules = {
        name: os.path.basename(str(raw_modules.get(name, default_name)).strip()) or default_name
        for name, default_name in _CONFIG_MANIFEST_DEFAULTS.items()
    }
    manifest = dict(BUILTIN_CONFIG_MANIFEST)              # 从工厂模板复制清单基础结构
    manifest["modules"] = modules                        # 写入校验并补齐后的模块映射
    if raw_modules != modules or not os.path.isfile(manifest_path):
        with _IO_LOCK:
            _atomic_write_json(manifest_path, manifest)
    return modules


# 模块名与JSON文件名映射由config_manifest.json加载
MODULE_FILE_MAP: Dict[str, str] = _load_or_create_config_manifest()
# 软件运行日志固定文件名(界面只配目录,文件名写死)
APP_LOG_FILENAME = "app.log"  # 日志目录下的固定日志文件名
# ==============================================================================
# [2. 目录路径工具函数]
# ==============================================================================
def get_data_store_root() -> str:
    """获取当前生效的数据总目录.

    :return: 数据总目录绝对路径(cookies/accounts/记录/下载/配置/日志均在其下)
    :rtype: str
    """
    return DATA_STORE_ROOT  # 返回启动期解析定型的总目录


def set_data_root_pointer(new_root: str) -> str:
    """写入数据目录指针(程序目录下),下次启动按新数据目录加载并自动迁移数据.

    :param new_root: 新数据目录(相对路径锚定程序目录,绝对路径原样使用)
    :type new_root: str
    :return: 指针文件完整路径
    :rtype: str
    """
    abs_root = _to_abs_path(new_root)  # 先统一转换为绝对路径
    os.makedirs(abs_root, exist_ok=True)  # 确保新目录存在(已存在不报错)
    payload = {  # 指针文件内容,同时记录上一站用于多次切换的迁移接力
        "_comment": "数据目录指针,由界面'路径配置-数据目录'生成;删除本文件即恢复出厂data_store目录",  # 人读注释
        "data_dir": abs_root,  # 下次启动要使用的数据目录
        "previous_data_dir": DATA_STORE_ROOT,  # 当前(旧)数据目录,作为下次迁移源
    }
    with _IO_LOCK:  # 与其他落盘操作串行,避免并发写竞争
        _atomic_write_json(DATA_ROOT_POINTER_FILE, payload)  # 原子写入程序目录下的指针文件
    return DATA_ROOT_POINTER_FILE  # 返回指针文件路径供界面提示


def _read_data_root_pointer() -> Dict[str, Any]:
    """读取数据目录指针内容(供迁移逻辑取 previous_data_dir).

    :return: 指针字典;文件不存在或损坏时返回空字典
    :rtype: Dict[str, Any]
    """
    try:
        if os.path.exists(DATA_ROOT_POINTER_FILE):  # 指针存在才读取
            with open(DATA_ROOT_POINTER_FILE, "r", encoding="utf-8") as f:  # UTF-8 打开
                return json.load(f)  # 返回解析后的指针字典
    except (json.JSONDecodeError, OSError, ValueError):  # 捕获指针文件读取/解析的全部异常
        pass  # 损坏/缺失统一按"无指针"处理
    return {}  # 兜底空字典


def migrate_data_store_if_needed() -> Optional[str]:
    """启动早期一次性迁移:总目录指向自定义位置且目标尚未初始化时,把旧总目录整体复制过去.

    - 必须在日志FileHandler绑定之前调用(旧日志文件未被本进程占用)
    - 复制而非移动:旧目录保留,用户确认无误后可自行删除
    - 迁移源优先取指针记录的previous_data_dir(支持多次切换接力),其次出厂data_store
    - 目标已含迁移标记则跳过,避免重复迁移覆盖新数据

    :return: 实际执行迁移时返回源目录路径;无需迁移返回None
    :rtype: Optional[str]
    """
    target = os.path.abspath(DATA_STORE_ROOT)  # 目标总目录绝对路径
    builtin = os.path.abspath(BUILTIN_STORE_DIR)  # 出厂目录绝对路径
    marker = os.path.join(DATA_STORE_ROOT, ".data_migrated")  # 迁移完成标记文件路径
    if os.path.exists(marker):  # 标记存在说明此前已迁移并初始化过
        return None  # 跳过,避免重复合并覆盖用户新数据

    # macOS 旧版出厂位置:历史上数据目录生成在 .app 包同级 data_store(如 /Applications/data_store)
    # 升级到新默认(Application Support)后需把旧数据一并迁移过来
    legacy_src = ""
    if sys.platform == "darwin":
        legacy = os.path.abspath(os.path.join(APP_BASE_DIR, "data_store"))
        if os.path.normcase(legacy) != os.path.normcase(target) \
                and os.path.isdir(legacy) and os.listdir(legacy):
            legacy_src = legacy
    if os.path.normcase(target) == os.path.normcase(builtin) and not legacy_src:  # 出厂目录且无旧数据:无需迁移
        return None  # 目标就是出厂目录:首次运行/未自定义,无需迁移

    os.makedirs(DATA_STORE_ROOT, exist_ok=True)  # 确保目标根目录存在(已存在不报错),为后续复制做准备
    # 确定迁移源:指针上一站 -> 出厂目录;源必须真实存在且非空
    src = ""  # 迁移源,初始为空,待后续确定有效源后赋值
    prev = str(_read_data_root_pointer().get("previous_data_dir", "")).strip()  # 取指针记录的上一站数据目录
    if prev:  # 记录了上一站则优先校验它
        prev_abs = os.path.abspath(_to_abs_path(prev))  # 上一站转绝对路径
        # 以下续行不能加行尾注释(续行符\\):上一站不能等于目标,且必须真实存在且非空目录
        if os.path.normcase(prev_abs) != os.path.normcase(target) \
                and os.path.isdir(prev_abs) and os.listdir(prev_abs):
            src = prev_abs  # 上一站有效,作为迁移源(支持多次切换接力)
    if not src and legacy_src:  # macOS 旧版出厂目录存在历史数据则迁移
        src = legacy_src  # 旧版出厂位置作为迁移源
    if not src and os.path.normcase(target) != os.path.normcase(builtin) \
            and os.path.isdir(builtin) and os.listdir(builtin):  # 上一站无效时回退出厂目录,同样要求非空
        src = builtin  # 出厂目录作为迁移源

    if src:  # 确定了有效迁移源才执行复制
        # dirs_exist_ok:目标已有ensure时创建的空目录结构也能合并复制
        shutil.copytree(src, DATA_STORE_ROOT, dirs_exist_ok=True)  # 整体复制(非移动),旧目录保留

    marker_payload = {  # 无论是否实际复制,都写标记表示该目标已初始化
        "_comment": "数据目录迁移完成标记,勿手动删除(删除后重启会再次尝试从旧目录合并)",  # 人读注释
        "migrated_from": src,  # 实际迁移源(为空表示无历史数据)
        "target": DATA_STORE_ROOT,  # 迁移目标目录
    }
    _atomic_write_json(marker, marker_payload)  # 写入迁移标记(原子写)
    return src or None  # 返回源路径;空串归一为 None


def get_store_sub_paths() -> Dict[str, str]:
    """获取当前生效的各业务存储路径(已应用界面配置).

    :return: 运行时路径字典的副本(避免外部直接修改内部状态)
    :rtype: Dict[str, str]
    """
    with _PATH_LOCK:  # 加锁拷贝,与 apply_path_settings 的刷新互斥
        return dict(_RUNTIME_PATHS)  # 返回浅副本


def _resolve_under_data_root(raw: Any, data_root: Optional[str] = None) -> str:
    """把子目录配置统一解析到指定数据目录下,并兼容旧版data_store前缀.

    新规则要求所有子目录都位于数据目录内部.绝对路径若在数据目录内则先转为
    相对路径;旧版 ``./data_store/xxx`` 会剥离首段data_store,避免生成
    ``data_store/data_store/xxx``.若旧绝对路径位于数据目录外,仅取最后一级目录名,
    保证运行时仍收敛到当前数据目录之下.

    :param raw: user_info.json中的子目录原始值
    :param data_root: 数据目录绝对路径,缺省使用当前DATA_STORE_ROOT
    :return: 位于数据目录下的规范化绝对路径;空值返回空串
    :rtype: str
    """
    root = os.path.abspath(data_root or DATA_STORE_ROOT)  # 当前唯一数据根目录
    value = os.path.expanduser(str(raw or "").strip().strip('"'))  # 清理路径文本
    if not value:  # 空值由调用方回退默认路径
        return ""
    if os.path.isabs(value):  # 绝对路径先尝试转成相对当前数据目录的路径
        try:
            relative = os.path.relpath(os.path.normpath(value), root)
        except ValueError:  # Windows跨盘符无法计算relpath时只保留目录末级名称
            relative = os.path.basename(os.path.normpath(value))
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            relative = os.path.basename(os.path.normpath(value))  # 禁止子目录逃逸数据根目录
    else:
        relative = os.path.normpath(value)                # 相对路径先消除./和重复分隔符
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    if parts and parts[0].lower() == "data_store":       # 兼容旧配置的./data_store/xxx写法
        parts = parts[1:]
    safe_relative = os.path.join(*parts) if parts else ""  # 重新按当前系统分隔符拼接
    if not safe_relative or safe_relative == os.pardir or safe_relative.startswith(os.pardir + os.sep):
        return ""                                         # 拒绝空目录及仍可能越界的父级路径
    return os.path.abspath(os.path.join(root, safe_relative))  # 最终路径强制位于数据目录下


def apply_path_settings(user_cfg: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """根据user_info.json中的路径配置刷新运行时有效路径,并确保目录存在.

    合并规则:以 _default_paths() 标准布局为底,所有非空子目录均以DATA_STORE_ROOT
    为唯一根目录拼接;CONFIG_ROOT始终以启动期解析结果为准(配置目录变化需重启).

    :param user_cfg: user_info模块配置(可为None,此时全部沿用默认)
    :type user_cfg: Optional[Dict[str, Any]]
    :return: 刷新后的有效路径字典(副本)
    :rtype: Dict[str, str]
    """
    effective = _default_paths()  # 先取出厂默认路径作为基底
    if user_cfg:  # 有用户配置才逐项覆盖
        for cfg_key, slot in _PATH_KEY_MAP.items():  # 遍历 配置键->运行时槽位 映射
            raw = str(user_cfg.get(cfg_key, "")).strip()  # 读取并规范化user_info中的路径文本
            resolved = _resolve_under_data_root(raw)       # 无论输入格式如何都收敛到数据目录下
            if resolved:  # 仅非空且合法的解析结果覆盖标准默认值
                effective[slot] = resolved                 # 写入运行时使用的绝对路径
    # CONFIG_ROOT以启动时解析结果为准(界面切换配置目录需重启,由指针文件承载)
    effective["CONFIG_ROOT"] = DATA_CONFIG_ROOT  # 强制回填启动期配置目录,防止被相对路径默认覆盖
    with _PATH_LOCK:  # 加锁刷新全局运行时路径
        _RUNTIME_PATHS.clear()  # 清空旧值
        _RUNTIME_PATHS.update(effective)  # 整体替换为新生效路径
    ensure_all_dir()  # 按新路径确保全部目录存在
    return get_store_sub_paths()  # 返回刷新后的副本


def get_log_file_path() -> str:
    """获取软件运行日志文件的当前生效完整路径.

    :return: 日志目录/app.log 的完整路径(文件名固定)
    :rtype: str
    """
    return os.path.join(get_store_sub_paths()["LOG_DIR"], APP_LOG_FILENAME)  # 运行时日志目录 + 固定文件名


def resolve_log_file_early() -> str:
    """日志初始化早期专用:在全量配置加载之前直接读user_info.json解析日志路径.

    此时MainModel尚未创建,不能走load_and_merge_all_config();
    文件缺失/损坏/值为空时一律回退默认日志路径,保证日志系统永远可启动.

    :return: 日志文件完整路径
    :rtype: str
    """
    fp = os.path.join(DATA_CONFIG_ROOT, MODULE_FILE_MAP["user_info"])  # 直接拼出 user_info.json 路径
    try:
        with open(fp, "r", encoding="utf-8") as f:  # 提前单独读取该文件
            raw = json.load(f)  # 解析 JSON
        raw_log_dir = str(raw.get("log_dir", "")).strip()  # 取 log_dir 配置
        if raw_log_dir:  # 配置了日志目录时按与apply_path_settings相同的规则解析
            log_dir = _resolve_under_data_root(raw_log_dir)  # 日志目录同样强制位于当前数据目录下
            if log_dir:  # 路径合法时拼接固定日志文件名
                return os.path.join(log_dir, APP_LOG_FILENAME)  # 最终日志路径:<数据目录>/<日志子目录>/app.log
    except (json.JSONDecodeError, OSError, ValueError):  # 捕获文件不存在/损坏/解析失败等全部异常
        pass  # 任何异常都静默走默认路径,保证日志可启动
    return os.path.join(_default_paths()["LOG_DIR"], APP_LOG_FILENAME)  # 兜底:默认日志目录/app.log


def ensure_all_dir() -> None:
    """程序入口调用:按当前生效路径一次性创建全部持久化存储目录.

    :return: 无返回值
    :rtype: None
    """
    paths = get_store_sub_paths()  # 取当前生效路径快照
    # 按固定顺序确保七个业务目录全部存在(exist_ok 兼容已存在)
    for key in ("COOKIE_DIR", "ACCOUNT_DIR", "CHECKIN_RECORD_DIR",
                "DOWNLOAD_SAVE_ROOT", "CONFIG_ROOT", "MAIL_DIR", "LOG_DIR"):
        os.makedirs(paths[key], exist_ok=True)  # 递归创建目录,已存在不报错


# ==============================================================================
# [3. 版本相关工具函数]
# ==============================================================================
def semver_compare(v1: str, v2: str) -> int:
    """
    简单语义化版本比较(按点分段逐段比较整数,缺位补0).

    :param v1: 版本字符串例如 "0.1.0"(可带 v/V 前缀)
    :type v1: str
    :param v2: 版本字符串例如 "0.2.0"(可带 v/V 前缀)
    :type v2: str
    :return: 1 v1更新;0相等;-1 v2更新
    :rtype: int
    """

    def _parse(ver: str):
        """把单个版本字符串拆成整数段列表.

        :param ver: 原始版本字符串
        :type ver: str
        :return: 各段整数,非数字段按 0 处理,如 "v1.2" -> [1,2]
        :rtype: list
        """
        parts = []  # 收集各段整数
        # 去掉版本前缀 v/V
        ver = ver.strip()  # 去首尾空白
        if ver.startswith(("v", "V")):  # 兼容 v1.0.0 / V1.0.0 写法
            ver = ver[1:]  # 截掉首字符前缀
        for s in ver.split("."):  # 按点拆分为主/次/修订段
            try:
                parts.append(int(s))  # 数字段转整数
            except ValueError:
                parts.append(0)  # 非数字段(如 rc/beta)按 0 处理,保证不崩
        return parts  # 返回整数段列表

    a = _parse(v1)  # 解析左版本
    b = _parse(v2)  # 解析右版本
    max_len = max(len(a), len(b))  # 取较长段数对齐比较
    for i in range(max_len):  # 从主版本段开始逐段比较
        ai = a[i] if i < len(a) else 0  # 左侧缺位补 0
        bi = b[i] if i < len(b) else 0  # 右侧缺位补 0
        if ai > bi:  # 首个差异段即可判定大小
            return 1  # v1 更新
        elif ai < bi:
            return -1  # v2 更新
    return 0  # 全部段相等


def get_update_download_dir(version_cfg: Dict[str, Any]) -> str:
    """获取更新包保存完整目录.

    使用用户在路径配置中设置的 download_dir(运行时 DOWNLOAD_SAVE_ROOT),
    而非固定的 UPDATE_DOWNLOAD_SUB_DIR,确保用户配置的下载目录实际生效.
    version_cfg 参数保留以兼容调用方签名.

    :param version_cfg: version 模块配置(当前未使用,仅为兼容调用签名保留)
    :type version_cfg: Dict[str, Any]
    :return: 更新包保存目录绝对路径(即运行时下载根目录)
    :rtype: str
    """
    return get_store_sub_paths()["DOWNLOAD_SAVE_ROOT"]  # 以界面配置的下载目录为准


def render_release_page_url(version_cfg: Dict[str, Any], github_cfg: Dict[str, Any]) -> str:
    """渲染release发布页面浏览器访问地址.

    仓库信息统一从 github.json 读取,地址模板从 version 配置读取.

    :param version_cfg: version 模块配置,取 UPDATE_RELEASE_PAGE_TPL 模板
    :type version_cfg: Dict[str, Any]
    :param github_cfg: github 模块配置,取仓库 owner/repo
    :type github_cfg: Dict[str, Any]
    :return: 可直接用浏览器打开的 releases 页面 URL
    :rtype: str
    """
    tpl = version_cfg["UPDATE_RELEASE_PAGE_TPL"]  # 取含 {owner}/{repo} 占位符的模板
    return tpl.format(owner=github_cfg["GITHUB_REPO_OWNER"], repo=github_cfg["GITHUB_REPO_NAME"])  # 填充仓库信息


def render_update_save_filename(version_cfg: Dict[str, Any], tag: str = "", installed: bool = False) -> str:
    """根据当前平台渲染更新包文件名,匹配Release资产名.

    平台映射:
        Windows -> wuge_tools-Windows.zip(绿色版) / wuge_tools-Windows-setup.exe(安装版)
        Linux   -> wuge_tools-Linux.tar.gz
        macOS arm64  -> wuge_tools-macOS-arm64.dmg
        macOS x86_64 -> wuge_tools-macOS-x86_64.dmg

    :param version_cfg: version 模块配置,取 UPDATE_SAVE_FILENAME_TPL / UPDATE_SETUP_FILENAME_TPL 模板
    :type version_cfg: Dict[str, Any]
    :param tag: 版本标签(预留占位符,默认为空串)
    :type tag: str
    :param installed: 是否安装版(仅 Windows 生效);安装版匹配 setup.exe 安装包
    :type installed: bool
    :return: 渲染后的更新包文件名
    :rtype: str
    """
    import platform as _pf  # 函数内导入标准库 platform,避免顶层无谓依赖
    system = _pf.system()  # 取操作系统标识:Windows/Darwin/Linux
    if system == "Windows":  # Windows 系统
        plat = "Windows"
    elif system == "Darwin":  # macOS 的 system() 返回 Darwin
        # macOS 区分 CPU 架构:Apple Silicon(arm64)与 Intel(x86_64),需下载对应架构的更新包
        # platform.machine() 返回 "arm64"(Apple Silicon)或 "x86_64"(Intel/Rosetta)
        arch = _pf.machine()
        plat = f"macOS-{arch}"  # 如 macOS-arm64 / macOS-x86_64
    else:  # 其余系统统一按 Linux 资产处理
        plat = "Linux"
    # 平台更新包格式契约: Windows=zip, Linux=tar.gz, macOS=dmg(原生磁盘镜像)
    if system == "Windows":
        archive_ext = "zip"
    elif system == "Darwin":
        archive_ext = "dmg"
    else:
        archive_ext = "tar.gz"
    # Windows 安装版: 改走 setup.exe 安装包模板(与绿色版 zip 区分)
    tpl = version_cfg["UPDATE_SAVE_FILENAME_TPL"]  # 取文件名模板(含 {tag}/{platform}/{archive_ext})
    if installed and system == "Windows":
        tpl = version_cfg.get("UPDATE_SETUP_FILENAME_TPL", "wuge_tools-{platform}-setup.exe")
    return tpl.format(tag=tag, platform=plat, archive_ext=archive_ext)  # 填充平台与格式后返回


# ==============================================================================
# [4. HTTP请求头组装工具函数]
# ==============================================================================
def build_service_headers(base_headers: Dict[str, str], extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    组装签到请求头:基础模板 + 业务传入额外headers(后者覆盖同名键).

    :param base_headers: 基础请求头字典(由调用方提供)
    :type base_headers: Dict[str, str]
    :param extra_headers: 当前接口需要追加/覆盖的自定义头,可为None
    :type extra_headers: Optional[Dict[str, str]]
    :return: 合并完成完整headers字典,传给底层http客户端
    :rtype: Dict[str, str]
    """
    final = base_headers.copy()  # 拷贝基础头,避免污染调用方原字典
    if extra_headers and isinstance(extra_headers, dict):  # 额外头非空且类型正确才合并
        final.update(extra_headers)  # 同名键以业务自定义头为准
    return final  # 返回合并后的完整请求头


# ==============================================================================
# [5. 多配置文件IO、合并核心逻辑]
# ==============================================================================
# 进程内写锁与原子写函数已在模块顶部定义,确保导入期清单初始化也可安全使用

def _strip_comment(d: Dict[str, Any]) -> Dict[str, Any]:
    """剔除json里面 _comment 注释key,返回新字典.

    注释字段仅给人阅读,程序运行不参与逻辑,因此加载合并前必须剥离.

    :param d: 原始字典(可能含 _comment 键)
    :type d: Dict[str, Any]
    :return: 去掉 _comment 键后的新字典(不改入参)
    :rtype: Dict[str, Any]
    """
    out = d.copy()  # 浅拷贝,避免修改原字典
    out.pop("_comment", None)  # 删除注释键,不存在也不报错
    return out  # 返回纯净字典


def _merge_module_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """模块字典浅合并:override存在key覆盖base,缺失保留base.

    :param base: 基底字典(出厂默认,已剥离注释)
    :type base: Dict[str, Any]
    :param override: 覆盖字典(用户 json 中的键)
    :type override: Dict[str, Any]
    :return: 合并后的新字典
    :rtype: Dict[str, Any]
    """
    res = base.copy()  # 从出厂默认拷贝起步
    for k, v in override.items():  # 遍历用户配置
        if k in res:  # 仅覆盖出厂默认中已声明的键(未知键忽略,保证配置面可控)
            res[k] = v  # 用户值覆盖默认值
    return res  # 返回合并结果


def _load_single_module(module_name: str) -> Dict[str, Any]:
    """加载单个模块配置;文件不存在生成模板;损坏回退内置默认.

    :param module_name: 模块名(必须存在于 MODULE_FILE_MAP / BUILTIN_MODULE_DEFAULTS)
    :type module_name: str
    :return: 剥离注释并与出厂默认合并后的有效配置字典
    :rtype: Dict[str, Any]
    :raises KeyError: 模块名未注册时由底层字典/映射访问抛出
    """
    filename = MODULE_FILE_MAP[module_name]  # 模块名 -> json 文件名
    fp = os.path.join(DATA_CONFIG_ROOT, filename)  # 拼出配置文件完整路径
    builtin_raw = BUILTIN_MODULE_DEFAULTS[module_name]  # 取出厂默认(带注释模板)
    builtin_clean = _strip_comment(builtin_raw)  # 剥离注释,得到运行用默认
    if not os.path.exists(fp):  # 用户配置文件不存在(首次运行)
        # 不存在,原子导出带注释的模板文件
        with _IO_LOCK:  # 加锁防止多线程同时判定不存在而重复导出
            if not os.path.exists(fp):  # 双重检查(锁内可能已被其他线程创建)
                _atomic_write_json(fp, builtin_raw)  # 导出带 _comment 的出厂模板供用户阅读修改
        _logger.info(f"📄 配置模块[{module_name}]文件不存在,已生成出厂模板:{fp}")  # 记录模板生成
        return builtin_clean  # 首次运行直接使用出厂默认
    try:
        with open(fp, "r", encoding="utf-8") as f:  # 读取用户配置
            user_raw = json.load(f)  # 解析 JSON
    except (json.JSONDecodeError, OSError) as e:  # 捕获JSON解析失败或文件读取IO异常
        # 文件读取出错,直接回退内置(必须留痕,否则"改了配置不生效"无法排查)
        _logger.warning(f"⚠ 配置模块[{module_name}]读取失败,本次回退出厂默认:{fp} | {type(e).__name__}:{e}")  # 警告留痕
        return builtin_clean  # 单模块损坏不影响其他模块,回退到出厂默认值
    user_clean = _strip_comment(user_raw)  # 剥离用户文件中的注释键
    merged = _merge_module_dict(builtin_clean, user_clean)  # 用户键覆盖默认键,缺失沿用默认
    return merged  # 返回合并结果


def load_and_merge_all_config() -> Dict[str, Dict[str, Any]]:
    """
    加载全部模块配置,逐个加载合并.
    顺序:先建目录→加载固定模块(含user_info)→应用界面路径配置.
    注:账号详情等业务数据由各自业务模块(runtime/model/account_store.py)独立加载,
        不混入通用配置合并结果.

    :return: 嵌套字典 cfg["version"] / cfg["github"] / cfg["smtp"] / cfg["proxy"] ...
    :rtype: Dict[str, Dict[str, Any]]
    """
    ensure_all_dir()  # 启动第一步:确保存储与配置目录存在
    final_cfg: Dict[str, Dict[str, Any]] = {}  # 汇总容器
    for mod_name in MODULE_FILE_MAP.keys():  # 按注册顺序逐模块加载(单模块损坏互不影响)
        final_cfg[mod_name] = _load_single_module(mod_name)  # 加载/导出/合并单个模块
    # user_info中的自定义路径生效后再创建自定义目录
    apply_path_settings(final_cfg.get("user_info"))  # 用界面路径配置刷新运行时路径并建目录
    _logger.info(
        f"📂 全局配置加载完成:配置目录={DATA_CONFIG_ROOT} | "
        f"共{len(MODULE_FILE_MAP)}个模块"
    )  # 记录加载结果与模块数量
    return final_cfg  # 返回全部模块的合并配置


# ---------------- 固定模块的通用读写(用于UI层) ----------------
def _read_module_json(module_name: str) -> dict:
    """简易读取单个固定模块配置(UI层读写用,区别于启动期的合并加载).

    :param module_name: 模块名
    :type module_name: str
    :return: 剥离注释后的模块配置;文件缺失会先生成模板,读取异常回退默认
    :rtype: dict
    :raises KeyError: module_name 不在 MODULE_FILE_MAP 时抛出
    """
    if module_name not in MODULE_FILE_MAP:  # 校验模块是否已注册
        raise KeyError(f"非固定配置模块:{module_name}")  # 未注册直接报错
    filename = MODULE_FILE_MAP[module_name]  # 取文件名
    fp = os.path.join(DATA_CONFIG_ROOT, filename)  # 拼完整路径
    default_cfg = BUILTIN_MODULE_DEFAULTS[module_name]  # 取出厂默认(带注释)
    if not os.path.exists(fp):  # 文件不存在
        _write_module_json(module_name, default_cfg)  # 先导出出厂模板
        return _strip_comment(default_cfg)  # 返回纯默认
    try:
        with open(fp, "r", encoding="utf-8") as f:  # 正常读取
            raw = json.load(f)  # 解析
        return _strip_comment(raw)  # 剥离注释后返回用户值
    except Exception:  # 捕获读取/解析过程中的任意异常
        return _strip_comment(default_cfg)  # 宽松回退出厂默认,保证界面可打开


def _write_module_json(module_name: str, data: dict):
    """简易写入单个固定模块配置;原子落盘并自动补回出厂_comment注释.

    :param module_name: 模块名
    :type module_name: str
    :param data: 待保存的模块配置(纯数据)
    :type data: dict
    :return: 无返回值
    :rtype: None
    :raises KeyError: module_name 不在 MODULE_FILE_MAP 时抛出
    """
    if module_name not in MODULE_FILE_MAP:  # 校验模块是否已注册
        raise KeyError(f"非固定配置模块:{module_name}")  # 未注册报错
    filename = MODULE_FILE_MAP[module_name]  # 取文件名
    fp = os.path.join(DATA_CONFIG_ROOT, filename)  # 拼完整路径
    payload = dict(data)  # 拷贝一份,避免污染调用方字典
    builtin_comment = BUILTIN_MODULE_DEFAULTS[module_name].get("_comment")  # 取该模块出厂人读注释
    if builtin_comment and not payload.get("_comment"):  # 有出厂注释且本次数据未自带注释
        # 注释提到首位,方便人阅读
        payload = {"_comment": builtin_comment, **payload}  # 字典展开把注释键放最前
    with _IO_LOCK:  # 落盘串行化
        _atomic_write_json(fp, payload)  # 原子写入
    _logger.debug(f"💾 配置模块[{module_name}]已原子落盘:{fp}")  # 调试级落盘记录


# ---------------- 配置目录指针(界面切换配置目录,重启生效) ----------------
def get_active_config_root() -> str:
    """获取当前实际生效的配置目录.

    :return: 启动期解析定型的配置目录绝对路径
    :rtype: str
    """
    return DATA_CONFIG_ROOT  # 返回常量(运行期不变)


def set_config_root_pointer(new_root: str) -> str:
    """写入配置目录指针文件,下次启动按新目录加载配置.

    :param new_root: 新配置子目录(始终约束在当前数据目录下)
    :type new_root: str
    :return: 指针文件完整路径
    :rtype: str
    """
    abs_root = _resolve_under_data_root(new_root, DATA_STORE_ROOT)  # 配置目录强制锚定数据总目录
    if not abs_root:  # 空值回退标准配置目录
        abs_root = os.path.join(DATA_STORE_ROOT, "config")
    os.makedirs(abs_root, exist_ok=True)  # 确保新配置目录存在
    os.makedirs(DEFAULT_STORE_DIR, exist_ok=True)  # 确保数据总目录存在(指针文件要放其下)
    payload = {  # 配置目录指针内容
        "_comment": "配置目录指针,由界面'路径配置-配置目录'生成;删除本文件即恢复默认配置目录",  # 人读注释
        "config_root": abs_root,  # 下次启动使用的配置目录
    }
    with _IO_LOCK:  # 落盘加锁
        _atomic_write_json(CONFIG_ROOT_POINTER_FILE, payload)  # 写入总目录内指针文件
    return CONFIG_ROOT_POINTER_FILE  # 返回指针路径


# ==================== UI层对外导出接口 ====================
def load_user_info() -> dict:
    """读取 user_info 用户偏好模块配置.

    :return: 剥离注释后的 user_info 配置字典
    :rtype: dict
    """
    return _read_module_json("user_info")  # 委托通用读取


def save_user_info(payload: dict):
    """增量保存 user_info 配置(读-改-写),并立即刷新运行时路径.

    :param payload: 需要更新的键值(仅这些键被合并,其余保持不变)
    :type payload: dict
    :return: 无返回值
    :rtype: None
    """
    cfg = _read_module_json("user_info")  # 先读当前完整配置
    cfg.update(payload)  # 用传入键值增量更新
    _write_module_json("user_info", cfg)  # 原子落盘
    # 路径类key保存后立即刷新运行时有效路径
    apply_path_settings(cfg)  # 保存后立刻生效(无需重启的路径项)
    path_keys = [k for k in payload if k in (  # 统计本次实际涉及的路径键,仅用于日志
        "cookie_dir", "account_dir", "checkin_record_dir", "download_dir",
        "mail_dir", "log_dir",
    )]  # 与界面六个可配目录键取交集
    _logger.info(
        f"💾 user_info配置已保存并刷新运行时路径(涉及路径项:{','.join(path_keys) or '无'})"
    )  # 记录保存结果,无路径项时显示"无"


def load_github_config() -> dict:
    """读取 github 模块配置.

    :return: 剥离注释后的 github 配置字典
    :rtype: dict
    """
    return _read_module_json("github")  # 委托通用读取


def save_github_config(payload: dict):
    """增量保存 github 模块配置(读-改-写).

    :param payload: 需要更新的 GitHub 键值
    :type payload: dict
    :return: 无返回值
    :rtype: None
    """
    cfg = _read_module_json("github")  # 读当前配置
    cfg.update(payload)  # 增量合并
    _write_module_json("github", cfg)  # 原子落盘


def load_transfer_github_config() -> dict:
    """读取文件传输下载器专用GitHub配置(transfer_github.json,与自更新github.json独立).

    :return: 剥离注释后的下载器GitHub配置字典
    :rtype: dict
    """
    return _read_module_json("transfer_github")  # 委托通用读取


def save_transfer_github_config(payload: dict):
    """保存文件传输下载器专用GitHub配置(增量读-改-写).

    :param payload: 需要更新的下载器GitHub键值
    :type payload: dict
    :return: 无返回值
    :rtype: None
    """
    cfg = _read_module_json("transfer_github")  # 读当前配置
    cfg.update(payload)  # 增量合并
    _write_module_json("transfer_github", cfg)  # 原子落盘


def load_proxy_config() -> dict:
    """读取 proxy 代理模块配置.

    :return: 剥离注释后的 proxy 配置字典
    :rtype: dict
    """
    return _read_module_json("proxy")  # 委托通用读取


def load_wecom_config() -> dict:
    """读取 wecom 企业微信推送模块配置.

    :return: 剥离注释后的 wecom 配置字典
    :rtype: dict
    """
    return _read_module_json("wecom")  # 委托通用读取


def save_wecom_config(payload: dict):
    """增量保存 wecom 企业微信推送配置(读-改-写).

    :param payload: 需要更新的企业微信推送键值
    :type payload: dict
    :return: 无返回值
    :rtype: None
    """
    cfg = _read_module_json("wecom")  # 读当前配置
    cfg.update(payload)  # 增量合并
    _write_module_json("wecom", cfg)  # 原子落盘


def save_proxy_config(payload: dict):
    """增量保存 proxy 代理模块配置(读-改-写).

    :param payload: 需要更新的代理键值
    :type payload: dict
    :return: 无返回值
    :rtype: None
    """
    cfg = _read_module_json("proxy")  # 读当前配置
    cfg.update(payload)  # 增量合并
    _write_module_json("proxy", cfg)  # 原子落盘


def save_file_io_config(payload: dict):
    """保存文件IO配置(下载/上传分片大小与并发数).

    :param payload: 需要更新的文件IO键值
    :type payload: dict
    :return: 无返回值
    :rtype: None
    """
    cfg = _read_module_json("file_io")  # 读当前配置
    cfg.update(payload)  # 增量合并
    _write_module_json("file_io", cfg)  # 原子落盘


def _with_proxy_scheme(addr: str) -> str:
    """为手动代理地址补全 scheme(缺省时补 http://).

    :param addr: 代理地址,如 "127.0.0.1" 或 "http://127.0.0.1"
    :type addr: str
    :return: 带 scheme 的地址
    :rtype: str
    """
    addr = (addr or "").strip()  # 防空并去空白
    return addr if "://" in addr else f"http://{addr}"  # 已含协议头则原样返回,否则补 http://


def resolve_effective_proxy(proxy_cfg: dict):
    """
    统一解析"当前请求实际应使用的代理"[签到/版本检查/更新下载等所有调用方唯一入口].
    优先级:
      1. AUTO_DETECT_SYSTEM_PROXY=true -> 实时检测系统代理,系统没开则不代理(忽略手动开关)
      2. 否则 SERVICE_DEFAULT_USE_PROXY=true -> 使用手动 http/https 地址+端口
      3. 否则不代理

    :param proxy_cfg: proxy 模块配置字典,可为 None/空
    :type proxy_cfg: dict
    :return: (proxies, source_desc); proxies 为 {"http":url,"https":url} 或 None
    :rtype: tuple
    """
    proxy_cfg = proxy_cfg or {}  # None 归一为空字典,避免 .get 异常

    if proxy_cfg.get("AUTO_DETECT_SYSTEM_PROXY", False):  # 第一优先级:自动跟随系统代理开关
        # 函数内import: infrastructure 不反向依赖本模块,此处仅单向引用
        from infrastructure.system_proxy import detect_system_proxy  # 延迟导入系统代理检测
        proxies, source = detect_system_proxy()  # 实时检测,返回代理字典与来源描述
        if proxies:  # 系统确实开启了代理
            # SOCKS代理需要PySocks依赖;缺失则降级直连,避免requests抛Missing dependencies for SOCKS support
            proxy_url = list(proxies.values())[0] if proxies else ""  # 取任一代理URL判断协议类型
            if proxy_url.startswith("socks"):  # SOCKS 代理需要额外依赖
                try:  # 尝试导入PySocks以检测是否支持SOCKS代理
                    import socks  # noqa: F401;仅尝试导入以确认 PySocks 已安装
                except ImportError:  # 捕获PySocks未安装的导入异常
                    _logger.warning(f"⚠ 系统代理为SOCKS({proxy_url})但未安装PySocks,本次直连")  # 缺依赖告警
                    return None, f"自动跟随-{source},SOCKS缺少PySocks降级直连"  # 降级直连并说明原因
            _logger.debug(f"🌐 代理决策:自动跟随系统代理({source}) -> {proxies}")  # 记录代理选择
            return proxies, f"自动跟随-{source}"  # 返回系统代理
        _logger.debug(f"ℹ️ 代理决策:自动跟随系统代理({source}),但系统未开启代理,本次直连")  # 开关开但系统没开代理
        return None, f"自动跟随-{source},系统未开启代理"  # 直连

    if proxy_cfg.get("SERVICE_DEFAULT_USE_PROXY", False):  # 第二优先级:手动代理开关
        http_addr = proxy_cfg.get("DEFAULT_HTTP_PROXY", "127.0.0.1")  # 手动 HTTP 地址,缺省本机
        https_addr = proxy_cfg.get("DEFAULT_HTTPS_PROXY", "127.0.0.1")  # 手动 HTTPS 地址,缺省本机
        port = proxy_cfg.get("DEFAULT_PROXY_PORT", 38457)  # 手动端口,缺省 38457
        def with_port(address: str) -> str:
            """为手动代理补端口;完整URL已含端口时避免重复追加."""
            url = _with_proxy_scheme(address)
            try:
                return url if urlsplit(url).port else f"{url}:{port}"
            except ValueError:
                return f"{url}:{port}"

        proxies = {  # 组装 requests 可用的代理字典
            "http": with_port(http_addr),  # HTTP/SOCKS完整URL或主机+独立端口
            "https": with_port(https_addr),  # HTTPS/SOCKS完整URL或主机+独立端口
        }
        # 手动配置若为SOCKS代理,同样需要PySocks
        proxy_url = list(proxies.values())[0] if proxies else ""  # 取URL判断是否 SOCKS
        if proxy_url.startswith("socks"):  # 手动填了 socks 地址
            try:  # 尝试导入PySocks以检测是否支持SOCKS代理
                import socks  # noqa: F401;仅尝试导入以确认 PySocks 已安装
            except ImportError:  # 捕获PySocks未安装的导入异常
                _logger.warning(f"⚠ 手动代理为SOCKS({proxy_url})但未安装PySocks,本次直连")  # 缺依赖告警
                return None, "手动SOCKS代理缺少PySocks降级直连"  # 降级直连
        _logger.debug(f"🌐 代理决策:使用手动配置代理 -> {proxies}")  # 记录手动代理
        return proxies, "手动配置代理"  # 返回手动代理

    _logger.debug("⛔ 代理决策:代理已关闭,本次直连")  # 两个开关均未启用
    return None, "代理已关闭"  # 直连
