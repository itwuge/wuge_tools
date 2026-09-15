# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: sign_tools.py
# 归属: runtime/model 业务模型层(MVC-M) —— V2free 签到业务工具函数集合
# ------------------------------------------------------------------------------
# 文件用途:
#   V2free 签到业务专用工具函数集合——负责用户中心 HTML 页面解析、
#   签到结果回填与按账号持久化、Cookie 免登校验、账号详情结构映射,
#   以及统一详情文本/耗时/流量文案的格式化(从 comm_tools/tools.py 拆分,
#   与通用工具层解耦);通用工具(如 extract_targz)仍在 comm_tools/tools.py,
#   供更新助手等复用。
# ------------------------------------------------------------------------------
# 架构定位:
#   Model 角色;被 workers(单账号/批量签到 Worker)与 controller 调用,
#   自身不依赖任何 view(PySide6);网络访问只经由入参传入的
#   ServiceApiService 完成,不直接持有界面状态。
#
#   关联组件:
#     - 上游: workers(单账号/批量签到 Worker)、controller
#     - 下游: runtime.model.account_store(get_checkin_record_path)、
#             runtime.model.app_config(get_log_file_path,仅调试转储时导入)、
#             service.run_ba.ServiceApiService(仅 TYPE_CHECKING)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 解析用户中心 HTML(Cloudflare 混淆邮箱/流量/签到按钮/订阅链接)
#   2. 签到收益按账号独立 JSON 持久化与内存回填
#   3. Cookie 免登加载与账号一致性校验
#   4. 签到成功/已签跳过后重拉用户中心并保护已回填收益
#   5. 统一账号详情契约映射与详情文本渲染、耗时/流量文案格式化
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责签到业务的工具函数,不直接发起网络请求
#   - 网络访问经由入参传入的 ServiceApiService 完成
#   - 不依赖任何 view(PySide6)
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块函数均在调用方线程中执行(通常为 QThread 工作线程);
#   文件 IO(签到收益持久化)在工作线程中执行,无并发锁(单账号串行);
#   HTML 解析为纯计算无状态,可在任意线程调用。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: json、logging、os、re、time、typing、
#             pathlib(函数内延迟导入)
#   - 第三方: beautifulsoup4(BeautifulSoup, html.parser)
#   - 项目内: runtime.model.account_store(get_checkin_record_path)、
#             runtime.model.app_config(get_log_file_path,仅调试转储时导入)、
#             service.run_ba.ServiceApiService(仅 TYPE_CHECKING 类型提示)
# ==============================================================================
# =========** [SignTools]Model =========
import json  # JSON 读写:签到收益记录文件的序列化/反序列化
import logging  # 标准日志库,输出签到流程诊断信息
import os  # 路径拼接/文件存在性判断
import re  # 正则:页面时间与流量文案提取
import time  # 时间戳生成/调试文件名时间戳
from typing import TYPE_CHECKING, Any, Dict, Optional  # 类型提示;TYPE_CHECKING 仅静态检查期为真

from bs4 import BeautifulSoup  # 第三方 HTML 解析库,解析 V2free 用户中心页面

from runtime.model.account_store import get_checkin_record_path  # 项目内:按邮箱取账号独立签到记录文件路径

if TYPE_CHECKING:  # 静态类型检查分支,运行时不执行,避免循环导入
    from service.run_ba import ServiceApiService  # 仅用于类型注解,不产生运行时依赖

_logger = logging.getLogger("CheckinTools")  # 本模块专用 logger,日志名 CheckinTools


def save_last_checkin_gain(email: str, msg: str) -> None:
    """
    持久化保存最近一次签到获得流量信息到【账号独立】本地json
    :param email: 账号邮箱,用于生成独立记录文件
    :type email: str
    :param msg: checkin接口返回的msg字符串,例如 "获得120MB流量"
    :type msg: str
    :return: 无返回值;写盘失败仅告警不抛异常
    :rtype: None
    :raises Exception: 捕获后转 warning 日志(文件占用/权限/磁盘问题),不向上抛出
    """
    store_path = get_checkin_record_path(email)  # 由邮箱定位 checkin_records/checkin_<邮箱>.json 路径
    store_data = {  # 待落盘的数据结构,包含收益文案与时间戳
        "last_gain_flow": msg,  # 签到接口返回的原始收益文案,如 "获得了 332MB 流量."
        "timestamp": int(time.time())  # 落盘时的 Unix 秒级时间戳,便于判断记录新旧
    }
    try:  # 捕获所有 IO 异常,保证持久化失败不阻断签到主流程
        with open(store_path, "w", encoding="utf-8") as f:  # 以 UTF-8 覆盖写账号记录文件
            json.dump(store_data, f, ensure_ascii=False, indent=2)  # 中文不转义、缩进2格,方便人工查看
        _logger.debug(f"💾 签到收益已持久化:{email} -> {store_path} ({msg})")  # 调试级成功日志
    except Exception as e:  # 权限/占用/编码等任意失败
        # 持久化失败不阻断签到主流程,但必须留痕(原为静默吞掉)
        _logger.warning(f"⚠ 保存签到收益记录失败:{email} | {e}")  # 告警留痕,便于排查落盘问题


