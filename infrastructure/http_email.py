#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: http_email.py
# 归属: infrastructure 基础设施层 —— SMTP 邮件发送底层组件
# ------------------------------------------------------------------------------
# 文件用途:
#   SMTP 邮件发送底层组件,仅提供纯文本邮件发送能力(不含任何业务报告组装逻辑);
#   内置 QQ 邮箱默认 SMTP 配置,自动处理中文编码、SSL/STARTTLS 选型与连接关闭,
#   上层业务传入普通中文字符串即可发信.
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 infrastructure 基础设施层的通知能力模块;仅依赖 Python 标准库
#   (smtplib/email),不依赖 runtime/updater_app 等业务层;由上层需要邮件通知
#   的业务模块实例化 EmailNotifier 调用.
#
#   关联组件:
#     - 上游: service.send_email 业务层
#     - 下游: smtplib、email.mime.text.MIMEText、email.header.Header
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 内置 smtp.qq.com:465 SSL 默认配置且支持实例传参全覆盖
#   2. 465 端口走 SMTP_SSL、587 端口走 SMTP+starttls、其余端口明文连接
#   3. 自动完成中文标题/正文编码
#   4. 固定 local_hostname="localhost" 规避 Windows 中文主机名 EHLO 报错
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅实现邮件发送能力,不含业务报告组装逻辑
#   - 对外公开方法: send_text_email(to_email, subject, body) -> (ok, msg)
#   - QQ 邮箱需要网页开启 POP3/SMTP,使用授权码登录,不要填账号密码
# ------------------------------------------------------------------------------
# 线程模型:
#   每次 send_text_email 调用均创建独立的 SMTP 连接,发送完成即关闭;多线程并发
#   调用不同实例完全安全,无共享状态竞争;同一实例被多线程并发调用时,因无共享
#   可变状态也是安全的(仅读取实例属性).
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、smtplib、email.mime.text.MIMEText、
#             email.header.Header、typing
#   - 第三方: 无
#   - 项目内: 无
# ==============================================================================
import logging  # 记录发信全过程的 info/debug/error 日志
import smtplib  # SMTP 协议客户端:提供 SMTP_SSL 隐式加密与 SMTP+starttls 显式升级
from email.mime.text import MIMEText  # 构造 text/plain 的 MIME 邮件体,内部承载 UTF-8 编码
from email.header import Header  # 对中文邮件标题做 MIME 头编码,避免乱码与 SMTP 头校验失败
from typing import Tuple, Optional  # 类型标注:Tuple 用于返回值,Optional 表示可空参数

_logger = logging.getLogger("EmailNotifier")  # 模块日志器:由 App 根 logger 统一配置 handler 输出


