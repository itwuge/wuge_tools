#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: run_ba.py
# 归属: service 业务服务层 —— 签到网站(runba.cyou)业务 API 封装服务
# ------------------------------------------------------------------------------
# 文件用途:
#   签到网站(runba.cyou)业务 API 封装服务;调用底层
#   infrastructure.http_client.BaseHttpClient 完成登录页访问、密码登录、
#   Cookie 加载、用户中心获取、签到等业务流程;透传底层原始返回结果
#   (无 success 外壳),上层统一通过 "error" in resp 判断网络成败、
#   通过 resp.get("ret") == 1 判断业务成败。
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 service 业务服务层;对上(controller/worker)提供签到网站业务接口,
#   对下依赖 infrastructure.http_client.BaseHttpClient 完成实际 HTTP 调用;
#   本模块封装业务请求头、请求体、Cookie 持久化与登录态管理,不涉及 UI。
#
#   关联组件:
#     - 上游: controller/worker(签到业务)
#     - 下游: runtime.model.app_config.get_store_sub_paths、
#             runtime.model.account_store.safe_email_name、
#             infrastructure.http_client.BaseHttpClient
# ------------------------------------------------------------------------------
# 核心功能:
#   1. session_open/session_close: 管理底层 HTTP 会话生命周期(Cookie 持久化)
#   2. get_cookie_filename_by_email: 根据邮箱生成账号专属 cookie 文件路径
#   3. visit_login_page: 访问登录页获取初始会话 Cookie
#   4. login: 密码登录(邮箱+密码),成功后保存 Cookie 到本地
#   5. load_cookie_session: 加载本地 Cookie 恢复登录态
#   6. get_user_info: 获取用户中心页面(HTML),刷新 Cookie
#   7. checkin: 调用签到接口 POST /user/checkin
#   8. 代理配置支持显式 proxy_mapping 与旧版 use_proxy/proxy_port 双模式
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅封装签到网站业务 API,不涉及 UI 或持久化逻辑
#   - 透传底层原始返回结果,上层通过 "error" in resp / resp.get("ret") == 1 判断
#   - Cookie 文件读写在工作线程中执行,无并发保护(单实例串行调用)
# ------------------------------------------------------------------------------
# 线程模型:
#   网络 IO 阻塞,务必在 QThread 子线程调用,禁止在 UI 主线程直接调用;
#   每个实例持有独立 BaseHttpClient,不同实例之间完全独立;
#   Cookie 文件读写在工作线程中执行,无并发保护(单实例串行调用)。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: __future__(annotations)、logging、os、typing
#   - 第三方: requests、urllib3
#   - 项目内: runtime.model.app_config.get_store_sub_paths、
#             runtime.model.account_store.safe_email_name、
#             infrastructure.http_client.BaseHttpClient
# ==============================================================================
from __future__ import annotations  # PEP 563:注解延迟为字符串求值,兼容旧版本对内置泛型写法的解析

import logging  # 标准库:记录签到业务流程的 debug/info/warning/error 日志
import os  # 标准库:拼接 cookie 文件路径
from typing import Any, Dict, Optional, Union  # 标准库:类型注解

import requests  # 第三方:仅用于类型注解(requests.Response)
import urllib3  # 第三方:用于关闭 SSL 证书不安全警告

from runtime.model.app_config import get_store_sub_paths  # 项目内:获取数据存储子目录路径
from runtime.model.account_store import safe_email_name  # 项目内:邮箱安全文件名转换

# -*- coding: utf-8 -*-
from infrastructure.http_client import BaseHttpClient  # 项目内:底层通用 HTTP 客户端

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 全局抑制 verify=False 时的 InsecureRequestWarning

# 哨兵:区分 proxy_mapping "未传入(回退 use_proxy 旧逻辑)" 与 "显式 None(明确不代理)"
# 用专用类而非裸 object(),便于静态类型检查器通过 isinstance 收窄联合类型
class _ProxyMappingUnset:
    """proxy_mapping 未传入时的哨兵类型(单例,不参与运行时逻辑)"""
    pass  # 空类体:仅用作类型标识与单例哨兵


_PROXY_MAPPING_UNSET = _ProxyMappingUnset()  # 创建哨兵单例:proxy_mapping 参数缺省时的默认值

# proxy_mapping 允许的类型:代理字典 / None(明确不代理) / 哨兵(未传入,回退旧逻辑)
ProxyMappingArg = Union[Dict[str, str], None, _ProxyMappingUnset]  # 联合类型别名,供参数注解使用


