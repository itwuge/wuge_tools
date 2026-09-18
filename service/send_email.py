# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: send_email.py
# 归属: service 业务服务层 —— 邮件发送业务层封装
# ------------------------------------------------------------------------------
# 文件用途:
#   邮件发送的业务层封装;对底层 infrastructure.http_email.EmailNotifier 做轻量包装,
#   透传 SMTP 配置与日志对象,对上(controller/worker)提供统一的纯文本邮件发送
#   业务接口;本层不修改底层 SMTP 发送逻辑,仅做参数透传与发送结果日志留痕.
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 service 业务服务层;对上提供 ServiceEmailSender 业务类,
#   对下依赖 infrastructure.http_email.EmailNotifier 完成实际 SMTP 发信;
#   本层职责单一:参数透传、日志留痕、生命周期管理(with 上下文,底层为短连接无需释放).
#
#   关联组件:
#     - 上游: controller/worker
#     - 下游: infrastructure.http_email.EmailNotifier
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 构造时透传 SMTP 服务器/端口/账号/授权码到底层 EmailNotifier
#   2. send_text() 委托底层发送纯文本邮件,记录发送开始/成功/失败日志
#   3. with 上下文管理器(底层每次发信均建独立连接并关闭,close 为空操作)
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅做参数透传与日志留痕,不修改底层邮件发送逻辑
#   - 底层每次发信均建独立连接并在 finally 中关闭,无长连接资源
#   - 网络 IO 阻塞,建议在 QThread 子线程调用
# ------------------------------------------------------------------------------
# 线程模型:
#   每次 send_text 调用均由底层创建独立 SMTP 连接并在 finally 中关闭,无长连接资源;
#   网络 IO 阻塞,建议在 QThread 子线程调用,避免阻塞 UI;
#   多线程并发调用不同实例完全安全,同一实例并发调用也安全(无共享可变状态).
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、typing
#   - 第三方: 无(底层依赖 smtplib 标准库)
#   - 项目内: infrastructure.http_email.EmailNotifier
# ==============================================================================
import logging  # 标准库:记录邮件发送过程的 info/warning 日志
from typing import Tuple  # 标准库:类型注解(返回类型)
from infrastructure.http_email import EmailNotifier  # 项目内:底层 SMTP 邮件发送器

_logger = logging.getLogger("ServiceEmailSender")  # 模块级 logger:上层未传 logger 时的兜底


class ServiceEmailSender:
    """
    Service 业务邮件发送封装类,封装底层 EmailNotifier

    职责:
        - 构造时将 SMTP 服务器/端口/发件账号/授权码透传到底层 EmailNotifier
        - send_text() 委托底层发送纯文本邮件,记录发送开始/成功/失败日志
        - with 上下文管理器(底层为短连接,close 为空操作)

    实例属性:
        _inner_email (EmailNotifier): 底层邮件发送器实例
        timeout (int): 预留超时配置(底层固定 10s,暂未透传)
        logger: 外部注入的日志对象
    """
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        timeout: int = 15,
        logger=None
    ):
        """
        初始化业务邮件发送封装,创建底层 EmailNotifier 实例

        :param smtp_host: SMTP 服务器域名,例如 "smtp.qq.com"
        :type smtp_host: str
        :param smtp_port: SMTP 端口;465(SSL)/587(STARTTLS)
        :type smtp_port: int
        :param smtp_user: 发件人完整邮箱地址
        :type smtp_user: str
        :param smtp_password: SMTP 授权码(QQ 邮箱不要填登录密码)
        :type smtp_password: str
        :param timeout: 预留超时秒数(底层固定 10s,本参数暂未透传)
        :type timeout: int
        :param logger: 外部日志实例,None 时使用模块默认 logger
        :type logger: Any
        """
        self._inner_email = EmailNotifier(  # 创建底层邮件发送器实例
            smtp_server=smtp_host,  # 透传 SMTP 服务器
            smtp_port=smtp_port,  # 透传 SMTP 端口
            sender_email=smtp_user,  # 透传发件邮箱
            sender_auth_code=smtp_password  # 透传授权码
        )
        self.timeout = timeout  # 保存超时配置(预留,底层暂未使用)
        # 上层未传logger时使用模块logger,保证邮件步骤始终可追踪
        self.logger = logger if logger else _logger  # 未传 logger 时回退模块 logger

    def send_text(self, to_email: str, subject: str, body: str) -> Tuple[bool, str]:
        """
        发送纯文本邮件,委托底层 EmailNotifier.send_text_email 执行

        :param to_email: 接收方邮箱地址字符串(单个邮箱)
        :type to_email: str
        :param subject: 邮件标题,支持中文,无需外部编码
        :type subject: str
        :param body: 邮件正文纯文本,支持中文
        :type body: str
        :return: (发送是否成功, 结果描述消息)
        :rtype: Tuple[bool, str]
        """
        self.logger.info(f"📧 [ServiceEmailSender] 发送纯文本邮件 -> {to_email} | 主题:{subject}")  # 记录发信起点
        ok, msg = self._inner_email.send_text_email(to_email, subject, body)  # 委托底层执行实际 SMTP 发信
        if ok:  # 发送成功分支
            self.logger.info(f"✅ [ServiceEmailSender] 通知邮件发送成功 -> {to_email}")  # 记录成功日志
        else:  # 发送失败分支
            self.logger.warning(f"❌ [ServiceEmailSender] 通知邮件发送失败 -> {to_email} | {msg}")  # 记录失败警告与原因
        return ok, msg  # 返回底层发信结果元组

    def close(self):
        """
        关闭邮件发送器(底层为短连接,每次发信后已自动关闭,此处为空操作)

        保留此方法是为了与其他 service 类保持一致的生命周期接口,支持 with 上下文.
        """
        pass  # 底层 EmailNotifier 每次发信均建独立连接并在 finally 中关闭,无需额外释放

    def __enter__(self):
        """with 上下文管理器入口,返回自身实例"""
        return self  # 返回自身实例供 as 变量绑定

    def __exit__(self, exc_type, exc_val, exc_tb):
        """with 上下文管理器出口,调用 close(空操作)"""
        self.close()  # 退出时调用 close(底层短连接已自动关闭)


# 模块公开 API 符号表:控制 from service.send_email import * 的导出范围
__all__ = ["ServiceEmailSender"]  # 仅导出邮件发送业务服务类
