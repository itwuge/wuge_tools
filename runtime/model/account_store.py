# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: account_store.py
# 归属: runtime/model 业务模型层(MVC-M) —— 签到业务数据持久化层
# ------------------------------------------------------------------------------
# 文件用途:
#   签到业务数据持久化层——负责账号详情、登录凭证(明文密码)、签到收益
#   记录、签到结果文本、Cookie 文件的路径管理与 JSON 原子读写
#   (从 comm_tools/app_config.py 拆分,与通用配置管理层解耦).
# ------------------------------------------------------------------------------
# 架构定位:
#   Model 角色;被 workers(签到 Worker)、controller(账号管理)与
#   sign_tools(收益回填)调用,不依赖任何 view;自身只做文件 IO,
#   网络/界面逻辑均不在本模块.
#
#   关联组件:
#     - 上游: workers(签到 Worker)、controller(账号管理)、sign_tools(收益回填)
#     - 下游: runtime.model.app_config(底层 IO 能力)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 账号详情 account_detail.json 的增删改查(含旧位置一次性迁移)
#   2. 账号凭证 account_credential.json 的明文密码存取
#   3. cookie/签到记录/邮件正文的文件路径生成
#   4. 签到结果文本 mail_body.txt 覆盖写
#   5. 签到任务前置清理(删旧详情快照、保留收益记录兜底)
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责签到业务数据的持久化,不涉及网络或 UI
#   - 所有 JSON 读写复用 app_config 的原子写与注释剥离能力
#   - 账号凭证以明文存储,提醒用户勿分享/上传
# ------------------------------------------------------------------------------
# 线程模型:
#   文件 IO 通过 app_config 的全局 IO 锁(_IO_LOCK)保护并发安全;
#   路径生成为纯函数无状态,可在任意线程调用;
#   建议在工作线程中执行文件读写,避免阻塞 UI.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: json、logging、os、typing
#   - 第三方: 无
#   - 项目内: runtime.model.app_config(DATA_CONFIG_ROOT、_IO_LOCK、
#             _atomic_write_json、_strip_comment、get_store_sub_paths)
# ==============================================================================
# =========** [AccountStore]Model =========
import json  # JSON 读写(实际解析委托 app_config,本模块保留原 import)
import logging  # 标准日志库
import os  # 路径拼接/文件存在性/目录创建
from typing import Any, Dict  # 类型提示

from runtime.model.app_config import (  # 复用通用配置层的底层 IO 能力,不反向耦合签到业务
    DATA_CONFIG_ROOT,  # 旧版数据配置根目录,用于一次性迁移老文件
    _IO_LOCK,  # 全局文件 IO 互斥锁,防止并发写坏 JSON
    _atomic_write_json,  # 原子写 JSON(临时文件+replace,防写一半崩溃)
    _strip_comment,  # 读取时剥离 _comment 说明键
    get_store_sub_paths,  # 获取运行时 ACCOUNT_DIR/COOKIE_DIR/MAIL_DIR 等子目录
)

_logger = logging.getLogger("AccountStore")  # 本模块专用 logger,日志名 AccountStore

# 账号详情文件名(存放在运行时ACCOUNT_DIR下)
ACCOUNT_DETAIL_FILE = "account_detail.json"  # 所有账号详情快照的总文件
# 账号登录凭证文件名(与账号详情同目录,随数据目录一起迁移)
ACCOUNT_CREDENTIAL_FILE = "account_credential.json"  # 邮箱->明文密码映射文件
# 签到结果/邮件正文固定文件名(界面只配目录,文件名写死)
MAIL_BODY_FILENAME = "mail_body.txt"  # 最近一次签到任务结果文本
# 凭证文件头部注释(明文密码安全提示)
_ACCOUNT_CREDENTIAL_COMMENT = (  # 写入 JSON 的 _comment 字段,提醒用户明文风险
    "账号登录凭证,key为账号邮箱,value.password为登录密码(明文存储,请勿分享/上传本文件)"
)