class ServiceApiService:
    """
    签到网站业务 API 封装服务类【透传底层原始返回,无 success 外壳】

    底层依赖 BaseHttpClient,自动管理 Session、Cookie、代理、重试逻辑。

    ✅ 返回规则:直接返回 BaseHttpClient 原始结果
        1. 网络异常/解析失败:dict 包含 "error" 键
        2. 接口正常返回:服务器原始 JSON 字典,例如 {"ret":0,"msg":"邮箱不存在"} / {"ret":1,"msg":"登录成功"}

    UI 判断示例:
        resp = service.login("xxx@xx.com","123456")
        if "error" in resp:
            print("网络错误", resp["error"])
        elif resp.get("ret") == 1:
            print("业务成功", resp.get("msg"))
        else:
            print("业务失败", resp.get("msg"))

    ⚠️ 注意:
        1. 网络 IO 会阻塞线程!UI 程序务必放到 QThread 子线程执行,禁止直接 UI 主线程调用
        2. verify_ssl=False 关闭 SSL 证书校验,仅用于调试环境

    实例属性:
        base_url (str): 签到网站基础域名
        use_proxy (bool): 旧版代理开关(proxy_mapping 未传时生效)
        proxy_port (int): 旧版代理端口
        proxy_mapping (ProxyMappingArg): 显式代理字典(优先级最高)
        logger (logging.Logger): 日志对象
        is_logged_in (bool): 当前登录态标志
        cookie_file (Optional[str]): 账号专属 cookie 文件路径
        _client (Optional[BaseHttpClient]): 底层 HTTP 客户端实例
    """
    def __init__(
            self,
            base_url: str = "https://go.runba.cyou",
            use_proxy: bool = True,
            proxy_port: int = 38457,
            cookie_file: Optional[str] = None,
            proxy_mapping: ProxyMappingArg = _PROXY_MAPPING_UNSET,
    ):
        """
        初始化签到网站业务服务实例

        :param base_url: 签到网站基础域名,默认 "https://go.runba.cyou"
        :type base_url: str
        :param use_proxy: 旧版代理开关(proxy_mapping 未传时生效),默认 True
        :type use_proxy: bool
        :param proxy_port: 旧版代理端口(本机代理),默认 38457
        :type proxy_port: int
        :param cookie_file: 指定 cookie 文件路径;None 时由 get_cookie_filename_by_email 动态生成
        :type cookie_file: Optional[str]
        :param proxy_mapping: 显式代理字典(由配置统一解析 resolve_effective_proxy 得到);
                              传入后优先级最高——dict 走代理、None 明确不代理;
                              缺省(未传)时回退 use_proxy/proxy_port 旧逻辑
        :type proxy_mapping: ProxyMappingArg
        """
        self.base_url = base_url  # 保存签到网站基础域名
        self.use_proxy = use_proxy  # 保存旧版代理开关
        self.proxy_port = proxy_port  # 保存旧版代理端口
        self.proxy_mapping: ProxyMappingArg = proxy_mapping  # 保存显式代理配置(优先级最高)
        self.logger = logging.getLogger("ServiceApiService")  # 创建模块专属 logger
        self.is_logged_in = False  # 初始化登录态为未登录
        self.cookie_file = cookie_file  # 保存 cookie 文件路径(可能为 None)
        self._client: Optional[BaseHttpClient] = None  # 底层客户端初始为 None,由 session_open 创建

    @staticmethod
    def get_cookie_filename_by_email(email: str) -> str:
        """
        根据邮箱生成账号专属 cookie 完整磁盘路径,存放至 data_store/cookies

        :param email: 用户邮箱地址
        :type email: str
        :return: 形如 "data_store/cookies/cookie_<安全邮箱名>.json" 的完整路径
        :rtype: str
        """
        paths = get_store_sub_paths()  # 获取数据存储各子目录路径字典
        safe_name = safe_email_name(email)  # 将邮箱转换为磁盘安全文件名(替换特殊字符)
        return os.path.join(paths["COOKIE_DIR"], f"cookie_{safe_name}.json")  # 拼接 cookie 文件完整路径

    def _get_default_proxy(self) -> Dict[str, str]:
        """
        旧版代理模式:基于 127.0.0.1 + proxy_port 组装 HTTP/HTTPS 代理字典

        :return: {"http": "http://127.0.0.1:port", "https": "http://127.0.0.1:port"}
        :rtype: Dict[str, str]
        """
        return {  # 返回 http/https 同指向本机代理端口的字典
            "http": f"http://127.0.0.1:{self.proxy_port}",  # HTTP 代理指向本机端口
            "https": f"http://127.0.0.1:{self.proxy_port}"  # HTTPS 代理同样指向本机端口
        }

    def _create_client(self) -> BaseHttpClient:
        """
        创建底层 BaseHttpClient 实例,根据 proxy_mapping / use_proxy 决定代理配置

        :return: 配置好代理与请求头的 BaseHttpClient 实例
        :rtype: BaseHttpClient
        """
        proxies: Dict[str, str]  # 声明最终生效的代理字典类型
        if isinstance(self.proxy_mapping, _ProxyMappingUnset):  # proxy_mapping 未传入:走旧版逻辑
            # 兼容旧调用方:未传 proxy_mapping 时按 use_proxy + proxy_port
            proxies = self._get_default_proxy() if self.use_proxy else {}  # 开关开则用本机代理,否则空字典
        else:  # proxy_mapping 已传入(dict 或 None):优先级最高
            # 统一解析路径:dict 走代理 / None 明确不代理
            proxies = self.proxy_mapping or {}  # dict 原样使用,None 归一为空字典
        client = BaseHttpClient(  # 创建底层 HTTP 客户端
            base_url=self.base_url,  # 传入基础域名
            timeout=20,  # 业务请求超时 20 秒
            max_retries=2,  # 最多重试 2 次
            retry_sleep=5.0,  # 每次重试前休眠 5 秒
            verify_ssl=False,  # 关闭 SSL 证书校验(调试环境)
            proxies=proxies,  # 传入最终代理配置
            headers={  # 浏览器模拟请求头,降低被风控概率
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",  # Chrome 桌面 UA
                "accept-language": "zh-CN,zh;q=0.9",  # 首选中文
                "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',  # 浏览器品牌提示头
                "sec-ch-ua-mobile": "?0",  # 非移动设备
                "sec-ch-ua-platform": '"Windows"',  # Windows 平台
            },
            logger=self.logger  # 传入日志器
        )
        return client  # 返回配置完成的客户端实例

    def set_proxy_enable(self, enable: bool, port: Optional[int] = None):
        """
        运行时切换旧版代理开关与端口,并同步到底层客户端(若已创建)

        :param enable: 是否启用代理(True/False)
        :type enable: bool
        :param port: 代理端口;None 表示不修改现有端口
        :type port: Optional[int]
        """
        if port is not None:  # 传入了新端口
            self.proxy_port = port  # 更新实例端口属性
        self.use_proxy = enable  # 更新代理开关
        if self._client:  # 底层客户端已创建时同步更新
            if enable:  # 启用代理
                self._client.set_proxy(self._get_default_proxy())  # 注入本机代理字典
            else:  # 关闭代理
                self._client.set_proxy(None)  # 清空代理配置

    def session_open(self):
        """打开底层 HTTP 会话,创建 BaseHttpClient 并进入 with 上下文(复用 Cookie)"""
        if self._client is not None:  # 已有会话,先关闭再重建
            self.session_close()  # 关闭旧会话释放资源
        # 仅用于日志展示的代理快照(与 _create_client 内的选择逻辑保持一致,不额外建 client)
        if isinstance(self.proxy_mapping, _ProxyMappingUnset):  # 未传显式代理:用旧版逻辑算快照
            proxy_snapshot = self._get_default_proxy() if self.use_proxy else {}  # 开关开则本机代理,否则空
        else:  # 已传显式代理
            proxy_snapshot = self.proxy_mapping or {}  # dict 原样,None 为空
        self.logger.info(  # 记录会话打开日志
            f"🌐 打开网络会话: base_url={self.base_url}, "  # 基础域名
            f"代理={proxy_snapshot if proxy_snapshot else '直连(无代理)'}"  # 代理配置或直连
        )
        self._client = self._create_client()  # 创建底层客户端
        self._client.__enter__()  # 手动进入 with 上下文,激活会话

    def session_close(self):
        """关闭底层 HTTP 会话,退出 with 上下文并释放 Cookie/连接池"""
        if self._client is not None:  # 会话存在才关闭
            self.logger.info("🧹 关闭网络会话,释放Cookie连接池")  # 记录会话关闭日志
            self._client.__exit__(None, None, None)  # 手动退出 with 上下文,释放资源
            self._client = None  # 置空客户端引用
        self.is_logged_in = False  # 重置登录态标志

    def visit_login_page(self) -> Union[Dict[str, Any], requests.Response]:
        """
        访问登录页 GET /auth/login,获取初始会话 Cookie

        :return: skip_parse=True 返回原始 requests.Response;网络失败返回含 error 的 dict
        :rtype: Union[Dict[str, Any], requests.Response]
        """
        if self._client is None:  # 会话未打开
            return {"error": "会话未打开,请先调用session_open()"}  # 返回错误提示
        page_login_get_headers = {  # 登录页 GET 请求头(模拟浏览器导航)
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',  # 浏览器品牌提示
            "sec-ch-ua-mobile": "?0",  # 非移动
            "sec-ch-ua-platform": "Windows",  # Windows 平台
            "upgrade-insecure-requests": "1",  # 允许 HTTPS 升级
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",  # Chrome UA
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",  # 接受 HTML 等
            "sec-fetch-site": "same-origin",  # 同源导航
            "sec-fetch-mode": "navigate",  # 导航请求
            "sec-fetch-user": "?1",  # 用户激活
            "sec-fetch-dest": "document",  # 目标为文档
            "referer": f"{self.base_url}/",  # 来源页为首页
            "accept-language": "zh-CN,zh;q=0.9",  # 首选中文
            "priority": "u=0, i"  # 网络优先级
        }
        self.logger.debug("🔗 GET 登录页 /auth/login (获取会话Cookie)")  # 记录访问登录页日志
        resp_page = self._client.request("GET", "/auth/login", headers=page_login_get_headers, skip_parse=True)  # 发起登录页请求(返回原始 Response)
        if isinstance(resp_page, dict) and "error" in resp_page:  # 网络失败分支
            self.logger.error(f"❌ 访问登录页失败:{resp_page.get('error')}")  # 记录错误详情
        else:  # 成功分支
            self.logger.debug(f"🔗 登录页响应 HTTP {getattr(resp_page, 'status_code', '?')}")  # 记录响应状态码
        return resp_page  # 返回原始 Response 或错误字典

    def login(self, email: str, password: str) -> Dict[str, Any]:
        """
        密码登录:先访问登录页获取 Cookie,再 POST /auth/login 提交邮箱密码

        :param email: 登录邮箱
        :type email: str
        :param password: 登录密码
        :type password: str
        :return: 服务器原始 JSON 字典;网络失败含 error 键,业务成功 ret=1
        :rtype: Dict[str, Any]
        """
        if self._client is None:  # 会话未打开
            return {"error": "会话未打开,请先调用session_open()"}  # 返回错误提示
        visit_ret = self.visit_login_page()  # 先访问登录页建立会话 Cookie
        if isinstance(visit_ret, dict) and "error" in visit_ret:  # 访问登录页失败
            return visit_ret  # 直接返回错误,不继续登录
        login_post_headers = {  # 登录 POST 请求头(模拟 AJAX)
            "sec-ch-ua-platform": "Windows",  # Windows 平台
            "x-requested-with": "XMLHttpRequest",  # 标记为 AJAX 请求
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",  # Chrome UA
            "accept": "application/json, text/javascript, */*; q=0.01",  # 接受 JSON
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',  # 浏览器品牌提示
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",  # 表单编码
            "sec-ch-ua-mobile": "?0",  # 非移动
            "origin": self.base_url,  # 来源域名
            "sec-fetch-site": "same-origin",  # 同源
            "sec-fetch-mode": "cors",  # CORS 模式
            "sec-fetch-dest": "empty",  # 无目标类型(AJAX)
            "referer": f"{self.base_url}/auth/login",  # 来源为登录页
            "accept-language": "zh-CN,zh;q=0.9",  # 首选中文
            "priority": "u=1, i"  # 网络优先级
        }
        login_data = {  # 登录表单数据
            "agree": 1,  # 同意协议(固定 1)
            "email": email,  # 邮箱
            "passwd": password,  # 密码
            "code": ""  # 验证码(留空)
        }
        # 安全要求:日志只记录账号,禁止打印密码明文
        self.logger.info(f"🔑 提交密码登录:账号={email}")  # 记录登录动作(仅账号)
        resp_login = self._client.post("/auth/login", data=login_data, headers=login_post_headers)  # 发起登录 POST
        if "error" not in resp_login and resp_login.get("ret") == 1:  # 网络成功且业务返回成功
            self.is_logged_in = True  # 标记已登录
            self.cookie_file = self.get_cookie_filename_by_email(email)  # 生成账号专属 cookie 路径
            self._client.dump_cookies(self.cookie_file)  # 保存 Cookie 到本地
            self.logger.info(f"✅ 登录成功,会话Cookie已保存:{self.cookie_file}")  # 记录登录成功
        else:  # 网络失败或业务返回非成功
            self.is_logged_in = False  # 标记未登录
            if "error" in resp_login:  # 网络层失败
                self.logger.error(f"❌ 登录网络异常:{resp_login.get('error')}")  # 记录网络错误
            else:  # 业务层失败(ret != 1)
                self.logger.warning(f"⚠ 登录业务返回 ret={resp_login.get('ret')},msg={resp_login.get('msg')}")  # 记录业务失败原因
        return resp_login  # 返回服务器原始响应

    def load_cookie_session(self) -> Dict[str, Any]:
        """
        从本地 cookie 文件加载会话,恢复登录态

        :return: {"ret": 1, "msg": "Cookie加载成功"} 或 {"error": "..."}
        :rtype: Dict[str, Any]
        """
        if self._client is None:  # 会话未打开
            return {"error": "会话未打开,请先调用session_open()"}  # 返回错误提示
        if self.cookie_file is None:  # cookie 文件路径未设置
            return {"error": "cookie_file未设置,无法加载Cookie"}  # 返回错误提示
        ok = self._client.load_cookies(self.cookie_file)  # 从本地文件加载 Cookie 到底层会话
        if ok:  # 加载成功
            self.is_logged_in = True  # 标记已登录
            self._client.dump_cookies(self.cookie_file)  # 回写 Cookie(刷新过期时间等)
            self.logger.info(f"🍪 Cookie加载成功:{self.cookie_file}")  # 记录加载成功
            return {"ret": 1, "msg": "Cookie加载成功"}  # 返回统一成功结构
        else:  # 加载失败(文件不存在或解析失败)
            self.is_logged_in = False  # 标记未登录
            self.logger.info(f"🍪 Cookie不可用(文件不存在或解析失败):{self.cookie_file}")  # 记录不可用
            return {"error": "Cookie文件不存在或解析失败"}  # 返回统一错误结构

    def get_user_info(self) -> Union[Dict[str, Any], requests.Response]:
        """
        获取用户中心页面 GET /user(HTML),刷新会话 Cookie

        :return: 原始 requests.Response;网络失败返回含 error 的 dict
        :rtype: Union[Dict[str, Any], requests.Response]
        """
        if self._client is None:  # 会话未打开
            return {"error": "会话未打开,请先调用session_open()"}  # 返回错误提示
        if not self.is_logged_in:  # 未登录
            return {"error": "未登录,请先登录或加载Cookie"}  # 返回错误提示
        if self.cookie_file is None:  # cookie 文件路径未设置
            return {"error": "cookie_file未设置"}  # 返回错误提示
        user_page_headers = {  # 用户中心 GET 请求头(模拟浏览器导航)
            "cache-control": "max-age=0",  # 不使用缓存
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',  # 浏览器品牌提示
            "sec-ch-ua-mobile": "?0",  # 非移动
            "sec-ch-ua-platform": "Windows",  # Windows 平台
            "upgrade-insecure-requests": "1",  # 允许 HTTPS 升级
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",  # Chrome UA
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",  # 接受 HTML
            "sec-fetch-site": "same-origin",  # 同源
            "sec-fetch-mode": "navigate",  # 导航
            "sec-fetch-user": "?1",  # 用户激活
            "sec-fetch-dest": "document",  # 文档目标
            "referer": f"{self.base_url}/auth/login",  # 来源为登录页
            "accept-encoding": "gzip, deflate",  # 接受压缩编码
            "accept-language": "zh-CN,zh;q=0.9",  # 首选中文
            "priority": "u=0, i"  # 网络优先级
        }
        self.logger.debug("👤 GET 用户中心 /user")  # 记录访问用户中心
        resp_raw = self._client.request("GET", "/user", headers=user_page_headers, skip_parse=True)  # 发起用户中心请求(返回原始 Response)
        if not (isinstance(resp_raw, dict) and "error" in resp_raw):  # 请求成功
            self._client.dump_cookies(self.cookie_file)  # 刷新保存 Cookie
            self.logger.debug(f"👤 用户中心响应 HTTP {getattr(resp_raw, 'status_code', '?')},Cookie已刷新")  # 记录成功
        else:  # 网络失败
            self.logger.error(f"❌ 获取用户信息网络异常:{resp_raw.get('error')}")  # 记录错误
        return resp_raw  # 返回原始 Response 或错误字典

    def checkin(self) -> Dict[str, Any]:
        """
        签到:先获取用户中心刷新 Cookie,再 POST /user/checkin 调用签到接口

        :return: 服务器原始 JSON 字典;网络失败含 error 键,签到成功 ret=1
        :rtype: Dict[str, Any]
        """
        if self._client is None:  # 会话未打开
            return {"error": "会话未打开,请先调用session_open()"}  # 返回错误提示
        if not self.is_logged_in:  # 未登录
            return {"error": "未登录,请先登录或加载Cookie"}  # 返回错误提示
        if self.cookie_file is None:  # cookie 文件路径未设置
            return {"error": "cookie_file未设置"}  # 返回错误提示
        fetch_ret = self.get_user_info()  # 先获取用户中心刷新 Cookie
        if isinstance(fetch_ret, dict) and "error" in fetch_ret:  # 获取用户中心失败
            return fetch_ret  # 直接返回错误,不继续签到
        checkin_headers = {  # 签到 POST 请求头(模拟 AJAX)
            "sec-ch-ua-platform": "Windows",  # Windows 平台
            "x-requested-with": "XMLHttpRequest",  # AJAX 标记
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",  # Chrome UA
            "accept": "application/json, text/javascript, */*; q=0.01",  # 接受 JSON
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',  # 浏览器品牌提示
            "sec-ch-ua-mobile": "?0",  # 非移动
            "origin": self.base_url,  # 来源域名
            "sec-fetch-site": "same-origin",  # 同源
            "sec-fetch-mode": "cors",  # CORS 模式
            "sec-fetch-dest": "empty",  # AJAX 目标
            "referer": f"{self.base_url}/user",  # 来源为用户中心
            "accept-encoding": "gzip, deflate, br, zstd",  # 接受压缩编码
            "accept-language": "zh-CN,zh;q=0.9",  # 首选中文
            "priority": "u=1, i"  # 网络优先级
        }
        self.logger.info("📝 调用签到接口 POST /user/checkin")  # 记录签到动作
        resp_check = self._client.post("/user/checkin", headers=checkin_headers)  # 发起签到 POST
        if "error" not in resp_check:  # 网络成功
            self._client.dump_cookies(self.cookie_file)  # 刷新保存 Cookie
            if resp_check.get("ret") == 1:  # 业务返回签到成功
                self.logger.info(f"✅ 签到接口返回成功:{resp_check.get('msg')}")  # 记录签到成功
            else:  # 业务返回非成功(如已签到)
                self.logger.info(f"ℹ️ 签到接口业务返回 ret={resp_check.get('ret')},msg={resp_check.get('msg')}")  # 记录业务返回
        else:  # 网络失败
            self.logger.error(f"❌ 签到接口网络异常:{resp_check.get('error')}")  # 记录网络错误
        return resp_check  # 返回服务器原始响应


# 模块公开 API 符号表
__all__ = ["ServiceApiService"]  # 仅导出签到网站 API 业务服务类

if __name__ == "__main__":  # 直接运行本文件时执行模块自测
    import logging  # 局部导入 logging 配置
    logging.basicConfig(level=logging.DEBUG)  # 配置 DEBUG 级别日志输出到控制台
    service = ServiceApiService(use_proxy=True, proxy_port=38457)  # 构造服务实例(启用本机代理)
    service.session_open()  # 打开网络会话
    res = service.login("itwuge@gmail.com", "a6230341")  # 执行密码登录
    print("原始返回:", res)  # 打印登录原始返回
    if "error" in res:  # 网络错误分支
        print(f"网络错误:{res['error']}")  # 打印网络错误
    elif res.get("ret") == 1:  # 登录成功分支
        print(f"登录成功:{res.get('msg')}")  # 打印成功消息
        check_res = service.checkin()  # 执行签到
        print("签到原始返回:", check_res)  # 打印签到返回
    else:  # 业务失败分支
        print(f"业务失败:{res.get('msg')}")  # 打印业务失败消息
    service.session_close()  # 关闭网络会话释放资源