def load_last_checkin_gain(email: str) -> Optional[str]:
    """
    读取本地持久化的上一次签到流量（账号独立）
    文件不存在/读取失败返回 None
    :param email: 账号邮箱,用于定位独立记录文件
    :type email: str
    :return: 上次签到收益文案;文件不存在或解析失败时为 None
    :rtype: Optional[str]
    :raises Exception: 内部捕获所有异常并归一为 None,不向上抛出
    """
    store_path = get_checkin_record_path(email)  # 账号独立签到记录文件完整路径
    if not os.path.exists(store_path):  # 文件不存在属正常情况(从未成功签到过)
        return None  # 直接返回 None,调用方按"无历史收益"处理
    try:  # 读盘与 JSON 解析均可能失败,统一兜底
        with open(store_path, "r", encoding="utf-8") as f:  # UTF-8 只读打开记录文件
            store_data = json.load(f)  # 反序列化为 dict
        return store_data.get("last_gain_flow")  # 取收益文案;键缺失时 get 返回 None
    except Exception:  # 文件损坏/JSON 非法/权限不足等
        return None  # 读取失败静默降级为无记录,避免影响签到流程


def fill_persisted_checkin_result(info_data: Dict[str, Any]) -> None:
    """
    原地回填本地持久化签到收益,仅当内存中checkin_gain_flow为空才回填
    需要info_data["email"]存在有效账号邮箱
    :param info_data: 用户信息字典(cleaning_html 产物),原地修改;需含 email 键
    :type info_data: Dict[str, Any]
    :return: 无返回值,满足条件时直接写入 info_data["checkin_gain_flow"]
    :rtype: None
    """
    current = info_data.get("checkin_gain_flow", "").strip()  # 内存中已有收益(去首尾空白),可能来自本次签到回填
    if current:  # 内存值非空说明本次流程已拿到收益
        return  # 不允许用历史记录覆盖最新值,直接返回
    email = info_data.get("email", "")  # 取账号邮箱作为记录文件定位 key
    if not email:  # 无邮箱则无法定位账号独立记录
        return  # 放弃回填
    persist_msg = load_last_checkin_gain(email)  # 从 checkin_<邮箱>.json 读上次收益
    if persist_msg:  # 历史记录存在且非空
        info_data["checkin_gain_flow"] = persist_msg  # 原地回填到用户信息字典


def fill_checkin_result(info_data: Dict[str, Any], checkin_resp: Dict[str, Any]) -> None:
    """
    将签到接口返回结果回填到info_data字典(原地修改)
    ✅ 直接原样存入msg到checkin_gain_flow,同时持久化到账号记录文件
    :param info_data: cleaning_html 返回的用户信息字典,必须包含email字段
    :type info_data: Dict[str, Any]
    :param checkin_resp: api_user_checkin 返回结果字典,包含 ret, msg
    :type checkin_resp: Dict[str, Any]
    :return: 无返回值;ret==1 时原地更新签到状态字段并落盘收益
    :rtype: None
    """
    email = info_data.get("email", "")  # 账号邮箱,用于收益按账号落盘与日志展示
    if checkin_resp.get("ret") == 1:  # 网站约定 ret==1 表示签到接口调用成功
        msg = checkin_resp.get("msg", "")  # 原样取出收益文案,不做裁剪(裁剪统一在展示层 short_flow_text)
        info_data["checkin_gain_flow"] = msg  # 回填今日签到收益
        info_data["today_checked"] = True  # 标记今日已完成签到
        info_data["can_checkin"] = False  # 同步内存态:已不可再签到
        info_data["dom_can_checkin"] = False  # 同步 DOM 态:按钮应为禁用
        info_data["dom_checkin_text"] = "check今日已签到"  # 同步按钮文案口径
        _logger.info(f"✅ 签到结果已回填:{email or '未知账号'} | 收益:{msg}")  # info 级成功日志
        if email:  # 有有效邮箱才能按账号落盘
            save_last_checkin_gain(email, msg)  # 持久化收益,供重复签到/重开软件时兜底回填
    else:  # ret 非 1:重复签到、失败或网站拒绝
        _logger.debug(f"ℹ️ 签到返回非成功(ret={checkin_resp.get('ret')}),不回填收益字段")  # 仅调试日志,不污染收益字段


