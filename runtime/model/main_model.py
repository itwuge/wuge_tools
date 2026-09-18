# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: main_model.py
# 归属: runtime/model 业务模型层(MVC-M) —— 主窗口数据模型
# ------------------------------------------------------------------------------
# 文件用途:
#   主窗口数据模型 MainModel——加载并持有全局合并配置,组装/保存界面表单数据,
#   管理账号列表、账号详情/凭证以及签到统计;纯数据与业务状态,完全无 Qt/UI 依赖.
# ------------------------------------------------------------------------------
# 架构定位:
#   MVC 中的 Model 角色,被 controller 调用,管理配置/账号数据;不依赖 view,
#   不 import 任何 PySide6 模块,不感知 View/Controller;底层 JSON 读写统一委托
#   app_config 与 account_store,本层只做键名映射与数据整形.
#
#   关联组件:
#     - 上游: controller
#     - 下游: runtime.model.app_config、runtime.model.account_store、
#             infrastructure.system_proxy(函数内延迟导入)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 启动加载并缓存全局合并配置(version/github/smtp/proxy/file_io/user_info 等模块)
#   2. 主窗口表单展示数据组装与路径/邮件/GitHub/代理/文件IO 配置保存
#   3. 账号列表增删、登录密码/凭证读写、账号详情持久化与跨天归一化
#   4. 账号签到统计(账号总数/今日已签数/选中账号今日收益)
#   5. 数据目录/配置目录/日志目录变更的重启判定与指针写入
#   6. 系统代理实时检测能力透传给界面
# ------------------------------------------------------------------------------
# 职责边界:
#   - 本层不直接操作文件;通过 app_config 原子读写各 JSON 配置模块,
#     通过 account_store 读写账号详情与登录凭证文件
#   - 不 import 任何 PySide6 模块,不感知 View/Controller
#   - 仅做键名映射与数据整形,不实现业务算法
# ------------------------------------------------------------------------------
# 线程模型:
#   配置读写通过 app_config 的全局 IO 锁保护并发安全;
#   数据组装为纯计算无状态,可在任意线程调用;
#   系统代理检测涉及阻塞 IO,由界面层在工作线程调用.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、os、re、datetime、typing
#   - 第三方: 无(函数内按需 import infrastructure.system_proxy)
#   - 项目内: runtime.model.app_config、runtime.model.account_store、
#             infrastructure.system_proxy(函数内延迟导入)
# ==============================================================================
# =========** [MainModel]Model =========
import logging  # 标准库:模型层运行日志
import os  # 标准库:删除指针文件、路径比较与拼接
import re  # 标准库:正则提取签到时间中的日期部分
from datetime import datetime  # 标准库:日期解析与"今天"比对(跨天判定)
from typing import Any, Dict, List, Optional  # 标准库:类型注解

_logger = logging.getLogger("MainModel")  # 本模型专用logger,名称MainModel

from runtime.model.app_config import (  # 从通用配置层导入配置读写/路径/指针能力
    CONFIG_ROOT_POINTER_FILE,  # 配置目录指针文件路径(数据目录变更时需删除)
    _read_module_json,  # 内部接口:读取单个固定模块 JSON(smtp/file_io/version)
    _resolve_under_data_root,  # 子目录配置统一锚定当前数据目录解析
    _to_abs_path,  # 数据目录等根路径锚定程序目录转绝对路径
    _write_module_json,  # 内部接口:原子写入单个固定模块 JSON
    get_active_config_root,  # 获取当前生效配置目录(变更比较用)
    get_data_store_root,  # 获取当前生效数据总目录(变更比较用)
    get_store_sub_paths,  # 获取运行时各业务有效路径
    load_and_merge_all_config,  # 启动期加载并合并全部配置模块
    load_github_config,  # 读取 github 模块
    load_proxy_config,  # 读取 proxy 模块
    load_user_info,  # 读取 user_info 用户偏好模块
    load_wecom_config,  # 读取 wecom 企业微信推送模块
    save_file_io_config,  # 保存文件IO(分片/并发)配置
    save_github_config,  # 保存 github 配置
    save_proxy_config,  # 保存 proxy 配置
    save_user_info,  # 增量保存 user_info(含账号列表/路径)
    save_wecom_config,  # 保存 wecom 企业微信推送配置
    set_config_root_pointer,  # 写配置目录指针(重启生效)
    set_data_root_pointer,  # 写数据目录指针(重启迁移)
)
from runtime.model.account_store import (  # 从账号存储层导入账号详情/凭证读写能力
    get_account_password,  # 读取单个账号已保存的登录密码
    load_account_credentials,  # 批量加载账号登录凭证字典
    load_account_detail,  # 加载全部账号详情快照
    remove_account_credential,  # 删除单个账号登录凭证
    remove_account_detail,  # 删除单个账号详情
    save_account_detail,  # 保存单个账号详情快照
    save_account_password,  # 保存单个账号登录密码
)

