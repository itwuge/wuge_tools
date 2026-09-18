# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: batch_sign_worker.py
# 归属: runtime/workers 后台任务线程层 —— 多账号批量签到 Worker
# ------------------------------------------------------------------------------
# 文件用途:
#   多账号批量签到后台线程实现模块.顺序对每个账号执行"清旧(删除历史详情/
#   收益记录)→ 登录签到 → 存新(本次结果落盘)"流水线,账号之间另加 10-50 秒
#   防风控随机间隔;全部账号结束后把"统计+明细"汇总写入签到结果文件,
#   并[只发一封]汇总邮件.
# ------------------------------------------------------------------------------
# 架构定位:
#   本模块是 QThread 工作线程,通过 Signal 向 controller/view 发送事件,
#   不直接操作 UI 控件.只调用 service.run_ba 业务封装与 runtime.model 的
#   工具/持久化 API,不写业务算法,不 import 任何 View/Model 界面类,
#   严格遵循分层架构.另在 root logger 临时挂载自定义 Handler,
#   把业务层日志桥接成 signal_log 信号推送到界面.
#
#   关联组件:
#     - 上游: Controller(批量签到控制)
#     - 下游: runtime.model.account_store、runtime.model.app_config、
#             runtime.model.sign_tools、service.run_ba.ServiceApiService、
#             service.send_email.ServiceEmailSender
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 逐账号执行 Cookie/密码登录、已签到判定、防风控延时、签到接口调用
#   2. 逐账号映射 detail 落盘,并实时发 signal_item_done 刷新"今日已签到"统计
#   3. 账号之间可中断随机间隔,响应关窗/取消操作
#   4. 汇总统计与逐账号明细写结果文件,按开关只发一封汇总邮件(发送器懒加载复用)
#   5. 任务级异常兜底:空结果列表 + 失败结果文件,避免界面卡死
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只调用 service.run_ba 业务封装与 runtime.model 的工具/持久化 API
#   - 不写业务算法,不 import 任何 View/Model 界面类
#   - 单账号异常 → 记录失败但不中断批量;任务级异常 → 写失败结果文件
# ------------------------------------------------------------------------------
# 线程模型:
#   - signal_log(str): 日志文本,在工作线程 emit
#   - signal_progress(int): 按已完成账号数换算百分比,在工作线程 emit
#   - signal_item_done(dict): 单个账号处理完即发,在工作线程 emit
#   - signal_result(list): 全部账号结果列表,在工作线程 emit
#   以上信号均由 Qt 自动以队列连接(QueuedConnection)跨线程投递给 UI 线程.
#   底层业务日志通过挂载到 root logger 的 _QtLogBridge 桥接到 UI 信号.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、random、datetime.datetime、time.perf_counter、typing
#   - 第三方: PySide6.QtCore.Signal
#   - 项目内:
#       runtime.workers.interruptible_sleep
#       runtime.workers.base_worker.BaseWorker
#       run() 内延迟导入:
#           runtime.model.account_store
#           runtime.model.app_config
#           runtime.model.sign_tools
#           service.run_ba.ServiceApiService
#       _send_summary_mail 内延迟导入:
#           service.send_email.ServiceEmailSender
# ==============================================================================

# ==============================================================================
# [批量签到 Worker - QThread 胶水层]
# 职责: 后台线程顺序执行多个账号,每个账号严格按流水线执行:
#       清旧(删历史详情/收益记录)→ 登录签到 → 存新(本次结果落盘);
#       账号之间另加 10-50 秒防风控随机间隔;
#       全部账号结束后把"统计+明细"汇总写入签到结果文件,并[只发一封]汇总邮件
# 边界: 只调用 service.run_ba 业务封装与 comm_tools 工具/持久化 API,
#       不写业务算法,不 import 任何 View/Model;
#       严禁引用 service/service.py、text_service.py 等测试入口(将来会删除)
# 信号: signal_log(str) / signal_progress(int) / signal_item_done(dict) / signal_result(list)
# 日志: 每一步同时写入 app.log(带 emoji 图标)并推送到界面日志区
# ==============================================================================
import logging  # 导入标准库日志模块:用于分级日志落盘,并提供 Handler 基类做 Qt 信号桥接
import random  # 导入随机数模块:用于生成登录后与账号间的防风控等待秒数
from datetime import datetime  # 从 datetime 模块导入 datetime 类:用于生成汇总文件的任务时间戳
from time import perf_counter  # 从 time 模块导入 perf_counter 函数:单调高精度计时器,用于统计单账号签到耗时
from typing import Any, Dict, List, Optional  # 从 typing 导入类型标注工具:用于容器/可选值的类型注解

from PySide6.QtCore import Signal  # 从 PySide6.QtCore 导入 Signal:Qt 信号定义工具,用于向 UI 线程回传事件

from runtime.workers import interruptible_sleep  # 从本包导入可中断分段睡眠函数:防风控等待期间可响应取消
from runtime.workers.base_worker import (
    BaseWorker,  # 导入 Worker 基类:提供 QThread 基础能力与日志双写功能
)


