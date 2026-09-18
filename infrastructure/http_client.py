#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: http_client.py
# 归属: infrastructure 基础设施层 —— 通用底层 HTTP 客户端
# ------------------------------------------------------------------------------
# 文件用途:
#   通用底层 HTTP 客户端,基于 requests.Session 封装,仅提供 HTTP 协议原语能力,
#   不含任何业务逻辑;负责会话 Cookie 与连接池管理、请求头合并、请求体透传、
#   代理、重试、拦截器链、Cookie 持久化以及 multipart 上传.
# ------------------------------------------------------------------------------
# 架构定位:
#   位于 infrastructure 基础设施层最底层;依赖方向只向下(标准库/第三方库),
#   不反向依赖任何业务层;被上层 service/api 等业务模块统一调用,业务层约定
#   通过 if "error" in resp 判断请求成败.
#
#   关联组件:
#     - 上游: service 业务层(github_update/run_ba 等)
#     - 下游: requests.Session、urllib3
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 会话 Cookie、HTTP 连接池管理,支持 with 上下文自动释放资源
#   2. 请求头合并规则:实例全局 default_headers → 单次请求传入 headers
#   3. 透传 json / form-urlencoded / multipart/form-data 请求
#   4. 支持全局/单次请求覆盖代理配置
#   5. 重试机制:仅 Timeout、ConnectionError、5xx 服务端错误触发重试
#   6. 请求拦截器、响应拦截器链支持
#   7. Cookie 的 json 文件导出与加载
#   8. 保留 multipart/form-data upload 文件上传(HTTP 原生协议能力)
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅提供 HTTP 协议原语能力,不含任何业务逻辑
#   - 已移除 download 下载方法,下载能力迁移至 file_transfer
#   - 成功返回服务端原始 JSON 字典,失败返回包含 error 键的字典
# ------------------------------------------------------------------------------
# 线程模型:
#   复用 requests.Session(连接池与 Cookie);支持 http/https 代理,只要存在非空
#   代理即强制关闭 SSL 证书校验;默认 timeout=15s;verify_ssl 默认 False;
#   已全局抑制 urllib3 的 InsecureRequestWarning.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: json、os、time、typing
#   - 第三方: requests、urllib3
#   - 项目内: 无
# ==============================================================================
import json  # 标准库:cookie文件读写、响应片段二次JSON解析
import os  # 标准库:判断cookie文件是否存在
import time  # 标准库:重试休眠与请求耗时计时
from typing import Any, Callable, Dict, List, Literal, Optional, Union, overload  # 标准库:类型注解与@overload重载声明

import requests  # 第三方:HTTP请求库,提供Session连接池能力
import urllib3  # 第三方:用于关闭底层不安全SSL警告
from requests.cookies import RequestsCookieJar  # 第三方:session cookie容器,仅用于类型注解

# 走代理时统一关闭SSL证书验证(见下方_effective_verify),抑制对应的InsecureRequestWarning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 全局抑制urllib3证书校验关闭警告


