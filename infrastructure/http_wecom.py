#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: http_wecom.py
# 归属: infrastructure 基础设施层 —— 企业微信(WeCom)群机器人 Webhook 推送底层组件
# ------------------------------------------------------------------------------
# 文件用途:
#   企业微信群机器人 Webhook 消息推送底层组件,提供纯文本(text)、
#   Markdown(markdown)、图片(image)三种消息类型的发送能力;
#   不包含任何业务报告组装逻辑,上层业务传入完整 payload 即可推送.
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 infrastructure 基础设施层的通知能力模块;仅依赖 Python 标准库与
#   第三方库 requests,【不依赖任何项目内其他模块】,与其他基础设施组件
#   (http_client/http_email 等)完全解耦;由上层 service 业务模块调用.
#
#   关联组件:
#     - 上游: service.notify_wecom 业务层 / controller 测试按钮
#     - 下游: requests(第三方 HTTP 库)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 构造时传入完整 Webhook 地址(含 key= 鉴权参数),校验格式与来源域名
#   2. 支持 text / markdown / image 三种企业微信群机器人消息类型
#   3. 统一 POST JSON payload,按 errcode==0 判定成功并返回 (ok, msg) 元组
#   4. 空 webhook 地址判定为"未配置",由上层决定跳过,不抛异常
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅实现消息推送能力,不含业务内容组装、图片生成等逻辑
#   - 对外公开方法: send_text / send_markdown / send_image
#   - 不维护长连接:每次推送独立请求,失败不自动重试(由上层按需重试)
#   - Webhook 地址即群凭据:只发不收,严禁写死/上传,泄露后需在群设置里重建
# ------------------------------------------------------------------------------
# 线程模型:
#   每次推送调用均创建独立 HTTP 请求,无共享可变状态;
#   同一实例被多线程并发调用完全安全,可被任意工作线程复用.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、typing
#   - 第三方: requests
#   - 项目内: 无(底层完全自包含,不依赖其他 infrastructure 模块)
# ==============================================================================
import logging  # 标准库:记录推送全过程的 info/warning/error 日志
from typing import Any, Dict, List, Optional, Tuple  # 标准库:类型标注

import requests  # 第三方:HTTP 请求库,POST JSON payload 到企业微信 Webhook

# 本模块日志器:由 App 根 logger 统一配置 handler 输出
_logger = logging.getLogger("WeComWebhookNotifier")