# ==============================================================================
# [日志桥接器]把 logging 日志记录桥接成 Qt 信号
# 挂载到 root logger 后,业务层所有日志自动 propagate 到这里,
# 再通过 Qt 信号投递给 UI 线程的日志区显示
# ==============================================================================
class _QtLogBridge(logging.Handler):
    """把 logging 记录桥接成 Qt 信号(在 Worker 线程 emit,Qt 自动以队列连接投递给 UI 线程).

    通过继承 logging.Handler 实现自定义日志处理器,将业务层的 logging 输出
    转换为 Qt 信号推送到界面.任何桥接异常都静默吞掉,不影响签到主流程.
    """

    def __init__(self, emit_cb):
        """初始化日志桥接 Handler.

        :param emit_cb: 实际发射信号的回调函数(通常为 self.signal_log.emit),
                        接收一条格式化后的日志文本字符串
        :type emit_cb: Callable[[str], None]
        :return: 无返回值
        :rtype: None
        """
        super().__init__(level=logging.INFO)  # 调用父类 Handler 构造函数,以 INFO 级别初始化处理器
        self._emit_cb = emit_cb  # 保存信号发射回调函数,供 emit() 方法调用
        # 界面日志带短模块名前缀,便于区分步骤来源
        self.setFormatter(logging.Formatter("[%(name)s] %(message)s"))  # 设置日志格式为"[logger名] 消息内容"

    def emit(self, record: logging.LogRecord) -> None:
        """把一条 LogRecord 格式化后交给信号回调发射.

        重写 logging.Handler 的 emit 方法,将日志记录格式化后通过 Qt 信号
        发射到 UI 线程.任何异常都静默吞掉,保证不影响主流程.

        :param record: logging 框架传入的日志记录对象,包含级别、消息、logger 名等信息
        :type record: logging.LogRecord
        :return: 无返回值
        :rtype: None
        """
        try:  # 桥接过程单独保护,任何异常都不外泄
            self._emit_cb(self.format(record))  # 按格式器格式化日志记录,并通过回调函数推送到界面
        except Exception:  # 吞掉桥接阶段的全部异常
            # 日志桥接任何异常都不得影响签到主流程
            pass  # 静默忽略,保证签到流程不受日志故障影响


# ==============================================================================
# [结果块渲染函数]把单个账号的批量结果渲染成汇总文本中的一个完整区块
# ==============================================================================
def _account_result_block(item: Dict[str, Any]) -> str:
    """把单个账号的批量结果渲染成汇总文本中的一个完整区块.

    只要走完映射落盘(新签成功/已签跳过/业务返回未成功),就用与界面面板、
    单账号邮件完全相同的 format_user_info_text 渲染 detail 契约;
    登录/流程异常(user_info 与 detail 均缺失)时渲染带耗时与原因的失败占位块.

    :param item: 单账号批量结果字典,含 email、checkin_ok、already_signed、
                 detail、cost_sec、error 等字段
    :type item: Dict[str, Any]
    :return: 该账号在汇总文本中的完整文本区块字符串
    :rtype: str
    """
    # 延迟导入,与 Worker.run 一致,避免启动阶段加载 bs4 等网络解析依赖
    from runtime.model.sign_tools import format_sign_duration  # 把耗时秒数格式化为可读文案
    from runtime.model.sign_tools import (
        format_user_info_text,  # 从签到工具模块导入耗时格式化与详情文本渲染函数; 把 detail 渲染为详情文本块
    )

    email = str(item.get("email", "")).strip() or "未知账号"  # 从结果项中取账号邮箱并去除空白,缺失时兜底为"未知账号"
    detail = item.get("detail")  # 从结果项中取持久化详情契约(异常账号该字段为 None)
    if isinstance(detail, dict):  # 判断是否有详情字典,有则说明流程走完映射落盘,渲染标准详情块
        if item.get("checkin_ok"):  # 判断是否为本次新签成功
            title = f"📋 {email} | ✅ 新签成功"  # 设置成功标题
        elif item.get("already_signed"):  # 判断是否为今日已签到而跳过接口
            title = f"📋 {email} | ℹ️ 今日已签到,已跳过签到接口"  # 设置跳过标题
        else:  # 走到接口但业务未成功的情况
            # 走到接口但业务未成功:仍把页面详情展示出来,标题标明失败
            title = f"📋 {email} | ❌ 签到未成功"  # 设置未成功标题
        return format_user_info_text(email, detail, title)  # 用统一渲染函数输出详情块
    cost_text = format_sign_duration(item.get("cost_sec"))  # 异常账号:格式化其已耗时长
    reason = item.get("error") or "未知原因"  # 取失败原因,缺省兜底为"未知原因"
    return (  # 返回失败占位区块字符串
        "=" * 34 + "\n"  # 区块顶部分隔线(34 个等号)
        f"📋 {email} | ❌ 签到失败\n"  # 失败标题行
        + "-" * 50 + "\n"  # 标题与正文分隔线(50 个减号)
        f" ⏱️  签到耗时    :   {cost_text}\n"  # 已耗时长行
        f"失败原因:{reason}\n"  # 失败原因行
        + "=" * 34  # 区块底部分隔线(34 个等号)
    )