# ---------------- 账号详情:存放在界面可配置的"账号目录" ----------------
def _account_detail_file_path() -> str:
    """账号详情文件完整路径:跟随运行时ACCOUNT_DIR
    :return: account_detail.json 的绝对路径
    :rtype: str
    """
    return os.path.join(get_store_sub_paths()["ACCOUNT_DIR"], ACCOUNT_DETAIL_FILE)  # 账号目录 + 固定文件名


def _load_account_detail() -> Dict[str, Any]:
    """加载账号详情;首次加载时从旧位置(配置目录)做一次性迁移
    :return: {邮箱: 详情dict};文件损坏/迁移失败时回退空字典,不抛异常
    :rtype: Dict[str, Any]
    :raises json.JSONDecodeError: 旧文件迁移分支内捕获并告警
    :raises OSError: 旧文件迁移分支内捕获并告警
    """
    fp = _account_detail_file_path()  # 新位置(可配置账号目录)完整路径
    # 一次性迁移:旧版本account_detail.json位于配置目录
    legacy_fp = os.path.join(DATA_CONFIG_ROOT, ACCOUNT_DETAIL_FILE)  # 旧版本固定配置目录路径
    if not os.path.exists(fp) and os.path.exists(legacy_fp):  # 新文件不存在且旧文件存在 => 首次升级场景
        try:  # 迁移读旧文件可能失败,需捕获异常
            with open(legacy_fp, "r", encoding="utf-8") as f:  # 读旧位置 JSON
                legacy = _strip_comment(json.load(f))  # 解析并剥离 _comment 说明键
            if legacy:  # 旧数据非空才迁移
                _atomic_write_json(fp, legacy)  # 原子写到新位置(随账号目录配置走)
                _logger.info(f"📦 账号详情从旧位置一次性迁移完成:{legacy_fp} -> {fp}")  # 留痕迁移结果
                return legacy  # 直接返回迁移数据,本轮不再重复读盘
        except (json.JSONDecodeError, OSError) as e:  # 捕获旧文件损坏或不可读异常
            _logger.warning(f"⚠ 账号详情旧文件迁移失败,按新空文件启动:{e}")  # 告警后走新建空文件流程
    if not os.path.exists(fp):  # 新位置仍无文件(首次运行或迁移失败)
        with _IO_LOCK:  # 加全局锁,避免多线程同时判定不存在
            if not os.path.exists(fp):  # 双重检查(double-checked locking),防锁竞争重复写
                _atomic_write_json(fp, {})  # 原子写入空字典初始化
        _logger.debug(f"📄 账号详情文件不存在,已生成空文件:{fp}")  # 调试日志
        return {}  # 返回空数据
    try:  # 正常读盘,需捕获文件损坏或权限异常
        with open(fp, "r", encoding="utf-8") as f:  # UTF-8 只读
            return _strip_comment(json.load(f))  # 解析并剥离说明注释键
    except Exception as e:  # 捕获文件损坏/权限不足等任意异常
        _logger.warning(f"⚠ 账号详情读取失败,本次回退空数据:{fp} | {e}")  # 告警但不阻断启动
        return {}  # 降级为空字典


def _write_account_detail(data: Dict[str, Any]) -> None:
    """把账号详情整表原子写回(内部函数,自动附加文件头注释)
    :param data: 完整账号详情字典 {邮箱: 详情}
    :type data: Dict[str, Any]
    :return: 无返回值
    :rtype: None
    """
    fp = _account_detail_file_path()  # 目标文件路径
    payload = {"_comment": "每个账号的详细信息持久化,key为账号邮箱", **data}  # 头部说明键在前,账号数据在后
    with _IO_LOCK:  # 与其他 IO 操作互斥
        _atomic_write_json(fp, payload)  # 临时文件写完再 replace,防止崩溃产生半截 JSON