class WeComWebhookNotifier:
    """
    企业微信群机器人 Webhook 推送底层类;仅依赖 requests 与标准库,完全自包含.

    工作流程:
        1. 构造时校验并保存完整 Webhook 地址(须含 key= 鉴权参数)
        2. 调用方组装企业微信约定的 payload(msgtype=text/markdown/image)
        3. 统一 POST 到 Webhook 地址,按响应 errcode==0 判定成功
        4. 返回 (True, "推送成功") 或 (False, 错误信息)

    消息类型说明:
        - text:     纯文本,最兼容,个人微信(微信插件)同步也能正常显示
        - markdown: Markdown 文本,仅企业微信内渲染,个人微信同步可能
                    显示"暂不支持此消息类型"
        - image:    图片消息,需自行准备 base64 与 md5(上层生成图片后调用)

    线程安全说明:
        - 每次调用独立 HTTP 请求,无共享可变状态,多线程并发安全

    类常量:
        API_BASE (str): 企业微信群机器人 Webhook 固定接口域名
        DEFAULT_TIMEOUT (int): 默认请求超时秒数

    实例属性:
        webhook_url (str): 完整 Webhook 地址(含 key= 鉴权参数)
        timeout (int): 请求超时秒数
    """
    API_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"  # 群机器人固定接口
    DEFAULT_TIMEOUT = 10  # 默认请求超时秒数

    def __init__(self, webhook_url: str, timeout: int = DEFAULT_TIMEOUT):
        """
        初始化企业微信群机器人推送器.

        :param webhook_url: 完整 Webhook 地址,形如
            https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
        :type webhook_url: str
        :param timeout: 请求超时秒数,默认 10
        :type timeout: int
        """
        self.webhook_url = (webhook_url or "").strip()  # 去首尾空白保存
        self.timeout = timeout  # 超时秒数

    # ------------------------------------------------------------------
    # 配置校验
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """判断是否已配置有效 Webhook 地址(非空且格式合法)."""
        return bool(self.webhook_url)  # 非空即视为已配置,具体合法性由上层提示

    @classmethod
    def is_valid_webhook(cls, url: str) -> bool:
        """
        校验 Webhook 地址格式:必须指向企业微信官方接口且携带 key 参数.

        :param url: 待校验的 Webhook 地址
        :type url: str
        :return: True=格式合法;False=缺 key 参数/域名不符/非完整 URL
        :rtype: bool
        """
        url = (url or "").strip()  # 去首尾空白
        if not url.startswith("https://"):  # 必须 HTTPS 加密传输
            return False
        if cls.API_BASE not in url:  # 必须指向企业微信群机器人官方接口
            return False
        # 解析 query 参数,必须携带非空 key
        try:
            from urllib.parse import urlparse, parse_qs  # 标准库:URL 解析
            query = parse_qs(urlparse(url).query)  # 解析查询参数为字典
            return bool(query.get("key") and query["key"][0])  # key 存在且非空
        except Exception:  # 解析失败按非法处理
            return False

    @staticmethod
    def extract_key(url: str) -> str:
        """
        从 Webhook 地址中提取 key 鉴权参数(用于日志脱敏展示).

        :param url: 完整 Webhook 地址
        :type url: str
        :return: key 参数值;解析失败返回空串
        :rtype: str
        """
        try:
            from urllib.parse import urlparse, parse_qs  # 标准库:URL 解析
            query = parse_qs(urlparse(url).query)  # 解析查询参数
            return (query.get("key") or [""])[0]  # 返回 key 值,缺失返回空串
        except Exception:  # 解析异常统一返回空串
            return ""

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------
    def send_text(
        self,
        content: str,
        mentioned_list: Optional[List[str]] = None,
        mentioned_mobile_list: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """
        发送纯文本消息(最兼容,个人微信同步也能正常显示).

        :param content: 文本内容,最长 2048 字节
        :type content: str
        :param mentioned_list: 需要 @ 的成员 userid 列表,可省略
        :type mentioned_list: Optional[List[str]]
        :param mentioned_mobile_list: 需要 @ 的成员手机号列表,可省略
        :type mentioned_mobile_list: Optional[List[str]]
        :return: (是否成功, 描述信息)
        :rtype: Tuple[bool, str]
        """
        text_payload: Dict[str, Any] = {"content": content}  # 文本消息体
        if mentioned_list:  # 有 userid 列表才附加 @ 字段
            text_payload["mentioned_list"] = mentioned_list
        if mentioned_mobile_list:  # 有手机号列表才附加 @ 字段
            text_payload["mentioned_mobile_list"] = mentioned_mobile_list
        return self._post({"msgtype": "text", "text": text_payload})  # 统一发送

    def send_markdown(self, content: str) -> Tuple[bool, str]:
        """
        发送 Markdown 消息(仅企业微信内渲染,个人微信同步可能不支持).

        :param content: Markdown 内容,最长 4096 字节
        :type content: str
        :return: (是否成功, 描述信息)
        :rtype: Tuple[bool, str]
        """
        return self._post({"msgtype": "markdown", "markdown": {"content": content}})  # 统一发送

    def send_image(self, base64_data: str, md5_hex: str) -> Tuple[bool, str]:
        """
        发送图片消息(base64 与 md5 由上层生成).

        :param base64_data: 图片的 base64 编码字符串
        :type base64_data: str
        :param md5_hex: 图片二进制内容的 MD5 十六进制串
        :type md5_hex: str
        :return: (是否成功, 描述信息)
        :rtype: Tuple[bool, str]
        """
        return self._post({  # 组装图片消息 payload
            "msgtype": "image",  # 图片消息类型
            "image": {"base64": base64_data, "md5": md5_hex},  # 图片数据与校验
        })

    # ------------------------------------------------------------------
    # 内部发送实现
    # ------------------------------------------------------------------
    def _post(self, payload: Dict[str, Any]) -> Tuple[bool, str]:
        """
        统一 POST payload 到 Webhook,按企业微信 errcode==0 判定成功.

        :param payload: 企业微信约定的消息 payload 字典
        :type payload: Dict[str, Any]
        :return: (是否成功, 描述信息)
        :rtype: Tuple[bool, str]
        """
        if not self.webhook_url:  # 未配置 Webhook 地址
            return False, "未配置企业微信 Webhook 地址"  # 上层可据此跳过,不抛异常
        key_masked = self.extract_key(self.webhook_url)  # 提取 key 用于脱敏日志
        key_hint = f"key={key_masked[:6]}***" if key_masked else "无key参数"  # 脱敏展示
        _logger.info(f"📨 准备推送企业微信消息:{key_hint} | 类型={payload.get('msgtype', '?')}")
        try:  # 发送 POST 请求并解析响应
            resp = requests.post(  # 发起 HTTP POST
                self.webhook_url,  # 完整 Webhook 地址
                json=payload,  # JSON 序列化消息体
                timeout=self.timeout,  # 超时秒数
            )
            resp.raise_for_status()  # 非 2xx 直接抛异常进入 except
            body = resp.json()  # 解析 JSON 响应
        except requests.exceptions.Timeout:  # 请求超时
            _logger.error(f"❌ 企业微信推送超时(>{self.timeout}s):{key_hint}")
            return False, f"推送超时(>{self.timeout}s),请检查网络"
        except requests.exceptions.ConnectionError as e:  # 连接失败
            _logger.error(f"❌ 企业微信推送连接失败:{key_hint} | {type(e).__name__}")
            return False, f"网络连接失败:{type(e).__name__}"
        except requests.exceptions.RequestException as e:  # 其他请求异常
            _logger.error(f"❌ 企业微信推送请求异常:{key_hint} | {type(e).__name__}:{e}")
            return False, f"推送请求异常:{type(e).__name__}"
        except ValueError:  # 响应非 JSON
            _logger.error(f"❌ 企业微信推送响应解析失败:{key_hint} | 非JSON响应")
            return False, "推送响应解析失败(非JSON)"
        # 校验企业微信返回码:errcode==0 表示成功
        errcode = body.get("errcode")  # 企业微信返回码
        if errcode == 0:  # 成功
            _logger.info(f"✅ 企业微信推送成功:{key_hint}")
            return True, "推送成功"
        # 失败:记录 errcode 与 errmsg,便于上层提示具体原因
        errmsg = body.get("errmsg", "未知错误")  # 错误描述
        _logger.warning(f"⚠ 企业微信推送被拒绝:{key_hint} | errcode={errcode}, errmsg={errmsg}")
        return False, f"推送被拒绝(errCode={errcode}):{errmsg}"