# =========** [BatchSign]Worker - 多账号批量签到 **=========
class BatchSignWorker(BaseWorker):
    """多账号批量签到后台 Worker(QThread).

    顺序处理账号列表,每个账号走独立会话与"清旧→登录→签到→存新"流水线,
    通过信号实时上报进度与单项结果,结束后写汇总文件并按需发一封汇总邮件.
    单账号失败不影响整体批量,账号之间有防风控随机间隔.
    """

    # ------------------------------------------------------------------
    # 类属性(Qt 信号定义)
    # ------------------------------------------------------------------
    # 进度信号:载荷为 0~100 的进度百分比整数(int),按已完成账号数换算
    signal_progress = Signal(int)  # 定义进度信号,参数类型为整数,表示当前完成百分比
    # 单个账号处理完成(含新签成功/已签跳过/失败),payload 为该账号结果 dict
    signal_item_done = Signal(dict)  # 定义单项完成信号,参数类型为字典,携带单账号结果
    # 全部任务结果列表: [{email, login_ok, checkin_ok, user_info, error, ...}]
    signal_result = Signal(list)  # 定义结果信号,参数类型为列表,携带全部账号的结果

    # ------------------------------------------------------------------
    # 实例属性(在 __init__ 中初始化)
    # ------------------------------------------------------------------
    # self._account_list: List[Dict[str, str]] 待签到账号列表
    # self.config: dict 全局配置字典,包含代理/SMTP/邮件开关等配置节
    # self._mail_sender: ServiceEmailSender | None 汇总邮件发送器缓存,懒加载

    def __init__(self, account_list: List[Dict[str, str]], config: dict, parent=None):
        """初始化批量签到 Worker.

        :param account_list: 待签到账号列表,每项为含 email 和 password 键的字典,
                             格式如 [{"email": "...", "password": "..."}, ...]
        :type account_list: List[Dict[str, str]]
        :param config: 全局配置字典(与 SignMailWorker 入参一致,
                       含 proxy/smtp/user_info 等配置节)
        :type config: dict
        :param parent: Qt 父对象,用于线程生命周期管理,
                       通常为控制器或窗口对象
        :type parent: QObject | None
        :return: 无返回值
        :rtype: None
        """
        super().__init__(parent)  # 调用 BaseWorker 父类构造函数,初始化 QThread 并创建按类名命名的 logger
        self._account_list = account_list or []  # 保存待签到账号列表,None 时兜底为空列表
        self.config = config  # 持有全局配置字典(代理/SMTP/邮件开关等),供 run() 读取
        # 邮件发送器懒加载,整个批量任务只构造一次,各账号复用
        self._mail_sender = None  # 初始化汇总邮件发送器缓存为 None,首次发信时才创建

    def run(self) -> None:
        """QThread 线程入口:顺序执行全部账号的批量签到流水线.

        逐账号执行"清旧→登录→签到→存新"流水线,账号间防风控随机间隔,
        实时上报进度与单项结果,全部完成后写汇总文件并按需发汇总邮件.
        被取消时已完成账号的落盘保留,不发汇总信号、不发邮件.

        :return: 无返回值;结果列表通过 signal_result(list) 回传,
                 单项完成通过 signal_item_done(dict) 实时回传
        :rtype: None
        :raises Exception: 任务级异常在方法内部捕获并发射空结果信号,不向外抛出
        """
        # ------------------------------------------------------------------
        # 延迟导入区: 避免启动阶段加载网络库,加快应用启动速度
        # ------------------------------------------------------------------
        from runtime.model.account_store import (
            reset_account_sign_persistence,  # 清除账号历史签到持久化产物(详情快照/收益记录)
        )
        from runtime.model.account_store import (
            save_account_detail,  # 保存本次账号详情快照(以邮箱为 key)
        )
        from runtime.model.account_store import (
            write_checkin_result_text,  # 从账号存储模块导入清旧函数; 从账号存储模块导入详情保存函数; 从账号存储模块导入结果文件写入函数; 覆盖写签到结果文本文件 mail_body.txt
        )
        from runtime.model.app_config import resolve_effective_proxy  # 导入实际代理解析函数
        from runtime.model.sign_tools import (
            cleaning_html,  # 清洗用户中心 HTML 并解析为 user_info 字典
        )
        from runtime.model.sign_tools import (
            fill_checkin_result,  # 把签到接口返回的收益回填进 user_info 并落盘
        )
        from runtime.model.sign_tools import (
            fill_persisted_checkin_result,  # 用本地持久化记录补齐 user_info 收益字段
        )
        from runtime.model.sign_tools import (
            format_sign_duration,  # 导入耗时格式化函数:把耗时秒数格式化为可读文案
        )
        from runtime.model.sign_tools import (
            is_already_checked_in,  # 导入已签到判断函数:按按钮禁用状态判断今日是否已签到
        )
        from runtime.model.sign_tools import (
            refresh_user_info_after_checkin,  # 签到/跳过重拉用户中心(保护收益字段)
        )
        from runtime.model.sign_tools import (
            try_cookie_login,  # 导入 Cookie 免登函数:尝试用本地 Cookie 免登并解析用户信息
        )
        from runtime.model.sign_tools import (
            user_info_to_account_detail,  # 从签到工具模块导入 HTML 清洗函数; 从签到工具模块导入签到结果回填函数; 从签到工具模块导入持久化结果补齐函数; 从签到工具模块导入签到后重拉函数; 从签到工具模块导入详情映射函数; 把 user_info 映射为持久化 detail 契约
        )
        from service.run_ba import (
            ServiceApiService,  # 导入网站 API 业务封装类:提供会话/登录/用户中心/签到等接口
        )

        # ------------------------------------------------------------------
        # 初始化与日志桥接
        # ------------------------------------------------------------------
        total = len(self._account_list)  # 计算账号总数,用于进度百分比与序号计算
        # 挂到 ROOT: 业务 logger 全部 propagate 到 root,界面即可看到每一步;任务结束即摘除
        bridge = _QtLogBridge(self.signal_log.emit)  # 创建把业务日志桥接到 UI 信号的自定义 Handler
        root_logger = logging.getLogger()  # 获取 root logger(业务 logger 默认向上传播到此)
        root_logger.addHandler(bridge)  # 临时挂载桥接 Handler 到 root logger

        try:  # 主流程 try 块,保证 finally 中摘除桥接 Handler
            # --------------------------------------------------------------
            # 空账号快速返回
            # --------------------------------------------------------------
            if total == 0:  # 判断是否没有账号
                self._log("⚠ 没有可用于批量签到的账号(需先添加账号并保存密码)")  # 双写日志提示无账号
                self.signal_progress.emit(100)  # 进度直接置满
                self.signal_result.emit([])  # 回传空结果列表
                return  # 结束线程

            # --------------------------------------------------------------
            # 读取配置
            # --------------------------------------------------------------
            proxy_cfg = self.config["proxy"]  # 从全局配置中读取代理配置节
            smtp_cfg = self.config["smtp"]  # 从全局配置中读取 SMTP 邮件配置节
            # 邮件开关与单账号同源(user_info.json 的 enable_mail,默认开启)
            enable_mail = bool(  # 从配置中取邮件总开关并转换为布尔值
                self.config.get("user_info", {}).get("enable_mail", True)  # 缺省为开启状态
            )
            proxies, proxy_source = resolve_effective_proxy(proxy_cfg)  # 调用解析函数,得到实际代理映射与来源说明

            # --------------------------------------------------------------
            # 记录任务开始
            # --------------------------------------------------------------
            self._log(  # 双写批量任务启动横幅信息
                f"🚀 批量签到任务开始:共{total}个账号 | "  # 账号总数
                f"🌐代理:{proxy_source}" + (f" -> {proxies}" if proxies else "")  # 代理来源,有映射时附带详情
                + f" | 📧邮件通知:{'开启' if enable_mail else '关闭'}"  # 邮件开关状态
            )
            self.signal_progress.emit(0)  # 进度归零,开始任务

            # --------------------------------------------------------------
            # 逐账号处理循环
            # --------------------------------------------------------------
            task_results: List[Dict[str, Any]] = []  # 初始化已处理账号的结果收集列表
            for idx, acc in enumerate(self._account_list, 1):  # 从 1 开始编号顺序处理每个账号
                # 本账号签到耗时计时起点(清旧/登录/防风控延时/签到/重拉计入;
                # 账号之间的防风控间隔发生在本账号处理完之后,不计入)
                t_acc = perf_counter()  # 记录本账号处理的起始时刻
                email = str(acc.get("email", "")).strip()  # 从账号字典中取邮箱并去除空白
                password = str(acc.get("password", ""))  # 从账号字典中取登录密码(不落日志)
                self._log(f"🚀 [{idx}/{total}] 开始处理账号:{email}(密码不落日志)")  # 双写日志提示当前账号序号

                # ----------------------------------------------------------
                # 单账号流水线第一步[清旧]
                # ----------------------------------------------------------
                # 每个账号签到前先删其上一次的详情快照/收益记录,Cookie 与登录密码保留;
                # 失败账号也以干净状态进入下一轮
                self._log(f"🧹 {email} 清除本地历史签到信息(旧详情快照/收益记录)")  # 双写日志提示清旧操作
                reset_account_sign_persistence(email)  # 调用清旧函数,删除该账号旧详情快照与收益记录

                # 初始化本账号结果记录字典
                res_item: Dict[str, Any] = {  # 初始化本账号的结果记录
                    "email": email,  # 账号邮箱(后续可能被页面解析邮箱覆盖)
                    "login_ok": False,  # 登录是否成功标志,默认 False
                    "checkin_ok": False,  # 是否本次新签成功标志,默认 False
                    "already_signed": False,  # 是否今日已签而跳过标志,默认 False
                    "user_info": None,  # 用户中心解析结果快照,初始 None
                    "detail": None,  # 持久化详情契约,异常时为 None
                    "cost_sec": None,  # 本账号签到耗时(秒),初始 None
                    "error": None,  # 失败/异常原因,初始 None
                }
                service: Optional[ServiceApiService] = None  # 本账号独立会话句柄,先置空以便 finally 安全关闭

                try:  # 单账号流水线 try 块,异常被捕获并记录到 res_item
                    # ------------------------------------------------------
                    # 创建本账号独立会话
                    # ------------------------------------------------------
                    service = ServiceApiService(proxy_mapping=proxies)  # 构造该账号专用的 API 服务实例
                    service.session_open()  # 打开会话并加载本地 Cookie 文件

                    # ------------------------------------------------------
                    # 单账号流水线第二步[登录]: 优先 Cookie 免登,失败回退密码登录
                    # ------------------------------------------------------
                    cookie_ok, user_info, err = try_cookie_login(service, email)  # 调用 Cookie 免登函数
                    if cookie_ok and isinstance(user_info, dict):  # 判断 Cookie 是否有效且解析出字典,是则免登成功
                        res_item["login_ok"] = True  # 标记登录成功
                        res_item["user_info"] = user_info  # 暂存用户信息快照到结果项
                    else:  # Cookie 失效/解析失败:回退密码登录
                        self._log(f"⚠ {email} Cookie失效,回退密码登录:{err}")  # 双写日志提示回退原因
                        # 失效 Cookie 残留在会话中会污染登录请求,先关闭再重建干净会话
                        self._log(f"🧹 {email} 关闭残留Cookie会话并重建...")  # 双写日志提示重建会话
                        service.session_close()  # 关闭携带脏 Cookie 的旧会话
                        service.session_open()  # 重建不带脏 Cookie 的干净会话
                        login_resp = service.login(email, password)  # 调用登录方法,提交账号密码
                        if "error" in login_resp:  # 检查返回是否含 error 字段,含则表示网络层异常
                            raise RuntimeError(f"登录网络异常:{login_resp['error']}")  # 抛错进入账号级 except
                        if login_resp.get("ret") != 1:  # 检查 ret 是否为 1,非 1 表示业务层登录失败
                            raise RuntimeError(f"登录业务失败:{login_resp.get('msg')}")  # 抛出业务失败原因
                        res_item["login_ok"] = True  # 标记登录成功
                        self._log(f"✅ {email} 登录成功,正在拉取用户中心...")  # 双写日志提示拉取用户中心
                        resp_html = service.get_user_info()  # 请求用户中心页面 HTML
                        if isinstance(resp_html, dict) and "error" in resp_html:  # 检查返回是否为 dict 且含 error
                            raise RuntimeError(f"获取用户信息失败:{resp_html['error']}")  # 抛出获取用户信息异常
                        user_info = cleaning_html(getattr(resp_html, "text", ""))  # 清洗 HTML 并解析用户信息
                        self._log(  # 双写密码登录路径的解析调试信息
                            f"👤 {email} 密码登录拉取解析:last_use={user_info.get('last_use_text')!r} "  # 最近使用时间
                            f"warns={user_info.get('parse_warns', [])}"  # 解析告警列表
                        )
                        if not isinstance(user_info, dict):  # 检查解析结果是否异常(非字典)
                            user_info = {}  # 置为空字典,防止后续 .get 调用崩溃
                        # 页面没解析出邮箱时用任务账号兜底:保证签到收益记录能按账号落盘/回填
                        if not user_info.get("email"):  # 检查页面是否未解析出邮箱
                            user_info["email"] = email  # 用任务账号兜底邮箱字段
                            fill_persisted_checkin_result(user_info)  # 用本地持久化收益记录补齐
                        res_item["user_info"] = user_info  # 回存用户信息快照到结果项

                    # ------------------------------------------------------
                    # 单账号流水线第三步[签到]: 先判重,未签则防风控延时后调接口
                    # ------------------------------------------------------
                    # 以用户中心按钮状态为准:今日已签到(按钮禁用)直接跳过,
                    # 绝不调用 /user/checkin;仅未签到/状态不明才发起签到
                    user_info = res_item["user_info"]  # 取出用户信息(免登/密码登录两路已统一)
                    if is_already_checked_in(user_info):  # 调用判重函数,判断今日是否已签到
                        res_item["already_signed"] = True  # 标记为已签跳过
                        btn_text = (  # 取按钮文案用于日志显示
                            user_info.get("dom_checkin_text") if isinstance(user_info, dict)  # 字典时取按钮文本
                            else ""  # 非字典时给空串
                        ) or "今日已签到"  # 缺省兜底文案
                        self._log(f"ℹ️ {email} 今日已签到(按钮禁用:{btn_text}),跳过签到接口")  # 双写日志提示跳过原因
                        # 重复签到同样重拉用户中心:获取最新页面状态,
                        # 传入旧快照保护 checkin_gain_flow 不被拉取结果覆盖为空
                        self._log(f"👤 {email} 重新拉取用户中心,确认已签到状态...")  # 双写日志提示重拉
                        refreshed = refresh_user_info_after_checkin(service, email, user_info)  # 调用重拉函数
                        if refreshed is not None:  # 检查重拉是否成功,成功才覆盖
                            res_item["user_info"] = refreshed  # 更新结果项中的用户信息快照
                    else:  # 今日未签到/状态不明:防风控延时后发起签到
                        # 防风控:登录(含 Cookie 免登)与签到之间随机等待 5-30 秒,
                        # 模拟人工浏览用户中心后再点签到
                        wait_sec = round(random.uniform(5, 40), 1)  # 生成 5~40 秒的一位小数随机等待时长
                        self._log(f"⏳ {email} 防风控延时:登录后等待{wait_sec}秒再签到...")  # 双写日志提示等待
                        if not interruptible_sleep(self, wait_sec):  # 调用可中断分段睡眠;False 表示被取消
                            self._log(f"🛑 {email} 延时等待期间任务被取消,终止批量签到")  # 双写日志提示取消
                            break  # 跳出账号循环,进入收尾(不发汇总/不发邮件)
                        check_resp = service.checkin()  # 调用签到接口
                        if "error" in check_resp:  # 检查返回是否含 error 字段,含则表示网络层异常
                            raise RuntimeError(f"签到网络异常:{check_resp['error']}")  # 抛错进入账号级 except
                        if check_resp.get("ret") == 1:  # 检查 ret 是否为 1,为 1 表示业务签到成功
                            fill_checkin_result(res_item["user_info"], check_resp)  # 调用回填函数,回填收益并落盘签到记录
                            res_item["checkin_ok"] = True  # 标记新签成功
                            self._log(f"✅ {email} 签到成功:{check_resp.get('msg')}")  # 双写日志提示成功收益
                            # 持久化前重拉用户中心:拿签到后禁用态/今日已签/
                            # 最新签到时间,收益由记录文件回填进新快照
                            self._log(f"👤 {email} 重新拉取用户中心确认签到状态...")  # 双写日志提示重拉确认
                            refreshed = refresh_user_info_after_checkin(service, email, res_item["user_info"])  # 重拉
                            if refreshed is not None:  # 检查重拉是否成功,成功才覆盖
                                res_item["user_info"] = refreshed  # 更新结果项快照
                        else:  # 接口返回非成功
                            # 已签到后签到接口为禁用态,正常不会走到此分支;
                            # 走到这里说明接口返回非成功(如网络抖动/业务异常)
                            res_item["error"] = f"签到业务返回:{check_resp.get('msg')}"  # 记录业务返回消息
                            self._log(f"ℹ️ {email} 签到业务返回:{check_resp.get('msg')}")  # 双写日志提示业务消息

                    # ------------------------------------------------------
                    # 单账号流水线第四步[存新]: 映射 detail 落盘
                    # ------------------------------------------------------
                    # 走到这里说明流程未抛异常(新签成功/已签跳过/业务返回未成功),
                    # 统一映射落盘,持久化 key 以页面解析出的邮箱为准;异常分支不写.
                    # 签到耗时到此结算(下方账号防风控间隔不计入),随 detail 落盘,
                    # 与单账号/界面面板共用同一 detail 契约渲染
                    cost_sec = round(perf_counter() - t_acc, 2)  # 结算本账号签到耗时(秒,两位小数)
                    detail = user_info_to_account_detail(  # 调用映射函数,映射为持久化 detail 契约
                        res_item["user_info"], bool(res_item["checkin_ok"]), email  # 快照、是否新签、兜底账号
                    )
                    email_key = str(detail.pop("email", "") or email)  # 从 detail 中弹出邮箱作为持久化 key,缺省回退任务账号
                    detail["checkin_cost_sec"] = cost_sec  # 把耗时写入详情字典
                    res_item["email"] = email_key  # 回写实际持久化 key 到结果项
                    res_item["detail"] = detail  # 保存详情契约副本到结果项
                    res_item["cost_sec"] = cost_sec  # 保存耗时供汇总/失败块使用
                    save_account_detail(email_key, detail)  # 调用保存函数,按邮箱 key 落盘账号详情
                    self._log(f"💾 {email_key} 本次签到结果已本地持久化")  # 双写日志提示持久化完成
                    self._log(f"⏱️ {email_key} 签到耗时:{format_sign_duration(cost_sec)}")  # 双写日志提示耗时文案

                except Exception as e:  # 捕获单账号登录/签到流程异常:记录失败但不中断批量
                    res_item["error"] = f"任务异常:{e}"  # 记录异常原因到结果项
                    # 失败账号同样结算耗时(纯处理开销,无防风控延时),供失败占位块使用
                    res_item["cost_sec"] = round(perf_counter() - t_acc, 2)  # 结算失败账号已耗时长
                    self._log(f"❌ {email} 批量任务异常:{e}", logging.ERROR)  # 以错误级别双写异常信息
                    self._log(  # 双写失败账号的耗时信息
                        f"⏱️ {email} 签到耗时:"  # 耗时文案前缀
                        f"{format_sign_duration(res_item['cost_sec'])}"  # 格式化后的耗时
                    )
                    self._logger.error(f"❌ {email} 批量任务异常", exc_info=True)  # 落盘完整错误堆栈
                finally:  # 无论成功失败都关闭本账号会话
                    # 每账号独立会话,异常/continue 路径都保证关闭
                    if service is not None:  # 判断会话是否确实创建过,创建过才关闭
                        try:  # 关闭操作本身也可能抛异常,单独吞掉
                            service.session_close()  # 关闭底层 requests 会话,释放连接
                        except Exception as close_err:  # 捕获关闭失败异常
                            self._logger.warning(f"⚠ {email} 会话关闭异常:{close_err}")  # 仅落盘告警

                # ----------------------------------------------------------
                # 收集结果、上报进度与单项完成
                # ----------------------------------------------------------
                task_results.append(res_item)  # 把本账号(成功或失败)结果追加到收集列表
                pct = int(len(task_results) * 100 / total)  # 按已完成账号数换算进度百分比
                self.signal_progress.emit(pct)  # 发射进度信号
                # 单账号处理完成即通知 UI 刷新"今日已签到"统计(实时更新)
                self.signal_item_done.emit(res_item)  # 发射单项完成信号

                # ----------------------------------------------------------
                # 账号之间防风控间隔(最后一个账号不再等待)
                # ----------------------------------------------------------
                # 防风控:账号与账号之间随机间隔 10-50 秒,最后一个账号不再等待;
                # 分段睡眠可被关窗/取消立即中断,不会死等
                if idx < total:  # 判断是否为非最后一个账号,是才需要等待
                    gap_sec = round(random.uniform(10, 50), 1)  # 生成 10~50 秒的一位小数随机间隔
                    self._log(f"⏳ 账号防风控间隔:等待{gap_sec}秒后继续下一账号...")  # 双写日志提示间隔
                    if not interruptible_sleep(self, gap_sec):  # 调用可中断分段睡眠;False 表示被取消
                        self._log("🛑 账号间隔等待期间任务被取消,终止批量签到")  # 双写日志提示取消
                        break  # 跳出账号循环进入收尾

            # --------------------------------------------------------------
            # 统计汇总
            # --------------------------------------------------------------
            ok_cnt = sum(1 for r in task_results if r.get("checkin_ok"))  # 统计新签成功的账号数量
            skip_cnt = sum(1 for r in task_results if r.get("already_signed"))  # 统计今日已签跳过的账号数量
            # 统计只数[已处理完]的账号:取消时未轮到的账号不计入失败
            done_total = len(task_results)  # 计算已实际处理完的账号数
            fail_cnt = done_total - ok_cnt - skip_cnt  # 其余计为失败(含登录异常/接口未成功)

            # --------------------------------------------------------------
            # 组装汇总文本并写入结果文件
            # --------------------------------------------------------------
            # 组装"统计+逐账号明细"汇总文本,覆盖写入邮件目录/mail_body.txt
            lines = [  # 初始化汇总文本行列表
                "Service批量签到结果",  # 汇总标题行
                f"任务时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",  # 任务时间戳行
                f"账号总数:{total} | 已处理:{done_total} | "  # 统计行第一段
                f"新签成功:{ok_cnt} | 今日已签跳过:{skip_cnt} | 失败:{fail_cnt}",  # 统计行第二段
            ]
            cancelled = self.isInterruptionRequested()  # 判断任务是否被取消(关窗/取消按钮)
            if cancelled:  # 如果被取消,只做留痕,不发汇总信号、不发邮件
                # 被取消(关窗)时界面已销毁:已完成账号的落盘保留,结果文件写入
                # 已完成部分留痕,但不发射汇总、不发邮件
                lines.append(  # 追加取消提示行
                    f"⚠ 任务被取消,仅含已完成的 {len(task_results)}/{total} 个账号"  # 已完成数量说明
                )
            lines.append("")  # 追加空行,分隔统计与明细
            lines.append("账号明细:")  # 追加明细小节标题
            # 每个账号一个完整详情块(含签到耗时),块间空行分隔
            for r in task_results:  # 遍历已处理的结果列表
                lines.append("")  # 追加块间空行
                lines.append(_account_result_block(r))  # 追加该账号的渲染区块
            summary_text = "\n".join(lines) + "\n"  # 把行列表拼接为完整汇总文本,并以换行结尾
            try:  # 结果文件写入单独保护,失败不影响结果回填
                write_checkin_result_text(summary_text)  # 调用写入函数,覆盖写汇总结果文件
                self._log("📝 批量签到汇总已写入签到结果文件")  # 双写日志提示写入成功
            except Exception as write_err:  # 捕获写入失败异常
                self._log(f"⚠ 汇总结果写入结果文件异常:{write_err}", logging.WARNING)  # 以警告级别双写

            # --------------------------------------------------------------
            # 取消路径收尾:静默退出
            # --------------------------------------------------------------
            if cancelled:  # 判断是否为取消路径
                # 取消路径:界面已销毁,到此静默结束
                self._log("🛑 批量签到已取消,不发送汇总邮件")  # 双写日志提示取消收尾
                return  # 结束线程,不发汇总信号/不发邮件

            # --------------------------------------------------------------
            # 正常完成:记录统计、发汇总邮件、发结果信号
            # --------------------------------------------------------------
            self._log(  # 双写批量任务结束统计
                f"🎉 批量签到任务结束:新签成功{ok_cnt}/{total},"  # 成功数/总数
                f"今日已签跳过{skip_cnt}个,失败{fail_cnt}个"  # 跳过数与失败数
            )
            # 全部账号签完后[统一只发一封]汇总邮件(按全局开关);
            # 邮件正文与结果文件内容一致,发送失败不影响结果回填
            if enable_mail:  # 判断邮件开关是否开启,开启才发汇总邮件
                subject = (  # 组装邮件主题
                    f"Service批量签到汇总(成功{ok_cnt}/跳过{skip_cnt}/失败{fail_cnt})"  # 主题含三类计数
                )
                self._send_summary_mail(smtp_cfg, subject, summary_text, self._log)  # 复用发送器发唯一一封汇总邮件
            else:  # 开关关闭
                self._logger.info("📧 邮件通知未开启,跳过批量汇总邮件")  # 仅落盘记录跳过发送

            # --------------------------------------------------------------
            # 企业微信推送:推送内容与汇总邮件正文完全一致(summary_text)
            # --------------------------------------------------------------
            self._push_wecom(summary_text)  # 按企业微信开关推送完整汇总(未启用则静默跳过)

            self.signal_progress.emit(100)  # 发射进度信号,进度置满
            self.signal_result.emit(task_results)  # 发射全部账号结果列表信号

        # ------------------------------------------------------------------
        # 任务级异常兜底
        # ------------------------------------------------------------------
        except Exception as e:  # 捕获任务级异常(账号循环外的异常)
            self._logger.error(f"❌ 批量签到任务异常:{e}", exc_info=True)  # 落盘完整错误堆栈
            self._log(f"❌ 批量签到任务异常:{e}", logging.ERROR)  # 以错误级别双写异常信息
            # 任务级异常同样覆盖写入结果文件,保证其内容反映最近一次任务
            try:  # 失败汇总文件写入单独保护
                write_checkin_result_text(  # 调用写入函数,覆盖写异常终止提示
                    "Service批量签到结果\n"  # 标题行
                    f"任务时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"  # 时间戳行
                    f"❌ 批量任务异常终止:{e}\n"  # 异常原因行
                )
            except Exception:  # 捕获写入失败异常,不再处理
                pass  # 静默忽略
            try:  # 任务异常也推送企业微信(推送失败不影响结果信号)
                self._push_wecom(f"❌ 批量签到任务异常\n原因:{e}")  # 按开关推送异常原因
            except Exception as wecom_err:  # 推送异常单独吞掉
                self._logger.warning(f"⚠ 批量异常推送企业微信失败:{wecom_err}")  # 仅落盘告警
            self.signal_progress.emit(100)  # 进度置满,避免界面进度条停滞
            self.signal_result.emit([])  # 回传空结果列表

        # ------------------------------------------------------------------
        # finally 收尾: 摘除桥接 Handler
        # ------------------------------------------------------------------
        finally:  # 无论何种退出路径都摘除桥接 Handler,防止内存泄漏和日志残留
            root_logger.removeHandler(bridge)  # 从 root logger 移除临时挂载的 Qt 日志桥接器

    def _push_wecom(self, text: str) -> None:
        """按企业微信推送开关发送批量汇总消息(未启用/未配置则静默跳过).

        延迟导入配置与业务封装,避免启动阶段加载网络库;推送失败只记日志,
        绝不抛出异常影响签到主流程.消息类型按配置 text/markdown 分发.

        :param text: 推送内容(纯文本或 Markdown)
        :type text: str
        :return: 无返回值
        :rtype: None
        """
        try:  # 推送整体保护,任何异常不向上抛
            from runtime.model.app_config import load_wecom_config  # 延迟导入配置读取函数
            wecom_cfg = load_wecom_config()  # 读取企业微信推送配置(实时读文件)
            if not bool(wecom_cfg.get("WECOM_ENABLE", False)):  # 总开关未开启
                return  # 静默跳过,不发推送
            webhook_url = str(wecom_cfg.get("WECOM_WEBHOOK_URL", "")).strip()  # Webhook 地址
            if not webhook_url:  # 未配置 Webhook 地址
                self._logger.warning("⚠ 企业微信推送未配置 Webhook 地址,跳过推送")  # 仅落盘告警
                return  # 静默跳过
            from service.notify_wecom import ServiceWeComPusher  # 延迟导入业务封装
            msg_type = str(wecom_cfg.get("WECOM_MSG_TYPE", "text"))  # 消息类型
            pusher = ServiceWeComPusher(webhook_url, msg_type)  # 构造业务推送器
            self._log("📨 企业微信推送开始...")  # 双写日志提示开始推送
            ok, msg = pusher.send(text)  # 按配置类型发送推送
            if ok:  # 推送成功
                self._log(f"✅ 企业微信推送成功({msg})")  # 双写日志提示成功
            else:  # 推送失败
                self._log(f"❌ 企业微信推送失败:{msg}", logging.WARNING)  # 以警告级别记录失败
        except Exception as e:  # 推送链路任何异常
            self._logger.warning(f"⚠ 企业微信推送异常:{e}")  # 仅落盘告警,不影响签到流程

    def _send_summary_mail(
        self, smtp_cfg: dict, subject: str, body: str, log_fn
    ) -> None:
        """全部账号签完后,把汇总文本作为正文[只发一封]通知邮件.

        整个批量任务复用同一个 ServiceEmailSender(懒加载);任何异常只记日志,
        不向上抛——邮件失败不能影响结果回填与界面刷新.

        :param smtp_cfg: SMTP 配置节字典,含主机/端口/发件邮箱/授权码/收件人列表等
        :type smtp_cfg: dict
        :param subject: 邮件主题字符串
        :type subject: str
        :param body: 邮件正文字符串(与汇总结果文件内容一致)
        :type body: str
        :param log_fn: 日志回调函数(传入 self._log),保证日志走同一双写通道
        :type log_fn: Callable[..., None]
        :return: 无返回值
        :rtype: None
        """
        to_list = smtp_cfg.get("SMTP_DEFAULT_TO_LIST", [])  # 从 SMTP 配置中取默认收件人列表,缺省空列表
        if not to_list:  # 判断收件人是否为空,为空则跳过发送
            log_fn("⚠ 已开启邮件通知,但收件人为空,跳过发送", logging.WARNING)  # 以警告级别记录
            return  # 直接返回,不发送
        try:  # 发送过程整体保护,失败不影响结果回填
            if self._mail_sender is None:  # 判断发送器是否尚未创建(懒加载且全程只建一次)
                from service.send_email import ServiceEmailSender  # 延迟导入 SMTP 发送器类
                self._mail_sender = ServiceEmailSender(  # 构造并缓存邮件发送器实例
                    smtp_host=smtp_cfg["SMTP_DEFAULT_HOST"],  # SMTP 服务器主机地址
                    smtp_port=smtp_cfg["SMTP_DEFAULT_PORT"],  # SMTP 服务器端口
                    smtp_user=smtp_cfg["SMTP_SENDER_EMAIL"],  # 发件邮箱账号
                    smtp_password=smtp_cfg["SMTP_AUTH_CODE"],  # 邮箱授权码
                )
            log_fn(f"📧 批量签到完成,发送1封汇总邮件,收件人 {len(to_list)} 个")  # 记录收件人数量
            for to_mail in to_list:  # 遍历收件人列表,逐个发送同一封汇总邮件
                log_fn(f"📤 正在发送汇总邮件至 {to_mail} ...")  # 记录当前发送目标
                ok, msg = self._mail_sender.send_text(to_mail, subject, body)  # 调用发送方法,发送纯文本邮件
                if ok:  # 判断是否发送成功
                    log_fn(f"✅ 汇总邮件发送成功:{to_mail}({msg})")  # 记录成功信息
                else:  # 发送失败
                    log_fn(f"❌ 汇总邮件发送失败:{to_mail}({msg})", logging.WARNING)  # 以警告级别记录失败
        except Exception as mail_err:  # 捕获构造/发送阶段的任何异常
            log_fn(f"❌ 批量汇总邮件发送异常:{mail_err}", logging.ERROR)  # 以错误级别记录异常
            self._logger.error("❌ 批量汇总邮件异常", exc_info=True)  # 落盘完整错误堆栈