def load_account_detail() -> dict:
    """加载所有账号详情字典(数据位于可配置账号目录)
    :return: {邮箱: 详情dict};无数据或异常时为空字典
    :rtype: dict
    """
    return _load_account_detail()  # 直接委托内部加载(含迁移/初始化/容错)


def save_account_detail(account_email: str, detail_data: dict):
    """保存单个账号详情,不存在则新增,存在覆盖
    :param account_email: 账号邮箱,作为详情字典的 key
    :type account_email: str
    :param detail_data: 该账号的详情快照(user_info_to_account_detail 契约)
    :type detail_data: dict
    :return: 无返回值
    :rtype: None
    """
    cfg = _load_account_detail()  # 先整表读出
    is_new = account_email not in cfg  # 记录写入前是否已存在,用于日志区分新增/更新
    cfg[account_email] = detail_data  # 覆盖或新增该账号条目
    _write_account_detail(cfg)  # 整表原子写回
    _logger.info(f"💾 账号详情已{'新增' if is_new else '更新'}:{account_email}")  # 按新增/更新输出留痕


def remove_account_detail(account_email: str):
    """删除账号时同步删除该账号详情
    :param account_email: 待删除的账号邮箱
    :type account_email: str
    :return: 无返回值;账号不存在时静默跳过
    :rtype: None
    """
    cfg = _load_account_detail()  # 整表读出
    if account_email in cfg:  # 存在才执行删除与写盘
        del cfg[account_email]  # 移除该账号详情
        _write_account_detail(cfg)  # 原子写回
        _logger.info(f"🗑️ 账号详情已删除:{account_email}")  # 留痕


# ---------------- 账号登录凭证:与账号详情同目录(明文,用于自动登录/批量签到) ----------------
def _account_credential_file_path() -> str:
    """账号凭证文件完整路径:跟随运行时ACCOUNT_DIR
    :return: account_credential.json 的绝对路径
    :rtype: str
    """
    return os.path.join(get_store_sub_paths()["ACCOUNT_DIR"], ACCOUNT_CREDENTIAL_FILE)  # 账号目录 + 凭证文件名


def _load_account_credential() -> Dict[str, Any]:
    """加载账号凭证;文件不存在返回仅含注释的空结构(不自动落盘,首次保存时创建)
    :return: {邮箱: {"password": 密码}};读取异常时返回空字典
    :rtype: Dict[str, Any]
    """
    fp = _account_credential_file_path()  # 凭证文件路径
    if not os.path.exists(fp):  # 文件不存在(用户从未保存过密码)
        return {"_comment": _ACCOUNT_CREDENTIAL_COMMENT}  # 仅返回内存注释结构,不写盘
    try:  # 正常读盘,需捕获文件损坏或权限异常
        with open(fp, "r", encoding="utf-8") as f:  # UTF-8 只读
            data = _strip_comment(json.load(f))  # 解析并剥离注释键
        # 仅保留dict形态的账号项,过滤异常结构
        return {k: v for k, v in data.items() if isinstance(v, dict)}  # 字典推导式:剔除非 dict 的脏条目
    except Exception as e:  # 捕获文件损坏/权限不足等任意异常
        _logger.warning(f"⚠ 账号凭证读取失败,本次按无已保存密码启动:{fp} | {e}")  # 告警
        return {}  # 按无凭证启动,用户可重新输入密码


def _write_account_credential(data: Dict[str, Any]) -> None:
    """把凭证整表原子写回(内部函数,自动附明文安全提示注释)
    :param data: 完整凭证字典 {邮箱: {"password": ...}}
    :type data: Dict[str, Any]
    :return: 无返回值
    :rtype: None
    """
    fp = _account_credential_file_path()  # 目标路径
    payload = {"_comment": _ACCOUNT_CREDENTIAL_COMMENT, **data}  # 安全提示在前,凭证在后
    with _IO_LOCK:  # 互斥保护
        _atomic_write_json(fp, payload)  # 原子落盘