class ModelBusinessError(Exception):
    """Model层业务异常(供 controller 捕获并提示).

    Attributes:
        code: 错误码
        message: 错误描述
    """

    def __init__(self, code: int, message: str):
        """初始化业务异常.

        :param code: 业务错误码(由调用方约定含义)
        :type code: int
        :param message: 面向用户/日志的错误描述
        :type message: str
        """
        self.code = code  # 保存错误码
        self.message = message  # 保存错误描述
        super().__init__(self.message)  # 同步给基类 Exception,保证 str(e) 可用


class MainModel:
    """主窗口数据模型.

    持有全局合并配置,为 controller/view 提供表单数据组装、配置保存、
    账号与签到统计等纯数据能力,全程不依赖 Qt.

    Attributes:
        config: 全局合并配置 cfg["version"]/cfg["smtp"]/...
    """

    def __init__(self):
        """构造模型:加载并缓存全局合并配置,输出初始化日志.

        :return: 无返回值
        :rtype: None
        """
        # 全局配置供Worker使用(网络/smtp/版本等原始key)
        _logger.info("🚀 MainModel开始加载全局配置...")  # 标记模型初始化开始
        self.config: Dict[str, Dict[str, Any]] = load_and_merge_all_config()  # 加载并持有全部模块合并配置
        _logger.info(
            f"🧩 MainModel初始化完成:本地版本={self.get_local_version() or '(未配置)'},"
            f"账号数={len(self.config.get('user_info', {}).get('accounts', []))}"
        )  # 输出版本与账号数量,便于启动排查

    # ------------------------------------------------------------------
    # 全局配置
    # ------------------------------------------------------------------
    def get_global_config(self) -> Dict[str, Dict[str, Any]]:
        """获取内存中的全局合并配置(供 Worker 取网络/smtp/版本等原始键).

        :return: 全部模块的嵌套配置字典
        :rtype: Dict[str, Dict[str, Any]]
        """
        return self.config  # 直接返回缓存配置

    def get_local_version(self) -> str:
        """获取本地程序版本号.

        :return: version 模块中的 APP_VERSION;未配置时返回空串
        :rtype: str
        """
        return str(self.config.get("version", {}).get("APP_VERSION", ""))  # 安全链式取值并转字符串

    def is_auto_check_update_enabled(self) -> bool:
        """启动时是否自动静默检测新版本(默认开启;version.json缺失/非bool按开启处理).

        :return: True 表示启动后自动检测更新
        :rtype: bool
        """
        return bool(self.config.get("version", {}).get("AUTO_CHECK_UPDATE_ON_START", True))  # 缺省 True(开启)

    def update_local_version(self, new_version: str) -> None:
        """下载完成后写入最新版本号到version.json并刷新内存配置.

        :param new_version: 新版本号字符串
        :type new_version: str
        :return: 无返回值
        :rtype: None
        """
        _logger.info(f"🔖 写入新本地版本号:{self.get_local_version()} -> {new_version}")  # 记录版本变更轨迹
        version_cfg = _read_module_json("version")  # 读当前 version 模块
        version_cfg["APP_VERSION"] = new_version  # 更新版本号键
        _write_module_json("version", version_cfg)  # 原子落盘
        self._refresh_config()  # 重新加载合并配置,使内存立即生效

    # ------------------------------------------------------------------
    # 主窗口表单:展示数据组装(返回View.fill_form_data所需结构)
    # ------------------------------------------------------------------
    def load_form_data(self) -> Dict[str, Any]:
        """组装主窗口全部表单所需的展示数据(一次性读取各模块).

        :return: 含 paths/mail/github/file_io/proxy/accounts/summary 分组的字典,
                 供 View.fill_form_data 直接填充
        :rtype: Dict[str, Any]
        """
        user_cfg = load_user_info()  # 用户偏好(邮件开关/账号列表等)
        github_cfg = load_github_config()  # GitHub 仓库与 token
        proxy_cfg = load_proxy_config()  # 代理设置
        smtp_cfg = _read_module_json("smtp")  # SMTP 发件/收件配置
        wecom_cfg = load_wecom_config()  # 企业微信推送配置
        file_io_cfg = _read_module_json("file_io")  # 上传下载分片与并发

        to_list = smtp_cfg.get("SMTP_DEFAULT_TO_LIST", [])  # 收件人列表(可能多个)
        receive_email = to_list[0] if to_list else ""  # 界面只有一个收件人输入框,取列表首项回填
        eff_paths = get_store_sub_paths()  # 获取当前运行时已解析的各业务目录绝对路径
        data_root = get_data_store_root()  # 数据总目录在界面中始终按绝对路径展示

        def relative(path: str) -> str:
            """将业务目录转换为相对数据总目录的展示值.

            Windows跨盘符时 os.path.relpath 会抛 ValueError,此时保留绝对路径保证可用性.
            """
            try:
                return os.path.relpath(path, data_root)    # 正常情况返回相对数据目录的路径
            except ValueError:
                return path                               # 跨盘符等情况回退原绝对路径

        return {
            "paths": {  # 路径分组:数据目录为全路径,其余目录优先给相对路径
                "cookie_dir": relative(eff_paths["COOKIE_DIR"]),  # Cookie目录相对路径
                "account_dir": relative(eff_paths["ACCOUNT_DIR"]),  # 账号目录相对路径
                "checkin_record_dir": relative(eff_paths["CHECKIN_RECORD_DIR"]),  # 签到记录目录相对路径
                "download_dir": relative(eff_paths["DOWNLOAD_SAVE_ROOT"]),  # 下载目录相对路径
                "data_root": data_root,                  # 数据总目录完整绝对路径
                "config_root_dir": relative(eff_paths["CONFIG_ROOT"]),  # 配置目录相对路径
                "mail_dir": relative(eff_paths["MAIL_DIR"]),  # 邮件结果目录相对路径
                "log_dir": relative(eff_paths["LOG_DIR"]),  # 运行日志目录相对路径
            },
            "mail": {  # 邮件配置分组(表单键名,与 json 原始键不同)
                "enable_mail": bool(user_cfg.get("enable_mail", True)),  # 邮件总开关,缺省开
                "smtp_host": smtp_cfg.get("SMTP_DEFAULT_HOST", "smtp.qq.com"),  # 发件服务器,缺省 QQ
                "smtp_port": smtp_cfg.get("SMTP_DEFAULT_PORT", 465),  # 发件端口,缺省 465
                "sender_email": smtp_cfg.get("SMTP_SENDER_EMAIL", ""),  # 发件邮箱
                "smtp_code": smtp_cfg.get("SMTP_AUTH_CODE", ""),  # 授权码(回填用,敏感)
                "receive_email": receive_email,  # 收件人(列表首项)
            },
            "wecom": {  # 企业微信推送配置分组(表单键名,与 json 原始键不同)
                "enable_wecom": bool(wecom_cfg.get("WECOM_ENABLE", False)),  # 企业微信推送总开关,缺省关
                "webhook_url": wecom_cfg.get("WECOM_WEBHOOK_URL", ""),  # 群机器人 Webhook 完整地址(敏感,回填用)
                "msg_type": wecom_cfg.get("WECOM_MSG_TYPE", "text"),  # 推送消息类型:text/markdown
            },
            "github": {  # GitHub 分组
                "owner": github_cfg.get("GITHUB_REPO_OWNER", ""),  # 仓库所有者
                "name": github_cfg.get("GITHUB_REPO_NAME", ""),  # 仓库名
                "update_pkg": self.config.get("version", {}).get("UPDATE_SAVE_FILENAME_TPL", ""),  # 更新包文件名模板(属于version模块)
                "token": github_cfg.get("GITHUB_TOKEN", ""),  # 访问令牌
                "auto_check_update": bool(
                    self.config.get("version", {}).get("AUTO_CHECK_UPDATE_ON_START", True)
                ),  # 启动自动检测开关(属于version模块,缺省开)
            },
            "file_io": {  # 文件IO分组:界面表单键 -> 配置键的值
                "download_chunk": file_io_cfg.get("DOWNLOAD_CHUNK_SIZE", 1 * 1024 * 1024),  # 下载分片,缺省 1MB
                "download_workers": file_io_cfg.get("DOWNLOAD_MAX_WORKERS", 4),  # 下载并发,缺省 4
                "upload_chunk": file_io_cfg.get("UPLOAD_CHUNK_SIZE", 5 * 1024 * 1024),  # 上传分片,缺省 5MB
                "upload_workers": file_io_cfg.get("UPLOAD_MAX_WORKERS", 3),  # 上传并发,缺省 3
            },
            "proxy": {  # 代理分组
                "auto_detect": bool(proxy_cfg.get("AUTO_DETECT_SYSTEM_PROXY", False)),  # 自动跟随系统代理
                "use_proxy": bool(proxy_cfg.get("SERVICE_DEFAULT_USE_PROXY", False)),  # 手动代理开关
                "http_proxy": proxy_cfg.get("DEFAULT_HTTP_PROXY", "127.0.0.1"),  # HTTP 代理地址
                "https_proxy": proxy_cfg.get("DEFAULT_HTTPS_PROXY", "127.0.0.1"),  # HTTPS 代理地址
                "proxy_port": proxy_cfg.get("DEFAULT_PROXY_PORT", 38457),  # 代理端口
            },
            "accounts": list(user_cfg.get("accounts", [])),  # 账号邮箱列表(拷贝,防界面误改缓存)
            # 账号统计区:今日已签到数/今日签到获取流量
            "summary": self.get_checkin_summary(),  # 账号总数 + 今日已签数
        }

    # ------------------------------------------------------------------
    # 配置管理Tab:五类配置独立保存
    # ------------------------------------------------------------------
    def save_path_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """保存路径配置并判定数据、配置或日志目录变化是否需要重启.

        子目录保持界面提交的相对路径原文写入user_info.json;比较和写指针时,
        再以新数据目录为基准解析为绝对路径.

        :param payload: 数据目录全路径及七个业务子目录路径
        :type payload: Dict[str, Any]
        :return: need_restart和data_root_changed两个状态标志
        :rtype: Dict[str, Any]
        """
        old_paths = get_store_sub_paths()                 # 记录保存前运行时目录,用于日志目录变化比较
        old_data_root = get_data_store_root()             # 记录当前数据总目录绝对路径
        new_data_root_raw = str(payload.get("data_root", "")).strip()  # 获取界面提交的数据目录文本
        new_data_root = _to_abs_path(new_data_root_raw) if new_data_root_raw else old_data_root  # 空值沿用当前目录
        data_root_changed = os.path.normcase(new_data_root) != os.path.normcase(old_data_root)  # 忽略Windows路径大小写比较

        path_payload: Dict[str, str] = {}                 # 最终写入user_info的七个相对子目录
        for key in ("cookie_dir", "account_dir", "checkin_record_dir", "download_dir",
                    "config_root_dir", "mail_dir", "log_dir"):
            raw_value = str(payload.get(key, "")).strip()  # 获取界面提交的相对或绝对子目录
            resolved_value = _resolve_under_data_root(raw_value, new_data_root)  # 强制锚定新数据目录
            if resolved_value:  # 有效路径统一转回相对值保存,保持配置可迁移
                path_payload[key] = os.path.relpath(resolved_value, new_data_root)
            else:
                path_payload[key] = ""                   # 空值留给app_config使用标准默认目录
        save_user_info(path_payload)                      # 仅保存相对路径,读取时按当前数据目录重新拼接

        def resolved(key: str) -> str:
            """将已规范化的子目录配置按新数据目录解析为绝对路径."""
            return _resolve_under_data_root(path_payload.get(key, ""), new_data_root)

        need_restart = data_root_changed
        if data_root_changed:
            set_data_root_pointer(new_data_root)           # 写入最终数据根目录,下次启动据此读取新根目录/config
            new_config_root = resolved("config_root_dir")  # 在新数据目录下预创建配置子目录
            if new_config_root:
                os.makedirs(new_config_root, exist_ok=True)
            try:
                os.remove(CONFIG_ROOT_POINTER_FILE)        # 清除旧根目录内配置指针,新根默认读取自身config
            except OSError:
                pass                                      # 指针不存在或无法删除不阻断数据根目录切换
        else:
            new_cfg_root = resolved("config_root_dir")
            if new_cfg_root and os.path.normcase(new_cfg_root) != os.path.normcase(get_active_config_root()):
                set_config_root_pointer(new_cfg_root)
                need_restart = True
            new_log_dir = resolved("log_dir")
            if new_log_dir and os.path.normcase(new_log_dir) != os.path.normcase(old_paths["LOG_DIR"]):
                need_restart = True
        self._refresh_config()
        return {"need_restart": need_restart, "data_root_changed": data_root_changed}

    def save_mail_config(self, payload: Dict[str, Any]) -> None:
        """独立保存邮件开关及SMTP发件/收件配置.

        :param payload: 邮件开关、SMTP服务器、端口、发件人、授权码和收件人
        :type payload: Dict[str, Any]
        :return: 无返回值
        :rtype: None
        """
        save_user_info({"enable_mail": bool(payload.get("enable_mail", False))})  # 邮件总开关保存到用户配置
        smtp_cfg = _read_module_json("smtp")
        smtp_cfg.update({
            "SMTP_DEFAULT_HOST": payload.get("smtp_host", ""),
            "SMTP_DEFAULT_PORT": int(payload.get("smtp_port", 465)),
            "SMTP_SENDER_EMAIL": payload.get("sender_email", ""),
            "SMTP_AUTH_CODE": payload.get("smtp_code", ""),
            "SMTP_DEFAULT_TO_LIST": [payload.get("receive_email", "")] if payload.get("receive_email") else [],
        })
        _write_module_json("smtp", smtp_cfg)
        self._refresh_config()

    def save_wecom_config(self, payload: Dict[str, Any]) -> None:
        """独立保存企业微信推送开关及群机器人 Webhook 配置.

        :param payload: 企业微信开关、Webhook 完整地址与消息类型
        :type payload: Dict[str, Any]
        :return: 无返回值
        :rtype: None
        """
        save_wecom_config({                              # 表单键转换为 wecom.json 原始配置键
            "WECOM_ENABLE": bool(payload.get("enable_wecom", False)),
            "WECOM_WEBHOOK_URL": str(payload.get("webhook_url", "")).strip(),
            "WECOM_MSG_TYPE": str(payload.get("msg_type", "text")).strip() or "text",
        })
        self._refresh_config()

    def is_wecom_enabled(self) -> bool:
        """读取企业微信推送总开关.

        :return: WECOM_ENABLE 布尔值;未配置时默认 False(关闭)
        :rtype: bool
        """
        return bool(load_wecom_config().get("WECOM_ENABLE", False))  # 每次实时读取,缺省关闭

    def save_github_only(self, payload: Dict[str, Any]) -> None:
        """独立保存GitHub仓库配置及version模块中的更新选项.

        :param payload: 仓库所有者、仓库名、令牌、更新包模板和自动检查开关
        :type payload: Dict[str, Any]
        :return: 无返回值
        :rtype: None
        """
        save_github_config({                              # 增量更新github.json,保留API地址和超时等未展示字段
            "GITHUB_REPO_OWNER": payload.get("github_owner", ""),
            "GITHUB_REPO_NAME": payload.get("github_name", ""),
            "GITHUB_TOKEN": payload.get("github_token", ""),
        })
        version_cfg = _read_module_json("version")
        version_cfg["UPDATE_SAVE_FILENAME_TPL"] = payload.get("update_pkg", "")
        version_cfg["AUTO_CHECK_UPDATE_ON_START"] = bool(payload.get("auto_check_update", True))
        _write_module_json("version", version_cfg)
        self._refresh_config()

    def save_fileio_only(self, payload: Dict[str, Any]) -> None:
        """独立保存下载/上传分片大小与并发数配置.

        :param payload: 已由Controller转换为整数的四个文件IO参数
        :type payload: Dict[str, Any]
        :return: 无返回值
        :rtype: None
        """
        save_file_io_config({                            # 表单键转换为file_io.json原始配置键
            "DOWNLOAD_CHUNK_SIZE": int(payload["download_chunk"]),
            "DOWNLOAD_MAX_WORKERS": int(payload["download_workers"]),
            "UPLOAD_CHUNK_SIZE": int(payload["upload_chunk"]),
            "UPLOAD_MAX_WORKERS": int(payload["upload_workers"]),
        })
        self._refresh_config()

    def save_proxy_only(self, payload: Dict[str, Any]) -> None:
        """独立保存系统代理自动检测及手动代理参数.

        :param payload: 自动检测开关、手动代理开关、代理地址和已校验端口
        :type payload: Dict[str, Any]
        :return: 无返回值
        :rtype: None
        """
        save_proxy_config({                              # 表单键转换为proxy.json原始配置键
            "AUTO_DETECT_SYSTEM_PROXY": bool(payload.get("auto_detect", False)),
            "SERVICE_DEFAULT_USE_PROXY": bool(payload.get("use_proxy", False)),
            "DEFAULT_HTTP_PROXY": payload.get("http_proxy", ""),
            "DEFAULT_HTTPS_PROXY": payload.get("https_proxy", ""),
            "DEFAULT_PROXY_PORT": int(payload.get("proxy_port", 0)),
        })
        self._refresh_config()

    def detect_system_proxy(self):
        """
        实时检测操作系统代理设置(界面"检测系统代理"按钮用).

        :return: (proxies, source_desc);proxies={"http":url,"https":url} 或 None
        :rtype: tuple
        """
        from infrastructure.system_proxy import (
            detect_system_proxy,  # 延迟导入,保持 model 不顶层依赖基础设施
        )
        return detect_system_proxy()  # 直接透传检测结果

    # ------------------------------------------------------------------
    # 账号列表 / 账号详情
    # ------------------------------------------------------------------
    def list_accounts(self) -> List[str]:
        """获取账号邮箱列表.

        :return: user_info 中 accounts 的列表拷贝
        :rtype: List[str]
        """
        return list(load_user_info().get("accounts", []))  # 每次实时读取并转新列表,缺省空列表

    def add_account(self, email: str, password: str = "") -> bool:
        """新增账号并保存登录密码;邮箱已存在时仅更新密码,返回是否为新账号.

        :param email: 账号邮箱(账号唯一标识)
        :type email: str
        :param password: 登录密码,可为空串(空则不动密码)
        :type password: str
        :return: True 表示本次为新增账号;False 表示账号已存在
        :rtype: bool
        """
        accounts = self.list_accounts()  # 取当前账号列表
        is_new = email not in accounts  # 依据邮箱判重
        if is_new:  # 新账号:追加并持久化列表
            accounts.append(email)  # 列表末尾追加
            save_user_info({"accounts": accounts})  # 整体回写账号列表
            _logger.info(f"🆕 账号已新增:{email}(当前共{len(accounts)}个)")  # 新增留痕
        else:
            _logger.info(f"ℹ️ 账号已存在,本次仅更新登录密码:{email}")  # 已存在留痕
        # 无论新增还是已存在,只要界面带了密码就落盘(支持修改密码)
        if password:  # 密码非空才写凭证文件
            save_account_password(email, password)  # 单独加密/隔离保存登录密码
        return is_new  # 返回是否新增

    def remove_account(self, email: str) -> None:
        """从账号列表移除账号,并连带删除其详情与登录凭证.

        :param email: 要移除的账号邮箱
        :type email: str
        :return: 无返回值
        :rtype: None
        """
        accounts = self.list_accounts()  # 取当前列表
        if email in accounts:  # 存在才需要改列表
            accounts.remove(email)  # 移除该邮箱
            save_user_info({"accounts": accounts})  # 回写新列表
            _logger.info(f"🗑️ 账号已从列表移除:{email}(剩余{len(accounts)}个)")  # 移除留痕
        # 账号详情与登录凭证一并删除,避免残留可登录信息
        remove_account_detail(email)  # 删除账号详情快照
        remove_account_credential(email)  # 删除登录凭证(密码)

    @classmethod
    def normalize_detail_for_today(
        cls, detail: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """持久化快照"跨天翻篇"归一化(只改返回副本,不写磁盘).

        account_detail是签到那一刻的快照:昨天签到后signed_today=True、
        can_sign=False、带着昨日收益,隔日直接渲染会误报"今日已签到".
        若最后签到日期不是今天,把今日状态字段归零:
        signed_today=False、can_sign=True(网站按钮每日0点重置,跨天必可签)、
        checkin_gain_flow="--"(界面显示暂无,昨日收益不算今日获取).

        :param cls: 类本身(类方法隐式参数)
        :type cls: type
        :param detail: 账号详情快照字典,可为 None
        :type detail: Optional[Dict[str, Any]]
        :return: 归一化后的副本;入参非字典时原样返回
        :rtype: Optional[Dict[str, Any]]
        """
        if not isinstance(detail, dict):  # 空记录/脏数据直接原样返回
            return detail
        result = dict(detail)  # 拷贝,保证不修改磁盘读出的原对象
        if not cls.is_signed_today(result):  # 最后签到日期不是今天(跨天)
            result["signed_today"] = False  # 今日未签
            result["can_sign"] = True  # 网站每日 0 点重置,跨天必定可签
            result["checkin_gain_flow"] = "--"  # 昨日收益不算今日获取,显示"暂无"
            # 昨天的签到耗时不属于今天:置None让详情面板显示"未知",
            # 避免隔日误展示旧耗时
            result["checkin_cost_sec"] = None  # 旧耗时不延续到今天
        return result  # 返回归一化副本

    def get_account_detail(self, email: str) -> Optional[Dict[str, Any]]:
        """读取单个账号详情,并按"今天"做跨天归一化.

        :param email: 账号邮箱
        :type email: str
        :return: 归一化后的详情字典;无记录返回 None
        :rtype: Optional[Dict[str, Any]]
        """
        # 读出即按今天归一化,保证详情面板与统计区口径一致(磁盘原始快照不动)
        return self.normalize_detail_for_today(load_account_detail().get(email))  # 取原始快照后跨天翻篇

    def save_account_detail(self, email: str, detail: Dict[str, Any]) -> None:
        """保存单个账号详情快照.

        :param email: 账号邮箱
        :type email: str
        :param detail: 账号详情数据(签到状态/收益/耗时等)
        :type detail: Dict[str, Any]
        :return: 无返回值
        :rtype: None
        """
        # checkin_gain_flow的口径由映射层(user_info_to_account_detail/Controller
        # 组装)保证:今日已签=真实收益,今日未签="--".此处直接落盘,不再保留旧值,
        # 否则跨天后未签到账号仍会一直显示昨天的收益
        save_account_detail(email, detail)  # 委托 account_store 直接覆盖落盘

    def get_account_password(self, email: str) -> str:
        """取账号已保存的登录密码(供选中回填/批量签到);未保存返回空串.

        :param email: 账号邮箱
        :type email: str
        :return: 登录密码;无记录返回空串
        :rtype: str
        """
        return get_account_password(email)  # 透传账号存储层

    def list_account_credentials(self) -> List[Dict[str, str]]:
        """组装批量签到所需凭证列表,仅返回已保存密码的账号:[{email,password}].

        :return: 按账号列表顺序排列的凭证字典列表
        :rtype: List[Dict[str, str]]
        """
        creds = load_account_credentials()  # 全量凭证字典(email -> {password,...})
        result: List[Dict[str, str]] = []  # 结果列表
        # 以账号列表顺序为准,凭证文件中多出的脏数据不参与
        for email in self.list_accounts():  # 只遍历正式账号列表
            item = creds.get(email)  # 取该账号凭证
            if isinstance(item, dict) and item.get("password"):  # 类型正确且确实存了密码
                result.append({"email": email, "password": str(item["password"])})  # 组装批量签到所需结构
        return result  # 返回有序凭证列表

    # ------------------------------------------------------------------
    # 账号全部信息统计
    # ------------------------------------------------------------------
    @staticmethod
    def is_signed_today(detail: Optional[Dict[str, Any]]) -> bool:
        """判定账号详情是否为"今日已签到".

        以页面按钮布尔signed_today为基础,再用最后签到时间的日期部分与今天
        比对校验:account_detail是持久化快照,隔日启动时旧记录的布尔仍为True,
        不校验日期会误报.时间缺失/无法解析时退回只信布尔,避免漏统计.

        :param detail: 账号详情快照,可为 None
        :type detail: Optional[Dict[str, Any]]
        :return: True 表示确认今日已签到
        :rtype: bool
        """
        if not isinstance(detail, dict) or not detail.get("signed_today"):  # 非字典或布尔为假:必未签
            return False
        last_sign = str(detail.get("last_sign_time", "")).strip()  # 取最后签到时间字符串
        m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", last_sign)  # 正则提取开头的年-月-日
        if not m:  # 时间缺失/格式不符:无法证伪,退回相信布尔
            return True
        try:  # 尝试解析签到日期字符串,需捕获格式错误
            sign_date = datetime.strptime(m.group(0), "%Y-%m-%d").date()  # 解析日期部分为 date 对象
        except ValueError:  # 捕获日期格式非法(如 2026-13-40)异常
            return True  # 非法日期同样退回相信布尔,避免漏统计
        return sign_date == datetime.now().date()  # 日期等于今天才算今日已签

    def get_checkin_summary(self) -> Dict[str, Any]:
        """全账号统计(供主界面统计区前两栏):账号总数/今日已签到数.

        注:第三栏"今日签到获取"只显示[当前选中账号]的收益,
        不走汇总,取值见get_selected_today_gain().

        :return: {"total": 账号总数, "signed_today": 今日已签数}
        :rtype: Dict[str, Any]
        """
        accounts = self.list_accounts()  # 账号列表(决定统计口径与总数)
        details = load_account_detail()  # 全量详情快照(从磁盘读取所有账号的持久化状态)
        signed_cnt = sum(  # 对今日已签账号计数
            1 for email in accounts if self.is_signed_today(details.get(email))  # 逐账号判定,缺失记未签
        )
        summary = {"total": len(accounts), "signed_today": signed_cnt}  # 组装统计结果字典
        _logger.debug(f"📊 签到统计:账号{summary['total']}个,今日已签{signed_cnt}个")  # 调试级统计日志
        return summary  # 返回统计字典供界面统计区展示

    def get_selected_today_gain(self, email: str) -> str:
        """获取指定(选中)账号[今日]签到获得的流量文案.

        仅当该账号今日确已签到才返回checkin_gain_flow;未签到/无记录/
        收益占位符均返回空串,由界面显示"暂无"(历史收益不算今日获取).

        :param email: 当前选中账号邮箱
        :type email: str
        :return: 今日收益文案;不满足条件返回空串
        :rtype: str
        """
        email = (email or "").strip()  # 防空并去空白
        if not email:  # 未选中账号
            return ""
        detail = load_account_detail().get(email)  # 取该账号原始快照
        if not isinstance(detail, dict) or not self.is_signed_today(detail):  # 无记录或今日未签
            return ""
        gain = str(detail.get("checkin_gain_flow", "")).strip()  # 取收益文案
        return "" if gain in ("", "--") else gain  # 空或占位符"--"均按暂无处理

    def is_mail_enabled(self) -> bool:
        """读取邮件通知总开关.

        :return: enable_mail 布尔值;未配置时默认 True
        :rtype: bool
        """
        return bool(load_user_info().get("enable_mail", True))  # 每次实时读取,缺省开启

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _refresh_config(self):
        """落盘后刷新内存中的全局合并配置,供后续Worker读取最新值.

        :return: 无返回值
        :rtype: None
        """
        _logger.debug("🔄 刷新内存中的全局合并配置")  # 调试级刷新留痕
        self.config = load_and_merge_all_config()  # 重新走完整加载合并流程并替换缓存
