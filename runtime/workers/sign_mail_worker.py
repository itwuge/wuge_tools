# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: sign_mail_worker.py
# 归属: runtime/workers 后台任务线程层 —— 单账号签到+邮件通知 Worker
# ------------------------------------------------------------------------------
# 文件用途:
#   单账号"签到 + 邮件通知"后台线程实现模块.严格按流水线执行:
#   清旧(删除历史详情/收益记录)→ 登录签到 → 存新(本次结果落盘)→ 发通知邮件.
#   本模块只负责线程胶水层逻辑,不包含具体业务算法实现.
# ------------------------------------------------------------------------------
# 架构定位:
#   本模块是 QThread 工作线程,通过 Signal 向 controller/view 发送事件,
#   不直接操作 UI 控件.只调用 service 业务层与 runtime.model 的工具/持久化 API,
#   不写业务算法,不 import 任何 View/Model 界面类,严格遵循分层架构.
#
#   关联组件:
#     - 上游: Controller(单账号签到控制)
#     - 下游: runtime.model.account_store、runtime.model.sign_tools、
#             runtime.model.app_config、service.run_ba.ServiceApiService、
#             service.send_email.ServiceEmailSender
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 删除账号上一次签到的本地持久化产物(详情快照 / 按账号收益记录)
#   2. Cookie 免登优先,失败后关闭脏会话并回退账号密码登录
#   3. 以用户中心按钮状态判定今日是否已签到,仅未签到时防风控延时后调签到接口
#   4. 把本次结果映射落盘、覆盖写签到结果文件,并按开关逐收件人发通知邮件
#   5. 异常路径兜底写结果文件,finally 中保证关闭网络会话
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只调用 service 业务层与 runtime.model 的工具/持久化 API
#   - 不写业务算法,不 import 任何 View/Model 界面类
#   - 登录/签到网络异常或业务失败 → 抛 RuntimeError 进入 except 兜底
# ------------------------------------------------------------------------------
# 线程模型:
#   - signal_log(str): 日志文本信号,在工作线程 emit
#   - signal_progress(int): 进度百分比信号(10/40/70/100 等节点),在工作线程 emit
#   - signal_result(dict): 最终结果信号,在工作线程 emit
#   以上信号均由 Qt 自动以队列连接(QueuedConnection)跨线程投递给 UI 线程.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、random、datetime.datetime、time.perf_counter
#   - 第三方: PySide6.QtCore.Signal
#   - 项目内:
#       runtime.workers.interruptible_sleep
#       runtime.workers.base_worker.BaseWorker
#       run() 内延迟导入:
#           runtime.model.account_store
#           runtime.model.sign_tools
#           runtime.model.app_config
#           service.run_ba.ServiceApiService
#           service.send_email.ServiceEmailSender
# ==============================================================================

# ==============================================================================
# [签到&邮件 Worker - QThread 胶水层]
# 职责: 后台线程执行单账号签到,严格按流水线执行:
#       清旧(删历史详情/收益记录)→ 登录签到 → 存新(本次结果落盘)→ 发通知邮件
# 边界: 只调用 service 业务层与 comm_tools 工具/持久化 API,
#       不写业务算法,不 import 任何 View/Model
# 信号: signal_log(str) / signal_progress(int) / signal_result(dict)
# 日志: 每一步同时写入 app.log(带 emoji 图标)并推送到界面日志区
# ==============================================================================
import logging  # 导入标准库日志模块:用于异常堆栈与分级日志落盘到 app.log 文件
import random  # 导入随机数模块:用于生成防风控随机等待秒数,模拟人工操作间隔
from datetime import datetime  # 从 datetime 模块导入 datetime 类:用于生成结果文件中的任务时间戳
from time import perf_counter  # 从 time 模块导入 perf_counter 函数:单调高精度计时器,用于统计账号签到耗时

from PySide6.QtCore import Signal  # 从 PySide6.QtCore 导入 Signal:Qt 信号定义工具,用于向 UI 线程回传事件

from runtime.workers import interruptible_sleep  # 从本包导入可中断分段睡眠函数:防风控等待期间可响应取消操作
from runtime.workers.base_worker import BaseWorker  # 导入 Worker 基类:提供 QThread 基础能力与日志双写功能