def user_info_to_account_detail(
    user_info: Optional[Dict[str, Any]],
    signed_ok: bool,
    fallback_email: str = "",
) -> Dict[str, Any]:
    """把cleaning_html的用户信息统一映射为界面持久化用的账号详情结构

    单账号签到Worker与批量签到Worker共用,避免字段映射两处漂移;
    user_info为None/非dict时返回全占位结构,不抛异常
    :param user_info: cleaning_html 解析出的用户信息字典;None/非 dict 时按空结构处理
    :type user_info: Optional[Dict[str, Any]]
    :param signed_ok: 本次签到接口是否返回成功(ret==1)
    :type signed_ok: bool
    :param fallback_email: 用户信息缺email时用任务账号兜底
    :type fallback_email: str
    :return: 扁平化账号详情字典(界面持久化/邮件渲染的统一契约),缺字段一律 "--" 占位
    :rtype: Dict[str, Any]
    """
    info = user_info if isinstance(user_info, dict) else {}  # 容错:非字典输入归一为空字典
    sub_links = info.get("sub_links")  # 订阅链接子字典(v2ray/clash/clash_pro)
    if not isinstance(sub_links, dict):  # 缺字段或结构异常
        sub_links = {}  # 兜底空字典,后续 .get 安全
    email_key = info.get("email") or fallback_email  # 页面邮箱优先,解析失败用任务账号兜底
    # checkin_gain_flow语义=【今日】签到获得的流量。getuserinfo页面不含此字段,
    # cleaning_html的fill_persisted_checkin_result会回填"上次签到收益"(可能是
    # 昨天的),因此必须先判定今日是否已签到:仅今日已签才采用内存回填值/签到
    # 记录文件兜底;今日未签一律"--"(界面显示"暂无"),历史收益不得展示
    signed_today = bool(info.get("today_checked", signed_ok))  # 页面已签标记为准,签到接口成功作为兜底真值
    gain_flow = ""  # 今日收益先初始化为空串
    if signed_today:  # 仅今日确实已签到才允许出现收益
        # 内存值(本次签到回填 或 已签跳过路径从今日记录回填)优先,再读记录文件兜底
        gain_flow = str(info.get("checkin_gain_flow", "") or "").strip()  # 内存收益转字符串并去空白
        if not gain_flow and email_key:  # 内存为空且有邮箱 key
            gain_flow = load_last_checkin_gain(email_key) or ""  # 从账号独立记录文件兜底读今日收益
    return {  # 统一扁平契约:键名即持久化字段,所有展示出口共用
        "checkin_gain_flow": gain_flow or "--",  # 今日签到收益;未签/取不到显示占位
        "flow_remain": info.get("remain_flow", "--"),  # 账户剩余流量
        "flow_today": info.get("today_used", "--"),  # 今日已用流量
        "flow_total_used": info.get("past_used", "--"),  # 历史累计已用流量
        "last_use_time": info.get("last_use_text", "--"),  # 上次使用代理时间(或"从未使用")
        "last_sign_time": info.get("last_sign_day", "--"),  # 上次签到时间
        "signed_today": signed_today,  # 今日是否已签到(布尔)
        "can_sign": bool(info.get("can_checkin", False)),  # 当前是否可签到(按钮可点)
        "sub_v2ray": sub_links.get("v2ray", ""),  # V2Ray 订阅链接
        "sub_clash": sub_links.get("clash", ""),  # Clash 订阅链接
        "sub_clash_pro": sub_links.get("clash_pro", ""),  # ClashPro 订阅链接
        # 页面解析告警透传,供统一详情文本渲染;非列表兜底为空列表
        "parse_warns": info.get("parse_warns", [])  # 解析降级告警(如未抓到某节点)
        if isinstance(info.get("parse_warns"), list) else [],  # 类型不符时强制空列表,防止渲染期迭代报错
        # email不展示在详情文本里,但供调用方做持久化key兜底
        "email": email_key,  # 账号邮箱,持久化/记录回填的 key
    }


