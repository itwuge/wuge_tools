# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: notify_wecom.py
# 归属: service 业务服务层 —— 企业微信推送业务层封装
# ------------------------------------------------------------------------------
# 文件用途:
#   企业微信群机器人推送的业务层封装;对底层 infrastructure.http_wecom
#   的 WeComWebhookNotifier 做轻量包装,透传 Webhook 地址与日志对象,
#   对上(controller/worker)提供统一的文本/Markdown 推送业务接口;
#   本层不修改底层推送逻辑,仅做参数透传与推送结果日志留痕.
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 service 业务服务层;对上提供 ServiceWeComPusher 业务类,
#   对下依赖 infrastructure.http_wecom.WeComWebhookNotifier 完成实际推送;
#   本层职责单一:参数透传、日志留痕、消息类型分发.
#
#   关联组件:
#     - 上游: controller/worker(签到结果推送等业务)
#     - 下游: infrastructure.http_wecom.WeComWebhookNotifier(底层完全自包含)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 构造时透传群机器人 Webhook 地址与消息类型到底层 WeComWebhookNotifier
#   2. send() 按配置的消息类型(text/markdown)分发到底层对应发送方法
#   3. send_text() / send_markdown() 直接委托底层对应方法,记录开始/成功/失败日志
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅做参数透传与日志留痕,不修改底层企业微信推送逻辑
#   - 底层每次推送均创建独立 HTTP 请求,无长连接资源
#   - 网络 IO 阻塞,建议在 QThread 子线程调用
# ------------------------------------------------------------------------------
# 线程模型:
#   每次推送调用均由底层创建独立 HTTP 请求,无长连接资源;
#   网络 IO 阻塞,建议在 QThread 子线程调用,避免阻塞 UI;
#   多线程并发调用不同实例完全安全,同一实例并发调用也安全(无共享可变状态).
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、typing
#   - 第三方: 无(底层依赖 requests 第三方库)
#   - 项目内: infrastructure.http_wecom.WeComWebhookNotifier
# ==============================================================================
import logging  # 标准库:记录推送过程的 info/warning 日志
from typing import Any, Optional, Tuple  # 标准库:类型注解(返回类型/可选参数)

from infrastructure.http_wecom import WeComWebhookNotifier  # 项目内:底层企业微信群机器人推送器

_logger = logging.getLogger("ServiceWeComPusher")  # 模块级 logger:上层未传 logger 时的兜底


class ServiceWeComPusher:
    """
    Service 业务企业微信推送封装类,封装底层 WeComWebhookNotifier

    职责:
        - 构造时将群机器人 Webhook 地址与消息类型透传到底层 WeComWebhookNotifier
        - send() 按配置的消息类型分发到 text / markdown 发送方法
        - send_text() / send_markdown() 委托底层发送,记录发送开始/成功/失败日志

    实例属性:
        _inner_notifier (WeComWebhookNotifier): 底层企业微信推送器实例
        msg_type (str): 推送消息类型(text=纯文本/markdown=Markdown)
        logger: 外部注入的日志对象
    """
    def __init__(
        self,
        webhook_url: str,
        msg_type: str = "text",
        logger: Optional[Any] = None,
    ):
        """
        初始化业务企业微信推送封装,创建底层 WeComWebhookNotifier 实例

        :param webhook_url: 群机器人 Webhook 完整地址(含 key= 鉴权参数)
        :type webhook_url: str
        :param msg_type: 推送消息类型;text=纯文本(默认,最兼容)/markdown=Markdown(仅企业微信内渲染)
        :type msg_type: str
        :param logger: 外部日志实例,None 时使用模块默认 logger
        :type logger: Optional[Any]
        """
        self._inner_notifier = WeComWebhookNotifier(  # 创建底层推送器实例
            webhook_url=webhook_url,  # 透传 Webhook 地址
        )
        self.msg_type = msg_type if msg_type in ("text", "markdown") else "text"  # 非法类型回退纯文本
        self.logger = logger or _logger  # 外部日志实例兜底为模块默认

    def send(self, content: str) -> Tuple[bool, str]:
        """
        按配置的消息类型发送一条消息(业务统一入口)

        :param content: 消息内容(纯文本或 Markdown)
        :type content: str
        :return: (是否成功, 描述信息)
        :rtype: Tuple[bool, str]
        """
        if not self._inner_notifier.is_configured():  # 未配置 Webhook 地址
            self.logger.warning("⚠ 企业微信推送跳过:未配置 Webhook 地址")
            return False, "未配置企业微信 Webhook 地址"
        if self.msg_type == "markdown":  # Markdown 消息
            return self.send_markdown(content)  # 委托底层 Markdown 发送
        return self.send_text(content)  # 默认纯文本发送

    def send_text(self, content: str) -> Tuple[bool, str]:
        """
        发送纯文本消息(最兼容,个人微信同步也能正常显示)

        :param content: 文本内容,最长 2048 字节
        :type content: str
        :return: (是否成功, 描述信息)
        :rtype: Tuple[bool, str]
        """
        self.logger.info("📨 企业微信推送开始:text")  # 记录发送开始日志
        ok, msg = self._inner_notifier.send_text(self._clip(content, 2048))  # 委托底层发送(text 协议上限2048字节)
        self._log_result("text", ok, msg)  # 记录发送结果日志
        return ok, msg  # 透传底层返回结果

    def send_markdown(self, content: str) -> Tuple[bool, str]:
        """
        发送 Markdown 消息(仅企业微信内渲染,个人微信同步可能不支持)

        :param content: Markdown 内容,最长 4096 字节
        :type content: str
        :return: (是否成功, 描述信息)
        :rtype: Tuple[bool, str]
        """
        self.logger.info("📨 企业微信推送开始:markdown")  # 记录发送开始日志
        ok, msg = self._inner_notifier.send_markdown(self._clip(content, 4096))  # 委托底层发送(markdown 协议上限4096字节)
        self._log_result("markdown", ok, msg)  # 记录发送结果日志
        return ok, msg  # 透传底层返回结果

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _clip(content: str, limit: int) -> str:
        """按企业微信消息字节上限截断内容(避免超长推送失败).

        企业微信 text 上限 2048 字节、markdown 上限 4096 字节;
        按 UTF-8 字节数截断,截断处保留完整字符,尾部追加截断提示.

        :param content: 原始消息内容
        :type content: str
        :param limit: 字节数上限(text=2048/markdown=4096)
        :type limit: int
        :return: 截断后的安全内容
        :rtype: str
        """
        raw = str(content)  # 统一转字符串
        if len(raw.encode("utf-8")) <= limit:  # 未超限直接返回
            return raw  # 原样返回
        tail = "\n…(内容过长已截断)"  # 截断提示后缀
        safe = limit - len(tail.encode("utf-8"))  # 为后缀预留字节
        # 按字节截断并丢弃末尾可能残缺的多字节字符,再拼提示
        return raw.encode("utf-8")[:safe].decode("utf-8", errors="ignore") + tail

    def _log_result(self, msg_type: str, ok: bool, msg: str) -> None:
        """
        记录推送结果日志(成功/失败统一留痕)

        :param msg_type: 实际发送的消息类型(text/markdown)
        :type msg_type: str
        :param ok: 是否发送成功
        :type ok: bool
        :param msg: 底层返回的描述信息
        :type msg: str
        :return: 无返回值
        :rtype: None
        """
        if ok:  # 发送成功
            self.logger.info(f"✅ 企业微信推送成功:{msg_type} | {msg}")
        else:  # 发送失败:明确记录失败原因
            self.logger.warning(f"⚠ 企业微信推送失败:{msg_type} | {msg}")