class BaseHttpClient:
    """
    高性能可复用HTTP会话客户端
    自动维护Cookie、连接池,with上下文自动释放TCP资源;支持拦截器、重试、代理、Cookie持久化

    工作流程:
        1. 调用 get/post/put/delete/patch 高层方法 → 委托 request() 统一入口
        2. request() 拼接完整 URL、合并请求头、计算代理与证书联动策略
        3. 执行请求拦截器链 → 任一拦截器返回 error 字典则中止请求不发网络
        4. 进入 while 循环发起请求 → 成功则解析响应并执行响应拦截器链后返回
        5. 失败则判断是否可重试(Timeout/ConnectionError/5xx) → 可重试则 sleep 后继续循环
        6. 不可重试或达到重试上限 → 返回带 error 键的错误字典
        7. 调用方统一通过 "error" in resp 判断请求成败

    线程安全说明:
        - 实例内部持有单个 requests.Session,Session 本身线程不安全
        - 多线程环境下建议每个线程持有独立的 BaseHttpClient 实例,避免共享 session
        - 拦截器链在请求线程中同步执行,拦截器回调需自行保证线程安全
        - Cookie 持久化(dump_cookies/load_cookies)应在单线程中操作,避免并发读写

    实例属性(全部可在 __init__ 中配置):
        base_url (str): API 基础域名,如 "https://api.example.com";endpoint 为完整 URL 时忽略
        timeout (int): 默认请求超时秒数,默认 15 秒
        proxies (Dict[str, str]): 全局代理配置字典,键为 http/https,值为代理 URL;空字典表示无代理
        verify_ssl (bool): 是否校验 HTTPS SSL 证书,默认 False;走代理时被强制关闭
        max_retries (int): 最大重试次数,0 表示关闭重试;仅 Timeout、ConnectionError、5xx 触发重试
        retry_sleep (float): 每次重试前休眠秒数,默认 15 秒
        logger (Any): 外部日志对象,为 None 则不输出任何内部日志
        session (requests.Session): requests 底层会话实例,维护连接池与 Cookie 状态
        default_headers (Dict[str, str]): 实例全局请求头,该实例全部请求生效
        req_interceptors (List[Callable]): 请求拦截器回调列表,按注册顺序执行
        resp_interceptors (List[Callable]): 响应拦截器回调列表,按注册顺序执行
        retry_exceptions (tuple): 可触发重试的异常类型元组(Timeout, ConnectionError)

    :param base_url: API基础域名,例 "https://go.runba.cyou";传入完整url时可忽略
    :type base_url: str
    :param timeout: 默认请求超时,单位秒
    :type timeout: int
    :param headers: 实例全局请求头字典,该实例全部请求生效
    :type headers: Optional[Dict[str, str]]
    :param proxies: 全局代理字典 {"http":"xxx","https":"xxx"};传None清空代理
    :type proxies: Optional[Dict[str, str]]
    :param verify_ssl: 是否校验SSL证书,False关闭校验,会产生urllib3警告
    :type verify_ssl: bool
    :param max_retries: 最大重试次数,0关闭重试;仅Timeout、ConnectionError、5xx触发重试
    :type max_retries: int
    :param retry_sleep: 重试之前等待休眠秒数
    :type retry_sleep: float
    :param logger: 外部日志实例,None不打印内部日志
    :type logger: Any
    :return: 无返回值(构造完成得到客户端实例)
    :rtype: None
    :raises Exception: requests.Session()创建失败等底层异常会直接向上抛出
    """
    def __init__(
        self,
        base_url: str = "",
        timeout: int = 15,
        headers: Optional[Dict[str, str]] = None,
        proxies: Optional[Dict[str, str]] = None,
        verify_ssl: bool = False,
        max_retries: int = 0,
        retry_sleep: float = 15.0,
        logger=None
    ):
        """
        创建BaseHttpClient实例

        :param base_url: API基础域名,例 "https://go.runba.cyou";传入完整url时可忽略
        :type base_url: str
        :param timeout: 默认请求超时,单位秒
        :type timeout: int
        :param headers: 实例全局请求头字典,该实例全部请求生效
        :type headers: Optional[Dict[str, str]]
        :param proxies: 全局代理字典 {"http":"xxx","https":"xxx"};传None清空代理
        :type proxies: Optional[Dict[str, str]]
        :param verify_ssl: 是否校验SSL证书,False关闭校验,会产生urllib3警告
        :type verify_ssl: bool
        :param max_retries: 最大重试次数,0关闭重试;仅Timeout、ConnectionError、5xx触发重试
        :type max_retries: int
        :param retry_sleep: 重试之前等待休眠秒数
        :type retry_sleep: float
        :param logger: 外部日志实例,None不打印内部日志
        :type logger: Any
        :return: 无返回值
        :rtype: None
        :raises Exception: requests.Session()创建失败等底层异常会直接向上抛出
        """
        self.base_url: str = base_url  # 保存API基础域名
        self.timeout: int = timeout  # 保存默认请求超时秒数
        self.proxies: Dict[str, str] = proxies if proxies is not None else {}  # 全局代理,None归一为空字典
        # 保留调用方的证书校验意图;实际请求时若存在代理由_effective_verify强制关闭
        self.verify_ssl: bool = verify_ssl  # 保存SSL校验意图(有代理时实际请求会被联动关闭)
        self.max_retries: int = max_retries  # 保存最大重试次数,0表示不重试
        self.retry_sleep: float = retry_sleep  # 保存重试前休眠秒数
        self.logger = logger  # 注入外部日志器,None则不输出内部日志
        self.session: requests.Session = requests.Session()  # 创建底层会话(连接池+Cookie自动维护)
        self.default_headers: Dict[str, str] = headers.copy() if headers else {}  # 复制外部头,避免共享可变字典
        self.req_interceptors: List[Callable] = []  # 初始化请求拦截器回调链
        self.resp_interceptors: List[Callable] = []  # 初始化响应拦截器回调链
        self.retry_exceptions = (  # 定义可触发重试的异常类型元组
            requests.exceptions.Timeout,  # 请求超时异常
            requests.exceptions.ConnectionError  # 连接失败异常
        )

    def set_headers(self, headers: Dict[str, str]) -> None:
        """
        更新实例全局请求头,合并覆盖原有header

        :param headers: 需要合并更新的头字典,示例 {"Authorization":"Bearer xxx"}
        :type headers: Dict[str, str]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        self.default_headers.update(headers)  # 以update方式合并/覆盖全局请求头

    def set_proxy(self, proxies: Optional[Dict[str, str]]) -> None:
        """
        设置全局代理,传入None清空代理配置

        联动规则:存在非空代理时请求自动关闭SSL证书校验(见request),此处不改verify_ssl意图

        :param proxies: 代理字典 {"http":"xxx","https":"xxx"} 或者 None
        :type proxies: Optional[Dict[str, str]]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        if proxies is None:  # 传入None表示清空代理
            self.proxies = {}  # 重置为空字典(不走代理)
        else:  # 传入有效代理字典
            self.proxies = proxies  # 覆盖全局代理配置

    @staticmethod
    def _effective_verify(verify_ssl: bool, proxies: Dict[str, str]) -> bool:
        """
        私有静态工具:代理与证书联动策略

        :param verify_ssl: 调用方期望的SSL证书校验开关
        :type verify_ssl: bool
        :param proxies: 当前实际生效的代理字典
        :type proxies: Dict[str, str]
        :return: 实际应传给requests的verify值
        :rtype: bool
        :raises: 无
        """
        return False if proxies else verify_ssl  # 代理非空强制False;无代理沿用原值

    def add_req_interceptor(self, func: Callable[[Dict], Any]) -> None:
        """
        注册请求拦截器,网络发送前执行
        回调返回带error的字典会直接终止请求,不发出网络;否则返回修改后的请求参数字典

        :param func: 回调函数,入参完整请求kwargs字典
        :type func: Callable[[Dict], Any]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        self.req_interceptors.append(func)  # 追加到请求拦截器链尾部

    def add_resp_interceptor(self, func: Callable[[Dict], Dict]) -> None:
        """
        注册响应拦截器,拿到解析完成后的结果字典执行

        :param func: 回调函数,入参解析后响应字典,必须返回处理完成字典
        :type func: Callable[[Dict], Dict]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        self.resp_interceptors.append(func)  # 追加到响应拦截器链尾部

    def close(self) -> None:
        """
        手动关闭session会话,释放urllib3连接池与TCP资源;优先使用with上下文

        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        if hasattr(self, "session") and self.session is not None:  # 防御:session存在且未释放才关闭
            self.session.close()  # 关闭底层会话,归还连接池资源

    def dump_cookies(self, save_file_path: str) -> None:
        """
        将当前session全部cookie导出保存到本地json文件;必须在with内部、close之前调用

        :param save_file_path: 输出json文件完整路径
        :type save_file_path: str
        :return: 无返回值
        :rtype: None
        :raises OSError: 文件路径不可写时抛出IO相关异常
        """
        jar: RequestsCookieJar = self.session.cookies  # 取出session当前cookie容器
        cookie_list = []  # 准备可json序列化的普通字典列表
        for cookie in jar:  # 遍历每一条cookie对象
            http_only: Optional[str] = getattr(cookie, "_rest", {}).get("HttpOnly")  # 安全读取_rest扩展中的HttpOnly标记
            cookie_list.append({  # 将cookie对象转为普通字典
                "name": cookie.name,  # cookie名称
                "value": cookie.value,  # cookie值
                "domain": cookie.domain,  # 所属域名
                "path": cookie.path,  # 所属路径
                "expires": cookie.expires,  # 过期时间戳
                "secure": cookie.secure,  # 是否仅HTTPS传输
                "rest": {  # 扩展字段容器
                    "HttpOnly": http_only  # 保留HttpOnly标记
                }
            })
        with open(save_file_path, "w", encoding="utf-8") as f:  # 以utf-8打开输出文件
            json.dump(cookie_list, f, ensure_ascii=False, indent=2)  # 中文不转义、缩进2格写入json

    def load_cookies(self, load_file_path: str) -> bool:
        """
        从本地json文件加载cookie到当前session

        :param load_file_path: cookie json文件路径
        :type load_file_path: str
        :return: True加载成功;False文件不存在/解析异常
        :rtype: bool
        :raises: 无(内部已捕获全部异常并返回False)
        """
        if not os.path.exists(load_file_path):  # 文件不存在无需尝试
            return False  # 直接返回加载失败
        try:  # 捕获IO/JSON格式/字段缺失等异常
            with open(load_file_path, "r", encoding="utf-8") as f:  # 以utf-8打开cookie文件
                cookie_list = json.load(f)  # 反序列化为cookie字典列表
            jar: RequestsCookieJar = self.session.cookies  # 取session cookie容器
            for item in cookie_list:  # 逐条写回cookie jar
                jar.set(  # 调用CookieJar.set写入单条cookie
                    name=item["name"],  # cookie名
                    value=item["value"],  # cookie值
                    domain=item["domain"],  # 域
                    path=item["path"],  # 路径
                    expires=item.get("expires"),  # 过期时间(可能缺失为None)
                    secure=item.get("secure", False),  # secure标记缺失默认False
                )
            return True  # 全部写入成功
        except Exception:  # 任何解析/IO异常都静默处理
            return False  # 返回加载失败

    def __enter__(self):
        """
        with上下文管理器入口

        :return: 返回客户端自身,供 as 变量使用
        :rtype: BaseHttpClient
        :raises: 无
        """
        return self  # 返回自身实例

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        with退出钩子,无论是否异常自动调用close释放资源

        :param exc_type: 异常类型(with内无异常时为None)
        :type exc_type: Optional[type]
        :param exc_val: 异常实例(with内无异常时为None)
        :type exc_val: Optional[BaseException]
        :param exc_tb: 异常回溯堆栈(with内无异常时为None)
        :type exc_tb: Optional[Any]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        self.close()  # 退出时无条件关闭会话释放资源

    def _build_url(self, endpoint: str) -> str:
        """
        私有方法:拼接base_url与endpoint路径
        如果传入是完整http链接直接原样返回

        :param endpoint: 相对路径或者完整http url
        :type endpoint: str
        :return: 拼接完成的完整请求url
        :rtype: str
        :raises: 无
        """
        if endpoint.lower().startswith("http"):  # 小写后判断是否已是完整http(s)链接
            return endpoint  # 完整链接原样返回,不再拼接
        base = self.base_url.rstrip("/")  # 去掉base_url尾部斜杠,避免双斜杠
        ep = endpoint.lstrip("/")  # 去掉endpoint头部斜杠
        return f"{base}/{ep}"  # 用单个斜杠拼接成完整URL

    def _parse_response(self, resp: requests.Response) -> Dict[str, Any]:
        """
        私有方法:解析response对象
        JSON解析失败尝试截取{}片段二次解析;全部失败返回携带raw_text的结果字典

        :param resp: requests Response响应对象
        :type resp: requests.Response
        :return: 解析后的结果字典
        :rtype: Dict[str, Any]
        :raises: 无(解析失败已转为错误字典)
        """
        try:  # 优先走标准JSON解析
            return resp.json()  # 直接返回requests解析出的JSON对象
        except (ValueError, requests.exceptions.JSONDecodeError):  # 响应体不是合法JSON时兜底
            raw_bytes = resp.content  # 取原始字节内容
            idx_start = raw_bytes.find(b"{")  # 定位第一个左花括号
            idx_end = raw_bytes.rfind(b"}")  # 定位最后一个右花括号
            json_candidate = None  # 二次解析结果占位
            if idx_start != -1 and idx_end != -1 and idx_end > idx_start:  # 花括号区间合法才尝试截取
                json_bytes = raw_bytes[idx_start: idx_end + 1]  # 截取疑似JSON片段(含末尾})
                try:  # 尝试解码并解析截取片段
                    text = json_bytes.decode("utf-8")  # 字节按utf-8解码为文本
                    json_candidate = json.loads(text)  # 二次JSON解析
                except (json.JSONDecodeError, UnicodeDecodeError):  # 片段仍非法或编码错误
                    pass  # 放弃二次解析,保持None
            if json_candidate is not None:  # 二次解析成功
                return json_candidate  # 返回提取出的JSON对象
            else:  # 所有解析手段均失败
                return {  # 返回标准化错误结构,业务层通过error键识别
                    "_parse_ok": False,  # 标记解析失败
                    "error": "response not valid json",  # 错误描述
                    "status_code": resp.status_code,  # HTTP状态码
                    "raw_text": resp.text,  # 原始响应文本
                    "raw_bytes": resp.content  # 原始响应字节
                }

    def _run_req_interceptors(self, req_kwargs: Dict[str, Any]) -> Union[Dict[str, Any], Dict]:
        """
        私有方法:顺序执行请求拦截器链

        :param req_kwargs: 即将发给requests的完整请求参数字典
        :type req_kwargs: Dict[str, Any]
        :return: 加工后的请求参数;某拦截器返回带error字典时提前返回该错误字典
        :rtype: Union[Dict[str, Any], Dict]
        :raises: 无(拦截器回调自身异常不在此捕获)
        """
        for inter_func in self.req_interceptors:  # 按注册顺序遍历请求拦截器
            ret = inter_func(req_kwargs)  # 执行拦截回调并接收返回值
            if isinstance(ret, dict) and "error" in ret:  # 回调返回错误字典则中止链路
                return ret  # 直接返回错误,上层将放弃发请求
            req_kwargs = ret  # 否则用返回值作为最新请求参数继续向后传递
        return req_kwargs  # 全部通过,返回最终请求参数

    def _run_resp_interceptors(self, resp_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        私有方法:顺序执行响应拦截器链

        :param resp_result: 解析完成的响应结果字典
        :type resp_result: Dict[str, Any]
        :return: 拦截器链加工完成的结果字典
        :rtype: Dict[str, Any]
        :raises: 无(拦截器回调自身异常不在此捕获)
        """
        result = resp_result  # 以入参作为链路初始结果
        for inter_func in self.resp_interceptors:  # 按注册顺序遍历响应拦截器
            result = inter_func(result)  # 每个拦截器加工上一步结果
        return result  # 返回最终加工结果

    def _do_retry_logic(self, exc: Exception, retry_count: int, max_retry: int, is_retryable: bool) -> tuple[bool, int]:
        """
        私有方法:重试逻辑判断

        :param exc: 当前捕获异常对象
        :type exc: Exception
        :param retry_count: 当前已经重试次数
        :type retry_count: int
        :param max_retry: 本次请求允许最大重试次数
        :type max_retry: int
        :param is_retryable: 上层已判定的可重试标志(Timeout/ConnectionError/5xx为True)
        :type is_retryable: bool
        :return: (是否继续重试, 更新后的重试计数)
        :rtype: tuple[bool, int]
        :raises: 无
        """
        if retry_count >= max_retry:  # 已达到重试上限
            return False, retry_count  # 不再重试,计数不变
        if not is_retryable:  # 当前异常不属于可重试类型
            return False, retry_count  # 直接终止
        time.sleep(self.retry_sleep)  # 按配置休眠,避免高频重试压垮服务端
        retry_count += 1  # 重试计数加1
        return True, retry_count  # 告知上层继续重试

    @overload
    def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        json: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        proxies: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        verify: Optional[bool] = None,
        max_retries: Optional[int] = None,
        skip_parse: Literal[True] = True,
        **kwargs
    ) -> requests.Response:
        """
        重载分支一:skip_parse=True 时返回原始Response

        :param method: HTTP大写方法字符串
        :type method: str
        :param endpoint: 接口相对路径或完整http url
        :type endpoint: str
        :param params: url查询参数字典
        :type params: Optional[Dict]
        :param data: form-urlencoded表单数据字典
        :type data: Optional[Dict]
        :param json: json请求体字典
        :type json: Optional[Dict]
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param proxies: 单次请求代理字典
        :type proxies: Optional[Dict[str, str]]
        :param timeout: 单次请求超时秒数
        :type timeout: Optional[int]
        :param verify: 单次是否校验ssl证书
        :type verify: Optional[bool]
        :param max_retries: 单次最大重试次数
        :type max_retries: Optional[int]
        :param skip_parse: 固定为True,跳过JSON解析
        :type skip_parse: Literal[True]
        :param kwargs: 透传给requests的其他参数
        :type kwargs: Any
        :return: 原始requests.Response对象
        :rtype: requests.Response
        :raises: 无
        """
        ...  # 仅类型检查器使用的重载存根,无实际实现

    @overload
    def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        json: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        proxies: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        verify: Optional[bool] = None,
        max_retries: Optional[int] = None,
        skip_parse: Literal[False] = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        重载分支二:skip_parse=False(默认) 时返回解析字典

        :param method: HTTP大写方法字符串
        :type method: str
        :param endpoint: 接口相对路径或完整http url
        :type endpoint: str
        :param params: url查询参数字典
        :type params: Optional[Dict]
        :param data: form-urlencoded表单数据字典
        :type data: Optional[Dict]
        :param json: json请求体字典
        :type json: Optional[Dict]
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param proxies: 单次请求代理字典
        :type proxies: Optional[Dict[str, str]]
        :param timeout: 单次请求超时秒数
        :type timeout: Optional[int]
        :param verify: 单次是否校验ssl证书
        :type verify: Optional[bool]
        :param max_retries: 单次最大重试次数
        :type max_retries: Optional[int]
        :param skip_parse: 固定为False,解析JSON
        :type skip_parse: Literal[False]
        :param kwargs: 透传给requests的其他参数
        :type kwargs: Any
        :return: 解析后的结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        ...  # 仅类型检查器使用的重载存根,无实际实现

    def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        json: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        proxies: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        verify: Optional[bool] = None,
        max_retries: Optional[int] = None,
        skip_parse: bool = False,
        **kwargs
    ) -> Union[Dict[str, Any], requests.Response]:
        """
        底层统一请求入口,get/post/put/delete/patch全部调用本方法

        :param method: HTTP大写方法字符串 "GET"/"POST"/"PUT"/"DELETE"/"PATCH"
        :type method: str
        :param endpoint: 接口相对路径或者完整http url
        :type endpoint: str
        :param params: url查询参数字典,拼接在url问号后
        :type params: Optional[Dict]
        :param data: form-urlencoded表单数据字典
        :type data: Optional[Dict]
        :param json: json请求体字典,requests自动添加Content-Type:application/json
        :type json: Optional[Dict]
        :param headers: 单次请求头字典,覆盖全局headers
        :type headers: Optional[Dict[str, str]]
        :param proxies: 单次请求代理,覆盖实例全局代理
        :type proxies: Optional[Dict[str, str]]
        :param timeout: 单次请求超时秒数,覆盖全局timeout
        :type timeout: Optional[int]
        :param verify: 单次是否校验ssl证书,覆盖全局verify_ssl
        :type verify: Optional[bool]
        :param max_retries: 单次最大重试次数,覆盖实例全局max_retries
        :type max_retries: Optional[int]
        :param skip_parse: True跳过JSON解析,直接返回原始requests.Response,用于获取HTML网页
        :type skip_parse: bool
        :param kwargs: 透传给requests的其他参数,例如files
        :type kwargs: Any
        :return: 解析字典;skip_parse=True返回requests原始Response对象
        :rtype: Union[Dict[str, Any], requests.Response]
        :raises: 无(网络/HTTP异常内部捕获并转为带error键的字典)
        """
        full_url = self._build_url(endpoint)  # 拼接得到最终请求URL
        use_timeout = timeout if timeout is not None else self.timeout  # 单次超时优先,否则用全局超时
        use_proxies = proxies if proxies is not None else self.proxies  # 单次代理优先,否则用全局代理
        # 联动:单次/全局任一存在非空代理,即关闭证书校验
        use_verify = verify if verify is not None else self.verify_ssl  # 单次verify优先,否则取全局校验意图
        use_verify = self._effective_verify(use_verify, use_proxies)  # 存在代理时强制关闭证书校验
        use_max_retry = max_retries if max_retries is not None else self.max_retries  # 单次重试次数优先
        current_retry = 0  # 当前已重试次数归零
        # 请求计时:成功日志带耗时,便于定位慢接口(DEBUG级,分片高频请求不污染app.log)
        req_started_at = time.time()  # 记录请求开始时间戳

        final_headers = self.default_headers.copy()  # 从全局头复制,避免污染实例默认头
        if headers is not None:  # 传入单次请求头时
            final_headers.update(headers)  # 合并并覆盖同名头

        req_kwargs = {  # 组装透传给session.request的全部参数
            "method": method.upper(),  # HTTP方法统一转为大写
            "url": full_url,  # 完整请求URL
            "params": params,  # URL查询参数
            "data": data,  # 表单数据
            "json": json,  # JSON请求体
            "headers": final_headers,  # 合并后的最终请求头
            "proxies": use_proxies,  # 生效代理
            "timeout": use_timeout,  # 生效超时
            "verify": use_verify,  # 生效SSL校验开关
            ** kwargs  # 其他透传参数(如files)
        }
        inter_result = self._run_req_interceptors(req_kwargs)  # 执行请求拦截器链
        if isinstance(inter_result, dict) and "error" in inter_result:  # 拦截器主动中止请求
            if self.logger:  # 配置了日志器才记录
                self.logger.error(f"❌ 请求拦截器中止请求 url={full_url}, err={inter_result.get('error')}")  # 记录中止原因
            return inter_result  # 不发出网络请求,直接返回错误字典
        req_kwargs = inter_result  # 用拦截器加工后的参数继续
        if self.logger:  # 配置日志器时打印DEBUG请求行
            self.logger.debug(  # DEBUG日志:请求发出前的配置信息
                f"🌐 → {method.upper()} {full_url} "  # 请求方法与URL
                f"(代理={'有' if use_proxies else '无'},verify={use_verify},timeout={use_timeout}s)"  # 代理/校验/超时配置
            )

        while True:  # 请求-重试无限循环,由内部return退出
            try:  # 捕获网络/HTTP异常以判定是否重试
                resp = self.session.request(** req_kwargs)  # 发起实际HTTP请求
                resp.raise_for_status()  # 4xx/5xx状态码抛HTTPError
                cost_ms = int((time.time() - req_started_at) * 1000)  # 计算本次总耗时(毫秒)
                if skip_parse:  # 调用方要求原始响应(如HTML网页)
                    if self.logger:  # 记录原始响应调试日志
                        self.logger.debug(  # DEBUG日志:原始响应返回
                            f"✅ ← {method.upper()} {full_url} HTTP {resp.status_code} "  # 方法/URL/状态码
                            f"{cost_ms}ms retry={current_retry}(原始响应)"  # 耗时与重试次数
                        )
                    return resp  # 直接返回requests.Response对象
                result = self._parse_response(resp)  # 解析JSON(含花括号片段兜底提取)
                result = self._run_resp_interceptors(result)  # 执行响应拦截器链
                if self.logger:  # 记录成功调试日志
                    self.logger.debug(  # DEBUG日志:JSON响应返回
                        f"✅ ← {method.upper()} {full_url} HTTP {resp.status_code} "  # 方法/URL/状态码
                        f"{cost_ms}ms retry={current_retry}"  # 耗时与重试次数
                    )
                return result  # 返回解析后的结果字典
            except requests.exceptions.Timeout as e:  # 超时异常:可重试
                raw_err = str(e)  # 保存错误文本
                is_retryable = True  # 标记为可重试
                exc_obj = e  # 保存异常对象供统一处理
            except requests.exceptions.ConnectionError as e:  # 连接异常:可重试
                raw_err = str(e)  # 保存错误文本
                is_retryable = True  # 标记为可重试
                exc_obj = e  # 保存异常对象
            except requests.exceptions.HTTPError as e:  # HTTP状态错误:仅5xx可重试
                raw_err = str(e)  # 保存错误文本
                is_retryable = False  # 默认不可重试
                exc_obj = e  # 保存异常对象
                if e.response is not None and 500 <= e.response.status_code <= 599:  # 服务端5xx
                    is_retryable = True  # 5xx标记为可重试
            except Exception as e:  # 其他未知异常:不重试
                raw_err = str(e)  # 保存错误文本
                is_retryable = False  # 标记不可重试
                exc_obj = e  # 保存异常对象
                import traceback  # 局部导入堆栈打印模块
                traceback.print_exc()  # 打印未知异常完整堆栈,便于排查

            retry_go, current_retry = self._do_retry_logic(exc_obj, current_retry, use_max_retry, is_retryable)  # 判定是否继续重试
            if retry_go:  # 需要重试
                if self.logger:  # 记录重试警告日志
                    self.logger.warning(  # WARNING日志:即将重试
                        f"⚠ 请求失败,第{current_retry}/{use_max_retry}次重试 "  # 当前重试轮次
                        f"{method.upper()} {full_url} | {type(exc_obj).__name__}:{raw_err}"  # 请求信息与异常摘要
                    )
                continue  # 进入下一轮while重新发起请求

            status_code = None  # 终止时默认无状态码
            resp = getattr(exc_obj, "response", None)  # 尝试从异常对象取响应
            if resp is not None:  # 异常携带响应对象
                status_code = resp.status_code  # 提取HTTP状态码
            err_dict = {  # 组装最终错误字典
                "error": raw_err,  # 错误文本
                "url": full_url,  # 请求URL
                "status_code": status_code,  # HTTP状态码(可能为None)
                "raw_exception": type(exc_obj).__name__,  # 异常类名
                "is_retryable": is_retryable,  # 该错误理论上是否可重试
                "retry_count": current_retry  # 实际已重试次数
            }
            # 错误字典不经过响应拦截器(拦截器期望正常业务JSON)
            cost_ms = int((time.time() - req_started_at) * 1000)  # 计算从发起到终止总耗时
            if self.logger:  # 记录最终失败错误日志
                self.logger.error(  # ERROR日志:请求彻底失败
                    f"❌ 请求最终失败 {method.upper()} {full_url} "  # 方法与URL
                    f"HTTP={status_code} {type(exc_obj).__name__} 耗时{cost_ms}ms "  # 状态码/异常名/耗时
                    f"重试{current_retry}次 | {raw_err}"  # 重试次数与错误详情
                )
            return err_dict  # 返回错误字典,业务层用 "error" in resp 判断

    def get(
        self,
        endpoint: str,
        params: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        ** kwargs
    ) -> Dict[str, Any]:
        """
        GET高层封装,仅用于JSON接口;获取HTML请使用request(skip_parse=True)

        :param endpoint: 接口路径或者完整url
        :type endpoint: str
        :param params: url查询参数字典
        :type params: Optional[Dict]
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param kwargs: 透传proxies、timeout等参数
        :type kwargs: Any
        :return: 解析结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        return self.request("GET", endpoint, params=params, headers=headers, ** kwargs)  # 委托统一入口发起GET

    def post(
        self,
        endpoint: str,
        data=None,
        json=None,
        headers: Optional[Dict[str, str]] = None,
        ** kwargs
    ) -> Dict[str, Any]:
        """
        POST高层封装,仅用于JSON接口

        :param endpoint: 接口路径或者完整url
        :type endpoint: str
        :param data: form-urlencoded表单字典
        :type data: Any
        :param json: json请求体字典
        :type json: Any
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param kwargs: 透传其他参数
        :type kwargs: Any
        :return: 解析结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        return self.request("POST", endpoint, data=data, json=json, headers=headers,** kwargs)  # 委托统一入口发起POST

    def put(
        self,
        endpoint: str,
        data=None,
        json=None,
        headers: Optional[Dict[str, str]] = None,
        ** kwargs
    ) -> Dict[str, Any]:
        """
        PUT高层封装

        :param endpoint: 接口路径或者完整url
        :type endpoint: str
        :param data: form表单数据
        :type data: Any
        :param json: json请求体
        :type json: Any
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param kwargs: 透传其他参数
        :type kwargs: Any
        :return: 解析结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        return self.request("PUT", endpoint, data=data, json=json, headers=headers,** kwargs)  # 委托统一入口发起PUT

    def delete(
        self,
        endpoint: str,
        params=None,
        headers: Optional[Dict[str, str]] = None,
        ** kwargs
    ) -> Dict[str, Any]:
        """
        DELETE高层封装

        :param endpoint: 接口路径或者完整url
        :type endpoint: str
        :param params: url查询参数
        :type params: Any
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param kwargs: 透传其他参数
        :type kwargs: Any
        :return: 解析结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        return self.request("DELETE", endpoint, params=params, headers=headers, ** kwargs)  # 委托统一入口发起DELETE

    def patch(
        self,
        endpoint: str,
        data=None,
        json=None,
        headers: Optional[Dict[str, str]] = None,
        ** kwargs
    ) -> Dict[str, Any]:
        """
        PATCH高层封装

        :param endpoint: 接口路径或者完整url
        :type endpoint: str
        :param data: form表单数据
        :type data: Any
        :param json: json请求体
        :type json: Any
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param kwargs: 透传其他参数
        :type kwargs: Any
        :return: 解析结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        return self.request("PATCH", endpoint, data=data, json=json, headers=headers,** kwargs)  # 委托统一入口发起PATCH

    def upload(self, endpoint: str, files: Dict, data: Optional[Dict] = None, headers: Optional[Dict[str, str]] = None, ** kwargs):
        """
        multipart/form-data 文件上传;调用方自行管理打开的文件句柄生命周期

        :param endpoint: 上传接口路径或者完整url
        :type endpoint: str
        :param files: 文件字典 {"file": open("test.bin","rb")}
        :type files: Dict
        :param data: 上传附带普通form表单字段字典
        :type data: Optional[Dict]
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param kwargs: 透传proxies timeout等参数
        :type kwargs: Any
        :return: 解析结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        return self.request("POST", endpoint, files=files, data=data, headers=headers, ** kwargs)  # 以multipart方式POST上传文件