def load_account_credentials() -> Dict[str, Dict[str, str]]:
    """加载全部账号凭证:{邮箱: {"password": 密码}};无凭证返回空字典
    :return: 账号凭证映射表
    :rtype: Dict[str, Dict[str, str]]
    """
    return _load_account_credential()  # 委托内部加载


def get_account_password(account_email: str) -> str:
    """取单个账号已保存的密码;未保存返回空串
    :param account_email: 账号邮箱
    :type account_email: str
    :return: 明文密码;无记录/结构异常时为空串
    :rtype: str
    """
    item = _load_account_credential().get(account_email)  # 取该账号凭证条目
    if isinstance(item, dict):  # 结构正常
        return str(item.get("password", ""))  # 取 password 并强转 str
    return ""  # 无条目或脏数据


def save_account_password(account_email: str, password: str) -> None:
    """保存(或更新)单个账号登录密码
    :param account_email: 账号邮箱(凭证 key)
    :type account_email: str
    :param password: 登录明文密码
    :type password: str
    :return: 无返回值;密码内容不写日志
    :rtype: None
    """
    cfg = _load_account_credential()  # 读出整表
    is_new = account_email not in cfg  # 区分首次保存/更新
    cfg[account_email] = {"password": password}  # 覆盖写入(明文)
    _write_account_credential(cfg)  # 原子写回
    _logger.info(f"🔑 账号登录密码已{'保存' if is_new else '更新'}:{account_email}(明文,密码不落日志)")  # 日志只记邮箱,严禁打印密码


def remove_account_credential(account_email: str) -> None:
    """删除账号时同步删除该账号登录凭证
    :param account_email: 待删除的账号邮箱
    :type account_email: str
    :return: 无返回值;不存在时静默跳过
    :rtype: None
    """
    cfg = _load_account_credential()  # 读出整表
    if account_email in cfg:  # 存在才删
        del cfg[account_email]  # 移除凭证
        _write_account_credential(cfg)  # 原子写回
        _logger.info(f"🗑️ 账号登录凭证已删除:{account_email}")  # 留痕


# ---------------- 签到记录/邮件/cookie 路径 ----------------
def safe_email_name(raw_email: str) -> str:
    """邮箱转磁盘安全文件名(当前直接返回原值,保留接口供未来扩展)
    :param raw_email: 原始邮箱字符串
    :type raw_email: str
    :return: 当前原样返回邮箱(邮箱字符天然可作 Windows 文件名)
    :rtype: str
    """
    return raw_email  # 预留扩展点:未来如需替换 @ 等字符在此统一处理


def get_cookie_file_path(email: str) -> str:
    """获取账号对应的cookie完整文件路径
    :param email: 账号邮箱
    :type email: str
    :return: COOKIE_DIR/cookie_<邮箱>.json 绝对路径
    :rtype: str
    """
    paths = get_store_sub_paths()  # 取运行时各数据子目录
    safe_name = safe_email_name(email)  # 文件名安全化(当前为原值)
    return os.path.join(paths["COOKIE_DIR"], f"cookie_{safe_name}.json")  # 按邮箱命名,cookie 与账号一一对应


def get_checkin_record_path(email: str) -> str:
    """获取账号对应的签到记录完整json路径
    :param email: 账号邮箱
    :type email: str
    :return: CHECKIN_RECORD_DIR/checkin_<邮箱>.json 绝对路径
    :rtype: str
    """
    paths = get_store_sub_paths()  # 取运行时数据子目录
    safe_name = safe_email_name(email)  # 文件名安全化
    return os.path.join(paths["CHECKIN_RECORD_DIR"], f"checkin_{safe_name}.json")  # 账号独立收益记录文件


def get_mail_body_path() -> str:
    """签到汇总结果txt的当前生效完整路径(邮件目录/mail_body.txt)
    :return: mail_body.txt 绝对路径,跟随界面配置的邮件目录
    :rtype: str
    """
    return os.path.join(get_store_sub_paths()["MAIL_DIR"], MAIL_BODY_FILENAME)  # 邮件目录 + 固定文件名