def refresh_user_info_after_checkin(
    service: "ServiceApiService",
    email: str,
    old_info: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """签到成功/已签跳过后重新拉取一次用户中心,获取最新页面状态

    必要性:
    - 签到成功:签到前拉取的user_info按钮可点(today_checked=False),直接持久化
      会误判"今日未签到",必须重拉拿禁用态按钮/今日已签标记/最新签到时间
    - 已签跳过(重复签到):同样需要最新页面状态,但页面本身不含收益字段,
      重拉后checkin_gain_flow会被cleaning_html清空,需保护不丢失已回填收益

    收益字段保护(不覆盖回填值):
    1. fill_persisted_checkin_result 从 checkin_records 记录文件回填今日收益
    2. 记录文件兜底仍为空时,继承 old_info 中已回填的 checkin_gain_flow
       (重复签到场景下,旧快照可能已从记录文件回填过收益)
    :param service: 已登录的 ServiceApiService 实例,用于再次请求用户中心
    :type service: ServiceApiService
    :param email: 当前任务账号邮箱,页面解析不到邮箱时兜底
    :type email: str
    :param old_info: 签到前的user_info快照,用于收益字段兜底继承;可选
    :type old_info: Optional[Dict[str, Any]]
    :return: 新user_info字典;拉取/解析失败返回None,调用方保留原快照
    :rtype: Optional[Dict[str, Any]]
    """
    resp = service.get_user_info()  # 重新请求用户中心页面
    if isinstance(resp, dict) and "error" in resp:  # service 层网络失败时返回 {"error": ...} 而非响应对象
        _logger.warning(f"👤 签到后重新拉取用户中心失败:{resp.get('error')}")  # 告警并保留旧快照
        return None  # 网络失败:调用方继续使用签到前 info
    http_status = getattr(resp, "status_code", "?")  # 取 HTTP 状态码用于诊断(对象无此属性时显示?)
    resp_text = getattr(resp, "text", "")  # 取 HTML 正文
    _logger.debug(f"👤 重拉用户中心 HTTP {http_status},响应长度:{len(resp_text) if resp_text else 0}")  # 调试:状态码+正文长度
    info = cleaning_html(resp_text)  # 重新解析 HTML 为结构化字典
    if not isinstance(info, dict):  # 解析异常兜底(正常 cleaning_html 恒返回 dict)
        _logger.warning("👤 签到后用户中心页面解析失败,保留签到前快照")  # 解析失败不覆盖旧数据
        return None  # 返回 None 让调用方保留旧快照
    # 记录重拉解析结果,便于诊断"上次使用时间"等字段抓取失败问题
    _logger.info(  # info 级输出关键字段抓取结果
        f"👤 重拉解析结果:last_use={info.get('last_use_text')!r}, "  # 上次使用时间(repr 暴露空白/空串)
        f"last_sign={info.get('last_sign_day')!r}, "  # 上次签到时间
        f"today_checked={info.get('today_checked')}, "  # 今日已签标记
        f"warns={info.get('parse_warns', [])}"  # 解析告警列表
    )
    # 调试转储:解析不到"上次使用时间"时,把原始HTML保存到调试文件供分析
    if "未抓取上次使用时间" in info.get("parse_warns", []) and resp_text:  # 命中特定告警且有正文
        _dump_debug_html(resp_text, "last_use_missing")  # 转储 debug_last_use_missing_<时间戳>.html
    # 页面没解析出邮箱时用任务账号兜底,保证收益回填按正确账号落盘
    if not info.get("email"):  # Cloudflare 混淆解析失败等场景
        info["email"] = email  # 用任务账号兜底
    # 保护已回填的签到状态与收益字段:
    # - today_checked:已签跳过/签到成功场景下旧快照已确认为True,重拉若解析不到
    #   按钮状态(返回False),不能覆盖,否则signed_today=False导致收益显示暂无
    # - checkin_gain_flow:页面本身不含此字段,cleaning_html返回空串,先从记录文件
    #   回填;仍为空则继承旧快照中的值,绝不把已回填收益覆盖成空
    if isinstance(old_info, dict):  # 存在旧快照才做状态保护
        if old_info.get("today_checked") and not info.get("today_checked"):  # 旧确认已签、新页面没解析出来
            info["today_checked"] = True  # 强制保留已签状态,防止偶发解析失败致漏签显示
            info["can_checkin"] = False  # 与已签态保持一致:不可再签
    if not str(info.get("checkin_gain_flow", "") or "").strip():  # 重拉后收益为空(页面本不含该字段)
        fill_persisted_checkin_result(info)  # 第一步:从账号记录文件回填
        if not str(info.get("checkin_gain_flow", "") or "").strip() and isinstance(old_info, dict):  # 记录文件也空且有旧快照
            old_gain = str(old_info.get("checkin_gain_flow", "") or "").strip()  # 取旧快照已回填收益
            if old_gain:  # 旧快照收益非空
                info["checkin_gain_flow"] = old_gain  # 第二步:继承旧值,绝不丢失
                _logger.info(f"ℹ️ 重拉后收益为空,继承旧快照收益:{old_gain}")  # 留痕说明收益来源
    _logger.info("👤 签到后用户中心已重新拉取,按钮状态/签到时间/今日收益已刷新")  # 完成日志
    return info  # 返回受保护后的最新用户信息


def is_already_checked_in(user_info: Optional[Dict[str, Any]]) -> bool:
    """根据用户中心页面按钮状态判断今日是否已签到

    网站规则:签到完成后签到按钮变为禁用态(a.btn.disabled/"今日已签到"),
    cleaning_html据此置today_checked=True。本函数即"已签到则不调用
    /user/checkin"的唯一判断口径,单账号与批量Worker共用。
    注意:仅在【明确已签到】时返回True以跳过签到接口;页面未抓到按钮等
    状态不明(today_checked/can_checkin均为初始False)时返回False,
    允许正常尝试签到,避免偶发解析失败导致永久漏签。
    :param user_info: cleaning_html 产物;None/非 dict 视为状态不明
    :type user_info: Optional[Dict[str, Any]]
    :return: True=明确今日已签(跳过签到接口);False=未签或状态不明(允许尝试)
    :rtype: bool
    """
    if not isinstance(user_info, dict):  # 无解析结果
        return False  # 类型不符按"未明确已签"处理,允许尝试签到
    return bool(user_info.get("today_checked"))  # 唯一口径:只认真阳性的已签标记


def try_cookie_login(service: "ServiceApiService", email: str) -> tuple[bool, Optional[Dict[str, Any]], str]:
    """
    根据邮箱尝试加载对应账号的Cookie免登
    ⚠️调用前必须执行 service.session_open()
    :param service: ServiceApiService实例
    :type service: ServiceApiService
    :param email: 需要登录的账号邮箱
    :type email: str
    :return: (是否有效, user_info字典/None, 错误消息)
    :rtype: tuple[bool, Optional[Dict[str, Any]], str]
    :raises Exception: 不主动抛出;网络/解析异常由 service 层封装为 error 字典
    """
    cookie_path = service.get_cookie_filename_by_email(email)  # 按邮箱推导 cookie_<邮箱>.json 路径
    service.cookie_file = cookie_path  # 告诉 service 本次要加载的 cookie 文件位置
    _logger.info(f"🍪 尝试Cookie免登:{email} ({cookie_path})")  # 免登尝试留痕
    load_ret = service.load_cookie_session()  # 加载 cookie 到 requests 会话,返回 ret/error 结构
    if load_ret.get("ret") != 1:  # ret!=1:cookie 文件不存在/损坏/过期格式
        err = load_ret.get("error", f"该账号[{email}]无可用Cookie")  # 取具体原因,缺省给通用提示
        _logger.info(f"🍪 Cookie免登失败,将回退密码登录:{err}")  # info 级:这是正常降级路径
        return False, None, err  # 通知调用方走密码登录
    _logger.debug("🍪 Cookie文件加载成功,正在用Cookie访问用户中心验证有效性")  # 文件加载≠会话有效,需请求验证
    resp = service.get_user_info()  # 带 cookie 访问用户中心,验证登录态
    if isinstance(resp, dict) and "error" in resp:  # 网络层失败
        service.is_logged_in = False  # 显式标记未登录
        _logger.warning(f"🍪 Cookie访问用户中心失败:{resp.get('error')}")  # 告警
        return False, None, f"访问用户中心失败：{resp.get('error')}"  # 返回失败三元组
    resp_text = getattr(resp, "text", "")  # 取用户中心 HTML
    user_info = cleaning_html(resp_text)  # 解析页面,关键是拿到当前登录邮箱
    parsed_email = user_info.get("email", "").strip()  # 页面呈现的账号邮箱(已解 Cloudflare 混淆)
    if not parsed_email or parsed_email != email:  # 解析不到或 cookie 属于别的账号(串号校验)
        service.is_logged_in = False  # 判定会话失效,标记未登录
        _logger.info(f"🍪 Cookie会话与目标账号不匹配(解析到:{parsed_email or '空'}),判定失效")  # 留痕不匹配原因
        return False, None, f"Cookie失效或者不是账号[{email}]的会话"  # 失败,回退密码登录
    _logger.info(  # 三重校验通过:文件可用+页面可访问+账号一致
        f"✅ Cookie免登成功:{email} | last_use={user_info.get('last_use_text')!r} "  # 上次使用时间
        f"warns={user_info.get('parse_warns', [])}"  # 顺带记录解析告警
    )
    if "未抓取上次使用时间" in user_info.get("parse_warns", []):  # 免登路径同样可能遇到页面结构变化
        _dump_debug_html(resp_text, "cookie_last_use_missing")  # 转储 HTML 供离线排查
    return True, user_info, ""  # 免登成功,直接复用解析结果,错误消息为空串


def _dump_debug_html(html_text: str, tag: str) -> None:
    """把原始HTML保存到调试文件,供字段解析失败时排查页面结构
    文件路径:日志目录/debug_<tag>_<时间戳>.html
    :param html_text: 待转储的原始 HTML 正文
    :type html_text: str
    :param tag: 文件名语义标签,如 last_use_missing
    :type tag: str
    :return: 无返回值;失败仅告警
    :rtype: None
    :raises Exception: 内部捕获全部异常,不影响签到主流程
    """
    try:  # 调试能力本身不能影响业务,全包 try
        import time  # 函数内再次导入 time(模块顶部已导入,保留原写法不动)
        from pathlib import Path  # 函数内延迟导入(实际未使用,保留原 import 不动)

        from runtime.model.app_config import get_log_file_path  # 延迟导入:复用日志文件路径推导日志目录
        log_dir = os.path.dirname(get_log_file_path())  # app.log 所在目录即调试文件目录
        os.makedirs(log_dir, exist_ok=True)  # 目录不存在则创建,已存在不报错
        ts = time.strftime("%Y%m%d_%H%M%S")  # 本地时间戳,避免同轮调试文件互相覆盖
        fp = os.path.join(log_dir, f"debug_{tag}_{ts}.html")  # 拼接调试文件完整路径
        with open(fp, "w", encoding="utf-8") as f:  # UTF-8 覆盖写
            f.write(html_text)  # 原样写入 HTML,可用浏览器直接打开对照 DOM
        _logger.info(f"🔍 调试HTML已保存:{fp}")  # 告知文件位置
    except Exception as e:  # 任意失败(权限/路径/编码)
        _logger.warning(f"🔍 调试HTML保存失败:{e}")  # 仅告警,不抛出


def cleaning_html(htmltext: str) -> Dict[str, Any]:
    """
    解析V2free用户中心HTML
    ⚠注意:优先DOM签到状态;时间推断guess_check_status_by_time做降级兜底
    :param htmltext: get_user_info返回response的 .text 网页源码字符串
    :type htmltext: str
    :return: 结构化用户信息字典;输入为空时返回带告警的全默认结构
    :rtype: Dict[str, Any]
    """
    result: Dict[str, Any] = {  # 解析结果骨架,任何字段抓不到都保留默认值
        "email": "",  # 账号邮箱(需解 Cloudflare __cf_email__ 混淆)
        "dom_can_checkin": False,  # DOM 按钮是否可点(原始页面信号)
        "dom_checkin_text": "",  # 签到按钮原始文案
        "can_checkin": False,  # 综合判定:当前是否可签到
        "today_checked": False,  # 综合判定:今日是否已签到
        "last_sign_day": "",  # 上次签到时间字符串
        "remain_flow": "",  # 剩余流量
        "checkin_gain_flow": "",  # 今日签到收益(页面本身不含,由持久化回填)
        "today_used": "",  # 今日已用流量
        "past_used": "",  # 过去累计已用流量
        "last_use_text": "",  # 上次使用代理时间/"从未使用"
        "parse_warns": [],  # 解析降级告警收集器,渲染到详情文本
        "sub_links": {  # 三种订阅链接
            "v2ray": "",  # V2Ray 订阅
            "clash": "",  # Clash 订阅
            "clash_pro": ""  # ClashPro 订阅
        }
    }
    if not htmltext or not htmltext.strip():  # None/空串/纯空白均视为无效页面
        result["parse_warns"].append("html文本为空")  # 记录告警,调用方可感知解析失败原因
        return result  # 提前返回默认骨架,避免后续解析空字符串报错
    soup = BeautifulSoup(htmltext, "html.parser")  # 用 Python 内置 html.parser 构建 DOM(无需 lxml)
    # 0. 用户中心‑账号邮箱解析（处理Cloudflare __cf_email__混淆）
    email = ""  # 先初始化为空
    heading_el = soup.select_one("b.content-heading")  # 定位账号标题区 <b class="content-heading">
    if heading_el:  # 标题节点存在才继续找混淆邮箱
        cf_a_tag = heading_el.select_one("a.__cf_email__")  # Cloudflare 邮箱保护标签 <a class="__cf_email__">
        if cf_a_tag and cf_a_tag.has_attr("data-cfemail"):  # 标签与 data-cfemail 属性同时存在
            cf_hex = cf_a_tag.get("data-cfemail", "")  # 取混淆十六进制串,如 "a1d8d2..."
            if not isinstance(cf_hex, str):  # 属性值理论为 str,防御异常类型
                cf_hex = ""  # 归一为空,后续解码失败
            try:  # 混淆串可能被截断/含非hex字符
                key = int(cf_hex[0:2], 16)  # Cloudflare 算法:首字节为 XOR 密钥
                email_bytes = bytearray()  # 累积解码后的字节
                for i in range(2, len(cf_hex), 2):  # 从第3个字符起,每两个hex字符为一字节
                    val = int(cf_hex[i:i+2], 16) ^ key  # 每字节与 key 异或还原
                    email_bytes.append(val)  # 收集明文字节
                email = email_bytes.decode("utf-8").strip()  # 字节按 UTF-8 解码为邮箱字符串
            except Exception:  # hex 非法/长度奇/UTF-8 解码失败
                email = ""  # 解码失败置空,稍后记告警
    result["email"] = email  # 写回结果
    if not email:  # 未拿到邮箱会影响 cookie 串号校验与收益落盘
        result["parse_warns"].append("未解析到账号邮箱")  # 记录降级告警
    # 1.剩余流量
    el_remain = soup.select_one("#remain")  # 按 id 定位剩余流量节点 <... id="remain">
    if el_remain:  # 节点存在
        result["remain_flow"] = el_remain.get_text(strip=True)  # 取纯文本并去空白,如 "12.3GB"
    else:  # 页面结构变更或未加载
        result["parse_warns"].append("未抓取剩余流量节点")  # 告警
    # 2.今日已用流量
    li_today_used = None  # 先初始化为空
    for li in soup.select("li.nodehead.node-flex"):  # 遍历头部信息区所有 <li class="nodehead node-flex">
        txt = li.get_text(strip=True)  # 取整行纯文本
        if txt.startswith("今日已用:"):  # 按前缀识别目标行
            li_today_used = li  # 命中目标节点
            break  # 找到即停
    if li_today_used:  # 找到了今日已用行
        a_tag = li_today_used.find("a")  # 数值包在该行内的 <a> 标签中
        if a_tag:  # <a> 存在
            result["today_used"] = a_tag.get_text(strip=True)  # 取用量数值文本
    else:  # 整行未找到
        result["parse_warns"].append("未抓取今日已用流量")  # 告警
    #3.过去已用流量
    li_past_used = None  # 初始化
    for li in soup.select("li.nodehead.node-flex"):  # 同一组 <li> 中二次遍历
        txt = li.get_text(strip=True)  # 行文本
        if txt.startswith("过去已用:"):  # 前缀匹配"过去已用:"
            li_past_used = li  # 命中
            break  # 停止搜索
    if li_past_used:  # 找到目标行
        a_tag = li_past_used.find("a")  # 用量值在 <a> 内
        if a_tag:  # 节点存在
            result["past_used"] = a_tag.get_text(strip=True)  # 取累计用量文本
    else:  # 未找到
        result["parse_warns"].append("未抓取过去已用流量")  # 告警
    #4.上次使用时间
    # 页面结构可能变化:优先在 li.nodehead.node-flex 中查找,找不到再 fallback 到 div;
    # 文本匹配用"上次使用"包含判断(而非严格startswith("上次使用:")),适配前缀差异;
    # 时间用正则提取,兼容"上次使用:2026-09-13 23:00:28"等格式;
    # 特殊情况:账号从未使用过代理时,页面显示"从未使用",直接透传该文本
    li_last_use = None  # 初始化
    for li in soup.select("li.nodehead.node-flex"):  # 第一轮:标准 li 结构
        txt = li.get_text(strip=True)  # 行文本
        if "上次使用" in txt or txt == "从未使用":  # 包含匹配 + 未使用特例
            li_last_use = li  # 命中
            break  # 停止
    if li_last_use is None:  # li 层没找到,降级
        for div in soup.select("div"):  # 第二轮:全页面 div 兜底扫描
            t = div.get_text(strip=True)  # div 文本
            if t.startswith("上次使用") or t == "从未使用":  # 前缀匹配或未使用特例
                li_last_use = div  # 命中降级节点
                break  # 停止
    if li_last_use:  # 成功定位节点
        raw_txt = li_last_use.get_text(strip=True)  # 取整行文本待正则处理
        if raw_txt == "从未使用":  # 账号从未用过代理,页面无时间
            # 账号从未使用过代理,页面无时间,直接显示"从未使用"
            result["last_use_text"] = "从未使用"  # 原样透传语义文案
        else:  # 正常含时间文本
            # 优先用正则提取时间,兼容有无冒号/空格差异
            m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", raw_txt)  # 匹配 YYYY-MM-DD HH:MM:SS
            if m:  # 命中标准时间
                result["last_use_text"] = m.group(1)  # 只保留时间本体,剔除标签杂质
            else:  # 时间格式变化时兜底
                # 兜底:去掉"上次使用"前缀后的剩余文本
                result["last_use_text"] = re.sub(r"^上次使用[::]?\s*", "", raw_txt).strip()  # 去前缀(兼容半角/全角冒号)
    else:  # 两轮查找均失败
        result["parse_warns"].append("未抓取上次使用时间")  # 告警,调用方可能触发 HTML 转储
    #5.签到历史时间
    last_sign_div = None  # 初始化
    for div in soup.select("div"):  # 遍历所有 div 找"上次签到:"
        t = div.get_text(strip=True)  # div 文本
        if t.startswith("上次签到:"):  # 前缀识别
            last_sign_div = div  # 命中
            break  # 停止
    if last_sign_div:  # 找到签到历史节点
        full_text = last_sign_div.get_text(strip=True)  # 取完整文本
        match_line = re.search(r"上次签到:(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", full_text)  # 精确捕获签到时间
        if match_line:  # 时间格式符合预期
            time_str = match_line.group(1)  # 取捕获组时间
            result["last_sign_day"] = time_str  # 写入上次签到时间
    else:  # 节点缺失
        result["parse_warns"].append("未抓取上次签到信息")  # 告警
    #5.1 DOM签到按钮状态解析
    dom_can_checkin = False  # 默认不可签
    dom_checkin_text = ""  # 默认无文案
    signed_a = soup.select_one("a.btn.disabled")  # 已签态:禁用的 <a class="btn disabled">
    if signed_a:  # 存在禁用按钮 => 今日已签
        dom_checkin_text = signed_a.get_text(strip=True)  # 取按钮文案,如"今日已签到"
        if "今日已签到" in dom_checkin_text:  # 文案确认为已签
            dom_can_checkin = False  # 明确不可签
    else:  # 无禁用按钮,再找可点的签到按钮
        checkin_btn = soup.select_one("button#checkin")  # 未签态:<button id="checkin">
        if checkin_btn:  # 按钮存在
            dom_checkin_text = checkin_btn.get_text(strip=True)  # 取文案,如"点我签到获取流量"
            if "点我签到获取流量" in dom_checkin_text:  # 文案确认为可签
                dom_can_checkin = True  # 标记可签到
    result["dom_can_checkin"] = dom_can_checkin  # 落原始 DOM 信号
    result["dom_checkin_text"] = dom_checkin_text  # 落按钮文案
    if dom_checkin_text:  # 抓到过按钮(任一态)才采用 DOM 判定,避免无按钮时误判
        result["can_checkin"] = dom_can_checkin  # 可签状态直接取 DOM
        result["today_checked"] = not dom_can_checkin  # 已签=按钮不可点的反面
    #6.订阅链接
    def _attr_str(tag, attr_name: str, default: str = "") -> str:
        """BeautifulSoup属性值可能为str或list[str],统一归一化为str
        :param tag: BeautifulSoup 标签对象
        :type tag: bs4.element.Tag
        :param attr_name: 待读取的属性名
        :type attr_name: str
        :param default: 异常/缺失时的兜底返回值
        :type default: str
        :return: 属性字符串值;非 str(如多值 list)时返回 default
        :rtype: str
        """
        val = tag.get(attr_name, default)  # 读自定义属性,缺失给默认值
        return val.strip() if isinstance(val, str) else default  # 仅 str 去空白;list 等异常形态直接兜底

    v2ray_a = soup.select_one('a.copy-text[data-clipboard-text*="sub=3"]')  # 属性包含选择器:V2Ray 复制按钮(sub=3)
    if v2ray_a:  # 节点存在
        result["sub_links"]["v2ray"] = _attr_str(v2ray_a, "data-clipboard-text")  # 订阅链接在剪贴板自定义属性里
    else:  # 未找到
        result["parse_warns"].append("未找到v2ray订阅链接DOM")  # 告警
    clash_a = soup.select_one('a.copy-text[data-clipboard-text*="clash=2"]')  # Clash 复制按钮(clash=2)
    if clash_a:  # 节点存在
        result["sub_links"]["clash"] = _attr_str(clash_a, "data-clipboard-text")  # 取订阅 URL
    else:  # 未找到
        result["parse_warns"].append("未找到clash订阅链接DOM")  # 告警
    clash_pro_a = soup.select_one('a.copy-text[data-clipboard-text*="clash=pro"]')  # ClashPro 复制按钮(clash=pro)
    if clash_pro_a:  # 节点存在
        result["sub_links"]["clash_pro"] = _attr_str(clash_pro_a, "data-clipboard-text")  # 取订阅 URL
    else:  # 未找到
        result["parse_warns"].append("未找到clash_pro订阅链接DOM")  # 告警
    fill_persisted_checkin_result(result)  # 页面无收益字段,用账号记录文件兜底回填上次收益
    warns = result.get("parse_warns", [])  # 收集本轮全部告警
    if warns:  # 存在降级项
        _logger.debug(f"🔍 用户中心HTML解析完成,{len(warns)}条降级警告:{';'.join(warns[:5])}")  # 最多打印前5条防爆日志
    else:  # 全部字段正常
        _logger.debug("🔍 用户中心HTML解析完成,全部字段抓取正常")  # 正常完成日志
    return result  # 返回结构化用户信息


def format_sign_duration(cost_sec: Any) -> str:
    """把账号签到耗时(秒,float)格式化成中文文案:不足1分显示"8.6秒",否则"1分23.4秒"

    非数值/负数/None一律返回"未知",供详情块与批量失败占位块统一使用
    :param cost_sec: 耗时秒数(期望 int/float),容忍任意异常类型
    :type cost_sec: Any
    :return: 中文耗时文案;非法输入返回"未知"
    :rtype: str
    """
    try:  # 入参可能是 None/str/异常对象
        sec = float(cost_sec)  # 强转 float 统一计算
    except (TypeError, ValueError):  # None、非数字字符串等
        return "未知"  # 非法输入占位
    if sec < 0:  # 负数无物理意义(计时异常)
        return "未知"  # 同样占位
    if sec < 60:  # 不足1分钟
        return f"{sec:.1f}秒"  # 保留1位小数,如"8.6秒"
    minutes = int(sec // 60)  # 整除取分钟数
    remain = sec - minutes * 60  # 余下不足1分钟的秒数
    return f"{minutes}分{remain:.1f}秒"  # 如"1分23.4秒"


def short_flow_text(text: str) -> str:
    """把签到获得流量文案精简为纯数值+单位,消除"签到获得:获得了…流量"语义重复

    "获得了 332MB 流量." → "332MB";提取不到数值+单位时去掉句末句点原样返回
    :param text: 签到接口原始 msg 文案
    :type text: str
    :return: 精简后的"数值+大写单位";无法提取时返回去句点的原文;空输入返回""
    :rtype: str
    """
    raw = str(text or "").strip()  # None 安全转字符串并去首尾空白
    if not raw:  # 空文案
        return ""  # 直接返回空串
    # 不用\b:Unicode模式下中文算单词字符,"MB流量"的B与流之间无单词边界
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)(?![A-Za-z])", raw, re.IGNORECASE)  # 捕获数字+单位;负向断言防匹配到单词中段;忽略大小写
    if not m:  # 页面文案变更,没匹配到数值单位
        return raw.rstrip("。.")  # 仅去掉中英文句末句号,其余原样展示
    num = m.group(1)[:-2] if m.group(1).endswith(".0") else m.group(1)  # 整数的 ".0" 尾巴去掉(332.0->332),小数保留
    return f"{num}{m.group(2).upper()}"  # 单位统一大写,输出如"332MB"/"1.5GB"


def format_user_info_text(
    email: str,
    detail: Optional[Dict[str, Any]],
    title: str = "📋 用户账号信息",
) -> str:
    """把账号详情(user_info_to_account_detail的扁平契约)格式化成统一纯文本块

    【唯一】账号详细信息渲染出口:界面详情面板(Controller渲染)、单账号邮件正文、
    批量汇总邮件逐账号块、签到结果文件全部走此函数,任何出口口径完全一致。
    :param email: 账号邮箱(持久化detail中不存邮箱,由调用方传入)
    :type email: str
    :param detail: 账号详情dict;None/非dict时返回全占位块,不抛异常
    :type detail: Optional[Dict[str, Any]]
    :param title: 文本块标题,默认"📋 用户账号信息"
    :type title: str
    :return: 多行纯文本账号详情块(换行符拼接)
    :rtype: str
    """
    d = detail if isinstance(detail, dict) else {}  # 容错:非字典归一为空字典
    email = (email or "").strip() or "未获取"  # 邮箱去空白;空值显示占位
    signed_today = bool(d.get("signed_today"))  # 今日已签标记
    can_sign = bool(d.get("can_sign"))  # 当前可否签到
    # 按钮状态只能从持久化的两个布尔还原(原始dom文案不落盘):
    # 可签到→立即签到;今日已签→今日已签到;两者皆否为异常占位
    if can_sign:  # 明确可签
        sign_btn = "🟢 可签到 | 【立即签到】"  # 绿色可签态
    elif signed_today:  # 不可签但今日已签
        sign_btn = "🔴 已签到 | 【今日已签到】"  # 红色已签态
    else:  # 两个布尔都为假:页面没抓到按钮等异常态
        sign_btn = "⚪ 未知 | 【未知】"  # 灰色未知占位
    sign_status_cn = "🟢 已签到" if signed_today else "🔴 未签到"  # 今日签到行的独立口径
    # 签到获得:"--"/空(今日未签)显示占位,不展示历史收益
    checkin_gain_flow = str(d.get("checkin_gain_flow", "") or "").strip()  # 取今日收益文本
    checkin_msg = short_flow_text(checkin_gain_flow) if checkin_gain_flow not in ("", "--") else " -- "  # 有效则精简,否则占位
    # 签到耗时:Worker用time.perf_counter实测后随detail落盘(本账号处理总耗时,
    # 含登录/防风控等待/签到/重拉);重开软件展示上次值,跨天归一化后为"未知"
    cost_text = format_sign_duration(d.get("checkin_cost_sec"))  # 秒值格式化为中文耗时
    warns = d.get("parse_warns")  # 解析告警列表
    if not isinstance(warns, list):  # 类型防御
        warns = []  # 归一空列表
    lines = [  # 文本块行序列,顺序即展示顺序
        "=" * 34,  # 顶部分隔线
        title,  # 标题
        "-" * 47,  # 标题下分隔线
        f" 👤  用户邮箱    :   {email}",  # 邮箱行
        f" 🔖  签到状态    :   {sign_btn}",  # 按钮还原态
        f" ⏱️  签到耗时    :   {cost_text}",  # 本次签到耗时
        f" 🕒  上次签到    :   {d.get('last_sign_time', '--')}",  # 上次签到时间
        f" 🗓️  今日签到    :   {sign_status_cn}",  # 今日是否已签
        f" 🎁  签到获得    :   {checkin_msg}",  # 今日收益(未签显示占位)
        f" 💾  剩余流量    :   {d.get('flow_remain', '--')}",  # 剩余流量
        f" 📊  今日已用    :   {d.get('flow_today', '--')}  |   过去已用: {d.get('flow_total_used', '--')}",  # 今日+累计用量
        f" ⌚  上次使用    :   {d.get('last_use_time', '--')}",  # 上次使用代理时间
        "-" * 47,  # 信息区与订阅区间隔线
        "🎁 订阅链接",  # 订阅区小标题
        f"🔗V2Ray: \n{d.get('sub_v2ray', '')}",  # V2Ray 链接(前置换行便于复制)
        f"🔗Clash: \n{d.get('sub_clash', '')}",  # Clash 链接
        f"🔗ClashPro: \n{d.get('sub_clash_pro', '')}",  # ClashPro 链接
    ]
    if warns:  # 有解析降级告警时追加展示,帮助定位页面变更
        lines.append("-" * 47)  # 告警区分隔线
        lines.append("⚠️ 解析警告列表:")  # 告警标题
        lines.extend(f"   • {w}" for w in warns)  # 每条告警加项目符号
    lines.append("=" * 34)  # 底部分隔线
    return "\n".join(lines)  # 以换行拼成单一字符串返回


__all__ = [  # 模块公开 API 白名单,控制 from sign_tools import * 的导出范围
    "cleaning_html",  # HTML 解析主函数
    "format_sign_duration",  # 耗时格式化
    "short_flow_text",  # 流量文案精简
    "format_user_info_text",  # 详情文本统一渲染
    "fill_checkin_result",  # 签到接口结果回填
    "save_last_checkin_gain",  # 收益落盘
    "load_last_checkin_gain",  # 收益读取
    "fill_persisted_checkin_result",  # 持久化收益回填内存
    "refresh_user_info_after_checkin",  # 签到后重拉用户中心
    "is_already_checked_in",  # 今日已签唯一判定
    "try_cookie_login",  # Cookie 免登
    "user_info_to_account_detail",  # 用户信息->详情契约映射
]