class EmailNotifier:
    """
    SMTP邮件发送底层类;内置QQ邮箱默认配置;实例传参可以覆盖全部SMTP配置

    工作流程:
        1. 构造 MIMEText 邮件体(text/plain, UTF-8 编码),设置 From/To/Subject
        2. 标题通过 email.header.Header 编码,避免中文裸头被 SMTP 服务器拒绝
        3. 根据端口选择连接方式:465 → SMTP_SSL(隐式加密);587 → SMTP+STARTTLS(显式升级)
        4. 其他端口 → 明文 SMTP 连接(不推荐,25 端口常被运营商封禁)
        5. 使用邮箱账号 + 授权码登录 SMTP 服务器(QQ 邮箱需开启 POP3/SMTP 并使用授权码)
        6. 发送邮件消息 → 返回 (True, "邮件发送成功") 或 (False, 错误信息)
        7. finally 中发送 QUIT 指令正常关闭连接,释放套接字资源

    端口与加密说明:
        - 465 端口:SMTP_SSL,连接建立时即启用 TLS 加密(隐式加密)
        - 587 端口:SMTP+STARTTLS,先明文握手再升级为 TLS 加密(显式加密)
        - 25 端口:明文 SMTP,不加密,大多数运营商封禁,仅用于内部测试

    线程安全说明:
        - 每次 send_text_email 调用均创建独立的 SMTP 连接,发送完成即关闭
        - 多线程并发调用不同实例完全安全,无共享状态竞争
        - 同一实例被多线程并发调用时,因无共享可变状态也是安全的(仅读取实例属性)

    类常量(默认配置,可被实例构造参数覆盖):
        DEFAULT_SMTP_SERVER (str): 默认 SMTP 服务器地址,值为 "smtp.qq.com"
        DEFAULT_SMTP_PORT (int): 默认 SSL 端口,值为 465
        DEFAULT_SENDER_EMAIL (str): 默认发件邮箱地址
        DEFAULT_SENDER_AUTH_CODE (str): 默认 SMTP 授权码;默认留空,由调用方从配置注入,禁止硬编码密钥

    实例属性:
        smtp_server (str): 当前实例使用的 SMTP 服务器域名
        smtp_port (int): 当前实例使用的 SMTP 端口号
        sender_email (str): 当前实例的发件人邮箱地址
        sender_auth_code (str): 当前实例的 SMTP 登录授权码
    """
    DEFAULT_SMTP_SERVER = "smtp.qq.com"  # 默认 QQ 邮箱 SMTP 服务器域名
    DEFAULT_SMTP_PORT = 465  # 默认端口 465,对应隐式 SSL 连接
    DEFAULT_SENDER_EMAIL = "enghin110@qq.com"  # 默认发件邮箱账号
    DEFAULT_SENDER_AUTH_CODE = ""  # 默认 SMTP 授权码:禁止硬编码在代码中,必须由调用方从配置(SMTP_AUTH_CODE)传入;留空避免泄露

    def __init__(
        self,
        smtp_server: Optional[str] = None,
        smtp_port: Optional[int] = None,
        sender_email: Optional[str] = None,
        sender_auth_code: Optional[str] = None
    ):
        """
        初始化邮件发送实例;不传参使用内置QQ邮箱默认配置

        :param smtp_server: SMTP服务器域名,None使用默认
        :type smtp_server: Optional[str]
        :param smtp_port: SMTP端口;465(SSL)/587(STARTTLS),None使用默认
        :type smtp_port: Optional[int]
        :param sender_email: 发件人完整邮箱地址,None使用默认
        :type sender_email: Optional[str]
        :param sender_auth_code: SMTP授权码,QQ邮箱不要填登录密码;None使用默认
        :type sender_auth_code: Optional[str]
        """
        self.smtp_server = smtp_server or self.DEFAULT_SMTP_SERVER  # 未显式传入则回退类常量服务器
        self.smtp_port = smtp_port or self.DEFAULT_SMTP_PORT  # 未传(或传 0)时回退默认端口
        self.sender_email = sender_email or self.DEFAULT_SENDER_EMAIL  # 发件邮箱回退默认值
        self.sender_auth_code = sender_auth_code or self.DEFAULT_SENDER_AUTH_CODE  # 授权码回退默认值

    def send_text_email(self, to_email: str, subject: str, body: str) -> Tuple[bool, str]:
        """
        发送纯文本邮件;内部自动处理中文编码,上层直接传入普通中文字符串

        :param to_email: 接收方邮箱地址字符串,单个邮箱
        :type to_email: str
        :param subject: 邮件标题,支持中文,无需外部编码
        :type subject: str
        :param body: 邮件正文纯文本,支持中文
        :type body: str
        :return: Tuple[bool, str] (发送是否成功, 结果描述消息)
        :rtype: Tuple[bool, str]
        """
        msg = MIMEText(body, "plain", "utf-8")  # 构造 text/plain、UTF-8 编码的邮件体,中文无需上层预处理
        msg["From"] = self.sender_email  # 设置发件人头
        msg["To"] = to_email  # 设置收件人头
        msg["Subject"] = Header(subject, "utf-8").encode()  # 标题经 Header 编码,规避中文裸头被 SMTP 服务器拒绝
        # 安全要求:只记录服务器/账号/收件人,禁止打印授权码
        _logger.info(
            f"📧 准备发送邮件: {self.sender_email} -> {to_email} "  # 记录发件/收件账号,便于追溯投递链路
            f"| 主题:{subject} | SMTP={self.smtp_server}:{self.smtp_port}"  # 同时记录主题与服务器端口,便于定位连接问题
        )
        server: Optional[smtplib.SMTP] = None  # 先置空,保证连接建立前就失败时 finally 判空不会误操作
        try:
            if self.smtp_port == 465:  # 465 约定为隐式 SSL 端口,必须一连接就加密
                _logger.debug(f"🔗 建立SMTP_SSL加密连接:{self.smtp_server}:{self.smtp_port}")
                server = smtplib.SMTP_SSL(
                    self.smtp_server,  # SMTP 服务器主机
                    self.smtp_port,  # SSL 端口
                    local_hostname="localhost",  # 固定本机名:规避 Windows 中文计算机名导致 EHLO 报错的系统坑
                    timeout=10  # 10 秒超时,避免网络异常时无限等待
                )
            else:  # 非 465 端口:先建立明文 SMTP 连接,再按需升级
                _logger.debug(f"🔗 建立SMTP连接:{self.smtp_server}:{self.smtp_port}")
                server = smtplib.SMTP(
                    self.smtp_server,  # SMTP 服务器主机
                    self.smtp_port,  # SMTP 端口(如 587/25)
                    local_hostname="localhost",  # 同样固定 localhost,规避中文主机名 EHLO 问题
                    timeout=10  # 连接超时 10 秒
                )
                # 按端口约定:587=STARTTLS升级,25=明文不升级
                if self.smtp_port == 587:  # 587 端口要求先明文握手再显式升级 TLS
                    _logger.debug("🔐 升级STARTTLS加密通道")
                    server.starttls()  # 在明文连接上协商 TLS,后续登录与发信均加密
            _logger.debug(f"🔐 SMTP授权登录中:{self.sender_email}")
            server.login(self.sender_email, self.sender_auth_code)  # 使用邮箱账号+授权码登录(QQ 邮箱授权码并非登录密码)
            server.send_message(msg)  # 发送已构造完成的 MIME 消息对象
            _logger.info(f"✅ 邮件投递成功:{to_email}")
            return True, "邮件发送成功"  # 投递成功:返回成功标记与中文描述
        except Exception as e:  # 网络不通、认证失败、收件地址非法等一切异常均在此收口
            _logger.error(f"❌ 邮件发送失败:{to_email} | {type(e).__name__}:{str(e)}")  # 记录异常类型与详情,但不含授权码
            return False, f"邮件发送失败:{str(e)}"  # 失败不抛出,转为 (False, 原因) 供上层直接界面提示
        finally:  # 无论成功失败都尝试收尾,防止套接字泄漏
            if server is not None:  # 连接可能尚未建立成功,需判空
                try:
                    server.quit()  # 发送 QUIT 指令正常结束 SMTP 会话
                    _logger.debug("🧹 SMTP连接已关闭")
                except Exception:  # 关闭阶段的异常无业务价值,忽略即可
                    pass