def write_checkin_result_text(content: str) -> str:
    """把[本次签到任务]的结果文本覆盖写入签到结果文件(mail_body.txt)

    单账号签到与批量签到共用:文件永远只保留最近一次任务的结果.
    父目录由ensure_all_dir统一创建,此处再兜底一次防止自定义路径迁移后缺失
    :param content: 待写入的完整结果文本(format_user_info_text 等渲染产物)
    :type content: str
    :return: 实际写入的完整文件路径
    :rtype: str
    """
    fp = get_mail_body_path()  # 当前生效结果文件路径
    parent = os.path.dirname(fp)  # 取父目录
    if parent:  # 父目录非空(正常绝对路径)
        os.makedirs(parent, exist_ok=True)  # 兜底建目录,已存在不报错
    with open(fp, "w", encoding="utf-8") as f:  # 覆盖写,只保留最近一次任务结果
        f.write(content or "")  # None 容错为空串
    _logger.info(f"📝 本次签到结果已写入结果文件:{fp}")  # 留痕
    return fp  # 返回实际路径供调用方提示/邮件附件使用


def reset_account_sign_persistence(account_email: str) -> None:
    """签到任务[前置清理]:删除该账号上一次的签到持久化产物

    每次签到任务严格按"清旧→签到→存新"执行,保证本地只剩本次真实结果:
    - account_detail.json 中的账号详情快照(面板状态/今日已签标记/收益)

    ⚠️ 注意:[不删除]checkin_records 目录下的签到收益记录文件.
    收益记录是"今日已签到"场景的兜底数据源:重复签到时(页面已签→跳过接口,
    或签到接口返回ret=0表示已签到)不会重新写入收益,此时需从记录文件回填.
    成功签到(ret=1)时由 fill_checkin_result 覆盖写入新收益,不会残留旧值;
    且 user_info_to_account_detail 仅在 signed_today=True 时才读取记录文件,
    昨日记录不会污染今日未签到的显示.
    Cookie(登录态)与账号登录密码不属于签到结果,保留不动;文件不存在静默
    :param account_email: 待清理的账号邮箱;空白入参直接返回
    :type account_email: str
    :return: 无返回值
    :rtype: None
    """
    account_email = (account_email or "").strip()  # None 安全处理并去空白
    if not account_email:  # 空邮箱无法定位数据
        return  # 直接跳过
    # 1.删除账号详情快照(签到后会重新写入)
    cfg = _load_account_detail()  # 读详情整表
    if account_email in cfg:  # 存在旧快照才清理
        del cfg[account_email]  # 删除该账号上轮详情
        _write_account_detail(cfg)  # 原子写回
        _logger.info(f"🗑️ 历史账号详情快照已清除:{account_email}")  # 留痕
    # 2.[保留]签到收益记录文件,供重复签到/已签跳过路径回填今日收益


__all__ = [  # 模块公开 API 白名单
    "ACCOUNT_DETAIL_FILE",  # 详情文件名常量
    "ACCOUNT_CREDENTIAL_FILE",  # 凭证文件名常量
    "MAIL_BODY_FILENAME",  # 邮件正文文件名常量
    "load_account_detail",  # 加载全部账号详情
    "save_account_detail",  # 保存单个账号详情
    "remove_account_detail",  # 删除单个账号详情
    "load_account_credentials",  # 加载全部凭证
    "get_account_password",  # 取单账号密码
    "save_account_password",  # 存单账号密码
    "remove_account_credential",  # 删单账号凭证
    "safe_email_name",  # 邮箱文件名安全化
    "get_cookie_file_path",  # cookie 文件路径
    "get_checkin_record_path",  # 签到收益记录路径
    "get_mail_body_path",  # 结果文本路径
    "write_checkin_result_text",  # 覆盖写结果文本
    "reset_account_sign_persistence",  # 签到前置清理
]