# =========** [SignMail]Worker - 单账号签到+邮件通知 **=========
class SignMailWorker(BaseWorker):
    """单账号签到并按开关发送通知邮件的后台 Worker(QThread).

    在线程内按"清旧 → 登录 → 签到 → 存新 → 发邮件"流水线调用 service/model 层,
    自身不持有任何 UI 引用,进度、日志、结果一律以信号上报给 Controller.
    所有网络请求和耗时操作都在子线程执行,避免阻塞 UI 主线程.
    """

    # ------------------------------------------------------------------
    # 类属性(Qt 信号定义)
    # ------------------------------------------------------------------
    # 进度信号:载荷为 0~100 的进度百分比整数(int),UI 线程据此刷新进度条
    signal_progress = Signal(int)  # 定义进度信号,参数类型为整数,表示当前完成百分比
    # 结果信号:载荷为单账号最终结果 dict,含 ok/signed/already_signed/msg/email/detail 等字段
    signal_result = Signal(dict)  # 定义结果信号,参数类型为字典,携带签到任务的最终结果

    # ------------------------------------------------------------------
    # 实例属性(在 __init__ 中初始化)
    # ------------------------------------------------------------------
    # self.config: dict 全局配置字典,包含 smtp、proxy 等配置节
    # self.sign_param: dict 签到参数容器,由 set_sign_param 在启动前注入,
    #                  含 account(账号)、password(密码)、enable_mail(邮件开关)

    def __init__(self, config: dict, parent=None):
        """初始化单账号签到 Worker.

        :param config: 全局配置字典(含 smtp、proxy 等配置节),
                       供 run() 方法读取使用
        :type config: dict
        :param parent: Qt 父对象,用于线程生命周期管理,
                       通常为控制器或窗口对象
        :type parent: QObject | None
        :return: 无返回值
        :rtype: None
        """
        super().__init__(parent)  # 调用 BaseWorker 父类构造函数,初始化 QThread 并创建按类名命名的 logger
        self.config = config  # 持有全局配置字典(SMTP/代理等配置),供 run() 方法读取使用
        self.sign_param = {}  # 初始化签到参数容器为空字典,由 set_sign_param 方法在启动前注入具体参数

    def set_sign_param(self, sign_param: dict):
        """由 Controller 设置本次签到参数.

        设置签到所需的账号、密码、邮件开关等参数,在线程启动前调用.

        :param sign_param: 签到参数字典,键包括:
                           - account: 签到账号(邮箱)
                           - password: 登录密码
                           - enable_mail: 是否发送通知邮件的布尔开关
        :type sign_param: dict
        :return: 无返回值
        :rtype: None
        """
        self.sign_param = sign_param  # 保存 Controller 下发的本次签到参数,供 run() 方法使用

    def run(self):
        """QThread 线程入口:执行单账号签到完整流水线.

        按"清旧 → 登录 → 签到 → 存新 → 发邮件"五步流水线执行,
        每一步都有进度信号上报,异常统一捕获并转为失败结果信号.
        被取消时直接 return,不发射结果信号(关窗流程).

        :return: 无返回值;成功/失败结果均通过 signal_result(dict) 异步回传,
                 被取消时直接 return 不发结果
        :rtype: None
        :raises Exception: 流水线内全部异常在方法内部捕获并转成失败结果信号,不再向外抛出
        """
        # ------------------------------------------------------------------
        # 延迟导入区: 避免启动阶段加载网络库,加快应用启动速度
        # ------------------------------------------------------------------
        from runtime.model.account_store import (  # 从账号存储模块导入清旧函数
            reset_account_sign_persistence,  # 清除账号历史签到持久化产物(详情快照/收益记录)
        )
        from runtime.model.account_store import (  # 从账号存储模块导入详情保存函数
            save_account_detail,  # 保存本次账号详情快照(以邮箱为 key)
        )
        from runtime.model.account_store import (  # 从账号存储模块导入结果文件写入函数
            write_checkin_result_text,  # 覆盖写签到结果文本文件 mail_body.txt
        )
        from runtime.model.sign_tools import (  # 从签到工具模块导入 HTML 清洗函数
            cleaning_html,  # 清洗用户中心 HTML 并解析为 user_info 字典
        )
        from runtime.model.sign_tools import (  # 从签到工具模块导入签到结果回填函数
            fill_checkin_result,  # 把签到接口返回的收益回填进 user_info 并落盘
        )
        from runtime.model.sign_tools import (  # 从签到工具模块导入持久化结果补齐函数
            fill_persisted_checkin_result,  # 用本地持久化记录补齐 user_info 收益字段
        )
        from runtime.model.sign_tools import format_sign_duration  # 从签到工具模块导入耗时格式化函数:把耗时秒数格式化为可读文案
        from runtime.model.sign_tools import (  # 从签到工具模块导入详情文本渲染函数
            format_user_info_text,  # 把 detail 渲染为面板/邮件共用的详情文本块
        )
        from runtime.model.sign_tools import is_already_checked_in  # 从签到工具模块导入已签到判断函数:按按钮禁用状态判断今日是否已签到
        from runtime.model.sign_tools import (  # 从签到工具模块导入签到后重拉函数
            refresh_user_info_after_checkin,  # 签到/跳过重拉用户中心最新状态(保护收益字段)
        )
        from runtime.model.sign_tools import try_cookie_login  # 从签到工具模块导入 Cookie 免登函数:尝试用本地 Cookie 免登并解析用户信息
        from runtime.model.sign_tools import (  # 从签到工具模块导入详情映射函数
            user_info_to_account_detail,  # 把 user_info 映射为持久化 detail 契约
        )
        from service.run_ba import ServiceApiService  # 导入网站 API 业务封装类:提供会话/登录/用户中心/签到等接口
        from service.send_email import ServiceEmailSender  # 导入 SMTP 纯文本邮件发送器类

        # ------------------------------------------------------------------
        # 初始化变量
        # ------------------------------------------------------------------
        service = None  # 网络会话服务句柄,先置空以便 finally 块中安全判断与关闭
        account = ""  # 账号备份变量,保证异常分支的日志/结果中也能带出账号信息
        # 账号处理总耗时计时起点(清旧/登录/防风控等待/签到/重拉都计入),
        # 用 perf_counter 保证单调高精度;发邮件耗时不算在签到耗时内
        t_start = perf_counter()  # 记录账号流水线起始时刻,用于后续计算签到总耗时

        try:  # 包裹整个签到主流程,任何异常统一进入失败兜底分支
            # --------------------------------------------------------------
            # 读取配置
            # --------------------------------------------------------------
            smtp_cfg = self.config["smtp"]  # 从全局配置中读取 SMTP 邮件配置节
            proxy_cfg = self.config["proxy"]  # 从全局配置中读取代理配置节

            # --------------------------------------------------------------
            # 提取签到参数
            # --------------------------------------------------------------
            account = self.sign_param.get("account", "")  # 从签到参数中取本次任务账号,缺省为空字符串
            password = self.sign_param.get("password", "")  # 从签到参数中取登录密码(不写入日志)
            enable_mail = bool(self.sign_param.get("enable_mail", False))  # 从签到参数中取邮件开关并转换为布尔值

            # --------------------------------------------------------------
            # 流水线第一步[清旧]
            # --------------------------------------------------------------
            # 先删除该账号上一次的签到持久化产物(账号详情快照+按账号收益记录),
            # 本次从干净状态开始;Cookie/登录密码不属于签到结果,不受影响
            self._log(f"🧹 清除本地历史签到信息:{account}(旧详情快照/收益记录)")  # 双写日志提示开始清旧操作
            reset_account_sign_persistence(account)  # 调用清旧函数,删除该账号旧详情快照与收益记录

            # --------------------------------------------------------------
            # 解析代理配置
            # --------------------------------------------------------------
            # 统一解析实际代理: 自动跟随系统代理 > 手动开关;无代理时返回 None
            from runtime.model.app_config import resolve_effective_proxy  # 延迟导入实际代理解析函数,避免启动时加载过多模块
            proxies, proxy_source = resolve_effective_proxy(proxy_cfg)  # 调用解析函数,得到 requests 代理映射与来源说明

            # --------------------------------------------------------------
            # 记录任务开始
            # --------------------------------------------------------------
            self._logger.info(f"🚀 开始执行签到任务:{account}")  # 落盘记录任务开始信息
            self.signal_log.emit(f"🚀 开始执行签到任务:{account}")  # 同步推送任务开始消息到界面日志区
            self._log(f"🌐 代理模式:{proxy_source}")  # 双写日志提示实际代理来源
            self._logger.info(f"🔗 邮件通知:{'开启' if enable_mail else '关闭'}")  # 落盘记录邮件开关状态
            self.signal_progress.emit(10)  # 发射进度信号,进度推进到 10%(准备工作完成)

            # --------------------------------------------------------------
            # 创建网络会话
            # --------------------------------------------------------------
            self._log("🔗 建立网络会话...")  # 双写日志提示即将创建网络会话
            service = ServiceApiService(  # 构造网站 API 服务实例,用于后续的登录/签到等操作
                proxy_mapping=proxies  # 注入解析后的代理映射(None 表示直连)
            )
            service.session_open()  # 打开 requests 会话并加载本地 Cookie 文件

            # --------------------------------------------------------------
            # 流水线第二步[登录]: 优先 Cookie 免登,失败回退密码登录
            # --------------------------------------------------------------
            self._log(f"🍪 尝试Cookie免登:{account}")  # 双写日志提示尝试 Cookie 免登
            cookie_ok, user_info, err = try_cookie_login(service, account)  # 调用 Cookie 免登函数,尝试用本地 Cookie 免登并解析用户信息
            if not cookie_ok:  # 判断 Cookie 免登是否失败(失效/解析失败),失败则回退密码登录
                self._log(f"⚠ Cookie已失效,回退密码登录:{err}")  # 双写日志提示回退密码登录及原因
                # 失效 Cookie 已加载进当前会话,不清理直接登录会带着错误 Cookie
                # 请求 /auth/login 导致登录异常,必须先关闭再重建干净会话
                self._log("🧹 关闭残留Cookie会话并重建干净会话...")  # 双写日志提示重建干净会话
                service.session_close()  # 关闭携带失效 Cookie 的旧会话
                service.session_open()  # 重建不带脏 Cookie 的干净会话
                self._log("🔑 正在访问登录页并提交账号密码...")  # 双写日志提示正在提交登录表单
                login_ret = service.login(account, password)  # 调用登录方法,提交账号密码执行登录
                if "error" in login_ret:  # 检查返回结构是否含 error 字段,含则表示网络层异常
                    raise RuntimeError(f"登录网络异常:{login_ret['error']}")  # 抛出运行时异常,进入统一失败兜底分支
                if login_ret.get("ret") != 1:  # 检查 ret 字段是否为 1,非 1 表示业务层登录失败
                    raise RuntimeError(f"登录业务失败:{login_ret.get('msg')}")  # 抛出业务失败原因的异常
                self._log("👤 登录成功,正在拉取用户中心信息...")  # 双写日志提示拉取用户中心页面
                resp_html = service.get_user_info()  # 请求用户中心页面,返回 HTML 响应内容
                if isinstance(resp_html, dict) and "error" in resp_html:  # 检查返回是否为 dict 且含 error,是则表示请求失败
                    raise RuntimeError(f"获取用户信息失败:{resp_html['error']}")  # 抛出获取用户信息异常
                user_info = cleaning_html(getattr(resp_html, "text", ""))  # 清洗 HTML 并解析为用户信息字典
                self._log(  # 双写密码登录路径的解析调试信息
                    f"👤 密码登录拉取解析:last_use={user_info.get('last_use_text')!r} "  # 日志第一段:最近使用时间原始文本
                    f"warns={user_info.get('parse_warns', [])}"  # 日志第二段:解析过程收集的告警列表
                )

            # --------------------------------------------------------------
            # 登录完成,进度推进到 40%
            # --------------------------------------------------------------
            self.signal_progress.emit(40)  # 发射进度信号,进度推进到 40%(登录阶段完成)
            # Cookie 失效路径已由 cleaning_html 重建 user_info;此处统一兜底为 dict:
            # 既消除 Optional 静态告警,也防止解析异常返回 None 导致后续 .get 崩溃
            if not isinstance(user_info, dict):  # 检查解析结果是否为非字典类型,异常时兜底
                user_info = {}  # 置为空字典,防止后续 .get 调用崩溃
            # 页面没解析出邮箱时用任务账号兜底:保证签到收益记录能按账号落盘/回填
            if not user_info.get("email"):  # 检查页面是否未解析出邮箱
                user_info["email"] = account  # 用任务账号兜底邮箱字段
                fill_persisted_checkin_result(user_info)  # 用本地持久化收益记录补齐该快照
            login_email = user_info.get("email") or account  # 确定登录邮箱,最终兜底为任务账号
            self._log(f"✅ 登录成功,账号:{login_email}")  # 双写日志提示登录成功的账号标识

            # --------------------------------------------------------------
            # 流水线第三步[签到]: 先判重,未签则防风控延时后调接口
            # --------------------------------------------------------------
            # 先以用户中心按钮状态判断:网站签到后按钮为禁用态,
            # 今日已签到就直接跳过,绝不调用 /user/checkin;仅未签到才发起
            signed_ok = False  # 初始化本次是否"新签成功"的标志,默认为 False
            already_signed = is_already_checked_in(user_info)  # 调用判重函数,按按钮禁用态判断今日是否已签到
            # 收益初值取 user_info 回填值(新登录路径 cleaning_html 已从本地记录补)
            gain_msg = str(user_info.get("checkin_gain_flow", "") or "")  # 取回填收益文案,缺省为空字符串

            if already_signed:  # 判断今日是否已签到,已签到则直接跳过,绝不调用签到接口
                btn_text = user_info.get("dom_checkin_text") or "今日已签到"  # 取按钮文案,缺省为提示语
                self._log(f"ℹ️ 页面显示今日已签到(签到按钮禁用:{btn_text}),跳过签到接口")  # 双写日志提示跳过原因
                self._logger.info(f"ℹ️ {account} 今日已签到,不调用/user/checkin")  # 落盘记录跳过签到接口
                self.signal_progress.emit(70)  # 发射进度信号,进度推进到 70%
                # 重复签到同样重拉用户中心:获取最新页面状态(签到时间/流量等),
                # 传入旧快照保护 checkin_gain_flow 不被拉取结果覆盖为空
                self._log("👤 重新拉取用户中心,确认已签到状态...")  # 双写日志提示重拉用户中心
                refreshed = refresh_user_info_after_checkin(service, account, user_info)  # 调用重拉函数,重拉并以旧快照保护收益字段
                if refreshed is not None:  # 检查重拉是否成功(返回新快照),成功才覆盖
                    user_info = refreshed  # 采用重拉得到的最新快照
                    gain_msg = str(user_info.get("checkin_gain_flow", "") or gain_msg)  # 更新收益文案,取不到时保留旧值
            else:  # 今日未签到:防风控延时后调用签到接口
                # 防风控:登录(含 Cookie 免登)与签到之间随机等待 5-40 秒,
                # 模拟人工浏览用户中心后再点签到,避免操作间隔固定/过快
                wait_sec = round(random.uniform(5, 40), 1)  # 生成 5~40 秒的一位小数随机等待时长
                self._log(f"⏳ 防风控延时:登录后等待{wait_sec}秒再签到...")  # 双写日志提示防风控等待
                if not interruptible_sleep(self, wait_sec):  # 调用可中断分段睡眠;返回 False 表示期间收到取消请求
                    self._log("🛑 延时等待期间任务被取消,终止本次签到")  # 双写日志提示因取消而终止
                    return  # 直接结束线程,不再继续签到/发结果(关窗流程)
                self._log("📝 今日未签到,正在执行签到...")  # 双写日志提示开始调用签到接口
                check_ret = service.checkin()  # 调用签到接口,发起 /user/checkin 请求
                self.signal_progress.emit(70)  # 发射进度信号,进度推进到 70%
                if "error" in check_ret:  # 检查返回是否含 error 字段,含则表示网络层异常
                    raise RuntimeError(f"签到网络异常:{check_ret['error']}")  # 抛出运行时异常,进入失败兜底分支

                gain_msg = check_ret.get("msg", "")  # 从接口返回中取消息字段(成功时即收益文案)
                if check_ret.get("ret") == 1:  # 检查 ret 是否为 1,为 1 表示签到业务成功
                    signed_ok = True  # 标记本次为新签成功
                    # 关键:签到接口 msg 是收益的唯一来源,必须回填进 user_info
                    # 并落盘签到记录文件,否则后续映射/持久化拿不到,永远显示 --
                    fill_checkin_result(user_info, check_ret)  # 调用回填函数,把收益回填到 user_info 并写签到记录
                    gain_msg = str(user_info.get("checkin_gain_flow", "") or gain_msg)  # 从回填结果再取收益文案
                    self._log(f"✅ 签到成功:{gain_msg}")  # 双写日志提示签到成功及收益
                    # 持久化前必须重拉用户中心:拿到签到后禁用态按钮/今日已签
                    # 标记/最新签到时间,否则用签到前快照会误判"今日未签到"
                    self._log("👤 重新拉取用户中心,确认签到后状态...")  # 双写日志提示重拉确认状态
                    refreshed = refresh_user_info_after_checkin(service, account, user_info)  # 调用重拉函数,重拉最新页面状态
                    if refreshed is not None:  # 检查重拉是否成功,成功才覆盖快照
                        user_info = refreshed  # 采用签到后的最新快照
                        gain_msg = str(user_info.get("checkin_gain_flow", "") or gain_msg)  # 同步更新收益文案
                else:  # 接口返回非成功
                    # 已签到后签到接口为禁用态,正常不会走到此分支;
                    # 走到这里说明接口返回非成功(如网络抖动/业务异常),
                    # 按未成功但不致命处理,收益等字段由后续映射逻辑兜底
                    self._log(f"ℹ️ 签到业务返回:{gain_msg}")  # 双写日志提示业务返回消息,流程继续

            # --------------------------------------------------------------
            # 组装结果文案(弹窗用)
            # --------------------------------------------------------------
            # 已签跳过带收益提示,新签/未签用接口 msg
            if already_signed:  # 判断是否为已签跳过场景
                result_msg = "今日已签到,无需重复签到。" + (  # 结果文案前半句:固定提示
                    f"本次收益:{gain_msg}" if gain_msg else ""  # 有收益文案时才附加收益部分
                )
            else:  # 新签成功或接口未成功场景
                result_msg = gain_msg  # 结果文案直接使用接口返回的消息

            # --------------------------------------------------------------
            # 流水线第四步[存新]: 映射 detail 落盘
            # --------------------------------------------------------------
            # 清旧在前,此处把本次真实结果统一映射落盘,
            # 与批量共用同一映射函数,避免字段两处漂移;失败路径走异常分支不写
            # 统计本账号签到耗时(到此业务流程全部结束;渲染/写文件/发邮件不计入),
            # 随 detail 一起落盘:重开软件后面板可展示上次签到耗时
            cost_sec = round(perf_counter() - t_start, 2)  # 结算账号签到耗时(秒,保留两位小数)
            detail = user_info_to_account_detail(user_info, signed_ok, account)  # 调用映射函数,把 user_info 映射为持久化 detail 契约
            email_key = str(detail.pop("email", "") or account)  # 从 detail 中弹出邮箱作为持久化 key,缺省回退任务账号
            detail["checkin_cost_sec"] = cost_sec  # 把本次签到耗时写入详情字典,供重开软件后展示
            save_account_detail(email_key, detail)  # 调用保存函数,按邮箱 key 落盘账号详情快照
            self._log(f"💾 本次签到结果已本地持久化:{email_key}")  # 双写日志提示持久化完成
            self._log(f"⏱️ {email_key} 签到耗时:{format_sign_duration(cost_sec)}")  # 双写日志提示可读的耗时文案

            # --------------------------------------------------------------
            # 渲染账号详情文本块(三出口共用)
            # --------------------------------------------------------------
            # 账号详细信息统一由工具函数渲染(界面面板/邮件正文/结果文件
            # 三出口共用同一份文本),输入与界面持久化完全相同的 detail 契约
            info_block = format_user_info_text(email_key, detail)  # 调用渲染函数,渲染三出口共用的账号详情文本块

            # --------------------------------------------------------------
            # 覆盖写签到结果文件
            # --------------------------------------------------------------
            # 把本次单账号结果覆盖写入"邮件目录/mail_body.txt",
            # 该文件永远只保留最近一次任务结果
            result_text = (  # 组装单账号结果文件文本内容
                "签到单账号结果\n"  # 结果文件标题行
                f"任务时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"  # 任务时间戳加一个空行
                f"{info_block}\n"  # 账号详情文本块
            )
            write_checkin_result_text(result_text)  # 调用写入函数,覆盖写入签到结果文件
            self._log("📝 本次签到结果已写入签到结果文件")  # 双写日志提示结果文件写入完成

            # --------------------------------------------------------------
            # 流水线第五步[发邮件]: 按开关发送通知邮件
            # --------------------------------------------------------------
            # 持久化完成后再按开关发送通知邮件
            if enable_mail:  # 判断邮件开关是否开启,开启时才尝试发送
                to_list = smtp_cfg.get("SMTP_DEFAULT_TO_LIST", [])  # 从 SMTP 配置中取默认收件人列表,缺省为空列表
                if to_list:  # 判断是否有收件人,有收件人才真正发送
                    self._log(f"📧 开始发送通知邮件,收件人 {len(to_list)} 个")  # 双写日志提示收件人数量
                    sender = ServiceEmailSender(  # 构造 SMTP 邮件发送器实例
                        smtp_host=smtp_cfg["SMTP_DEFAULT_HOST"],  # SMTP 服务器主机地址
                        smtp_port=smtp_cfg["SMTP_DEFAULT_PORT"],  # SMTP 服务器端口
                        smtp_user=smtp_cfg["SMTP_SENDER_EMAIL"],  # 发件邮箱账号
                        smtp_password=smtp_cfg["SMTP_AUTH_CODE"],  # 邮箱授权码
                    )
                    for to_mail in to_list:  # 遍历收件人列表,逐个发送同一正文的邮件
                        self._log(f"📤 正在发送邮件至 {to_mail} ...")  # 双写日志提示当前发送目标
                        # 邮件正文 = 账号详细信息格式化块(含签到耗时/收益/流量)
                        ok, msg = sender.send_text(  # 调用发送方法,发送纯文本邮件,返回是否成功与说明
                            to_mail, "签到通知", info_block  # 收件人、固定主题、详情正文
                        )
                        if ok:  # 判断是否发送成功
                            self._log(f"✅ 邮件发送成功:{to_mail}({msg})")  # 双写日志提示发送成功
                        else:  # 发送失败
                            self._log(f"❌ 邮件发送失败:{to_mail}({msg})", logging.WARNING)  # 以警告级别双写失败信息
                else:  # 开关开着但收件人为空
                    self._log("⚠ 已开启邮件通知,但收件人为空,跳过发送", logging.WARNING)  # 以警告级别双写跳过原因
            else:  # 邮件开关关闭
                self._logger.info("📧 邮件通知未开启,跳过发送")  # 仅落盘记录跳过发送,不推送到 UI

            # --------------------------------------------------------------
            # 企业微信推送:推送内容与邮件正文完全一致(info_block 详情块),失败原因走异常分支
            # --------------------------------------------------------------
            self._push_wecom(info_block)  # 按企业微信开关推送完整详情(未启用则静默跳过)

            # --------------------------------------------------------------
            # 进度置满,组装并发射最终结果
            # --------------------------------------------------------------
            self.signal_progress.emit(100)  # 发射进度信号,进度推进到 100%(全部完成)

            # 组装结果:详情已由本 Worker 持久化,detail 副本随结果带给 Controller,
            # Controller 只负责回填 UI,不再二次组装字段/重复存盘
            result = {  # 组装最终结果字典,随信号带给 Controller
                "ok": True,  # 流程整体成功标志
                "signed": signed_ok,  # 是否本次新签成功
                "already_signed": already_signed,  # 是否今日已签而跳过接口
                "msg": result_msg,  # 弹窗用结果文案
                "email": email_key,  # 持久化 key(实际邮箱)
                "detail": detail,  # 详情契约副本,供 UI 直接回填
            }
            if signed_ok:  # 判断是否为新签成功场景
                finish_desc = f"新签到成功:{gain_msg}"  # 带收益的成功描述
            elif already_signed:  # 判断是否为今日已签跳过场景
                finish_desc = "今日已签到,已跳过/user/checkin"  # 跳过描述
            else:  # 走到接口但未成功的场景
                finish_desc = gain_msg or "未签到"  # 优先用接口消息,兜底为"未签到"
            self._logger.info(f"🎉 签到任务完成:{account} | 结果:{finish_desc}")  # 落盘记录任务完成情况
            self.signal_result.emit(result)  # 发射最终成功结果信号给 Controller

        # ------------------------------------------------------------------
        # 异常兜底分支
        # ------------------------------------------------------------------
        except Exception as e:  # 捕获流水线任意异常:记录、写失败结果文件并发失败信号
            cost_sec = round(perf_counter() - t_start, 2)  # 失败也结算已消耗的时长
            self._logger.error(f"❌ 签到任务异常:{account or '未知账号'} | {str(e)}", exc_info=True)  # 落盘错误堆栈信息
            self.signal_log.emit(f"❌ 签到任务异常:{str(e)}")  # 推送异常消息到界面日志区
            self.signal_log.emit(  # 推送失败账号的签到耗时信息
                f"⏱️ {account or '未知账号'} 签到耗时:{format_sign_duration(cost_sec)}"  # 失败耗时文案
            )
            # 失败同样覆盖写入结果文件,保证其内容与最近一次任务一致
            try:  # 结果文件写入再包一层 try,避免二次异常影响失败信号发射
                fail_text = (  # 组装失败结果文本内容
                    "签到单账号结果\n"  # 标题行
                    f"任务时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"  # 任务时间戳
                    f"账号:{account or '未知账号'}\n"  # 失败账号标识
                    f"⏱️ 签到耗时:{format_sign_duration(cost_sec)}\n"  # 已耗时长
                    f"❌ 签到失败:{e}\n"  # 失败原因
                )
                write_checkin_result_text(fail_text)  # 覆盖写失败结果文件
            except Exception as write_err:  # 捕获结果文件写入失败的异常
                self._logger.warning(f"⚠ 失败结果写入结果文件异常:{write_err}")  # 仅落盘告警,不掩盖原异常
            # 旧持久化已在任务开头清除,失败时无新快照;带 email 供 Controller 刷新面板
            try:  # 失败也推送企业微信,告知失败原因(推送失败不影响结果信号)
                fail_wecom = (  # 组装失败推送文案
                    "❌ 签到失败\n"  # 结果标题
                    f"账号:{account or '未知账号'}\n"  # 账号
                    f"原因:{e}"  # 失败原因
                )
                self._push_wecom(fail_wecom)  # 按企业微信开关推送失败原因
            except Exception as wecom_err:  # 推送异常单独吞掉
                self._logger.warning(f"⚠ 失败推送企业微信异常:{wecom_err}")  # 仅落盘告警
            self.signal_result.emit(  # 发射失败结果信号,供 Controller 刷新面板
                {"ok": False, "signed": False, "msg": str(e), "email": account}  # 失败结果载荷
            )
        # ------------------------------------------------------------------
        # finally 收尾: 保证关闭网络会话
        # ------------------------------------------------------------------
        finally:  # 无论成功、失败还是取消,都保证关闭会话
            if service is not None:  # 判断会话是否确实创建过,创建过才需要关闭
                try:  # 关闭操作本身也可能抛异常,单独吞掉只记日志
                    service.session_close()  # 关闭底层 requests 会话,释放连接资源
                    self._logger.info(f"🧹 签到会话已关闭:{account}")  # 落盘记录会话已关闭
                except Exception as close_err:  # 捕获关闭异常,不掩盖主流程结果
                    self._logger.warning(f"⚠ 关闭签到会话异常:{close_err}")  # 仅落盘告警信息

    def _push_wecom(self, text: str) -> None:
        """按企业微信推送开关发送一条签到结果消息(未启用/未配置则静默跳过).

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
