#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: http_github.py
# 归属: infrastructure 基础设施层 —— GitHub REST-API 底层封装
# ------------------------------------------------------------------------------
# 文件用途:
#   GitHub REST-API 底层封装,仅获取元数据(用户/仓库/release/asset 的查询
#   与标准化),不含任何文件下载与磁盘文件名处理逻辑(下载能力已迁移至
#   file_transfer)。
# ------------------------------------------------------------------------------
# 架构定位:
#   位于 infrastructure 基础设施层;只依赖标准库与第三方 requests/urllib3,
#   无任何项目内模块依赖;被上层业务(版本检查、下载编排等)调用,
#   infrastructure 不反向依赖业务层。
#
#   关联组件:
#     - 上游: service.github_update 业务层
#     - 下游: requests.Session、urllib3
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 会话池、重试、代理、拦截器、with 上下文管理
#   2. github_token 自动注入 Authorization: Bearer 请求头,has_token 可判断鉴权状态
#   3. 用户/仓库/release/asset 元数据查询,release 列表自动分页
#   4. 统一标准化 release 资产:label 优先展示名、_raw_assets 原始副本、
#      _original_name、sha256
#   5. 支持按【label 展示名(可中文)】查找原始 asset 对象
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅获取 GitHub API 元数据,不做文件下载、不做磁盘文件名处理
#   - 资产标准化:对外 asset["name"] 优先取 label 展示名,label 为空回退原生 name
#   - _raw_assets 保存 API 返回的原始 asset 完整副本,供上层业务深度使用
#   - 不做磁盘文件名处理、不做文件下载;下载能力迁移至 file_transfer
# ------------------------------------------------------------------------------
# 线程模型:
#   复用 requests.Session;配置非空代理时强制关闭 SSL 证书校验并抑制
#   InsecureRequestWarning;默认 timeout=15s;token 仅通过请求头传递,不落日志明文。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: copy、json、re、time、typing
#   - 第三方: requests、urllib3
#   - 项目内: 无
# ==============================================================================
import copy  # 标准库:深拷贝release原始字典,避免污染入参
import json  # 标准库:响应JSON兼容解析
import re  # 标准库:sha256与中文字符正则
import time  # 标准库:重试休眠与请求计时
from typing import Any, Callable, Dict, List, Optional, Union  # 标准库:类型注解

import requests  # 第三方:HTTP请求库,提供Session连接池
import urllib3  # 第三方:用于关闭底层不安全SSL警告

# 走代理时统一关闭SSL证书验证(见request中联动逻辑),抑制对应的InsecureRequestWarning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 全局抑制urllib3证书校验关闭警告


class GitHubApiClient:
    """
    GitHub REST-API客户端类,仅负责元数据查询,不处理任何文件下载落地

    设计原则:
        - 单一职责:仅获取 GitHub API 元数据,不做文件下载、不做磁盘文件名处理
        - 资产标准化:所有 release 资产统一经过 _normalize_release_assets 标准化处理
        - label 优先:对外 asset["name"] 优先取 label 展示名(支持中文),label 为空才回退原生 name
        - 原始留存:_raw_assets 保存 API 返回的原始 asset 完整副本,供上层业务深度使用

    资产标准化规则(_normalize_release_assets):
        1. 深拷贝原始 release 字典,绝不污染入参
        2. 保存 _raw_assets 列表:每个 asset 的原始完整副本
        3. 对外 name 字段优先替换为 label(展示名,支持中文)
        4. 备份 _original_name:GitHub API 原生 name 字段
        5. 解析 digest 字段提取纯 sha256 哈希,存入 asset["sha256"]
        6. 删除对外输出中的 label 字段,保持字典干净

    API 速率限制说明:
        - 匿名访问(无 token):每小时 60 次请求,无法读取 Draft 草稿 Release
        - 携带 token:每小时 5000 次请求,可读取 Draft 草稿 Release
        - 建议:生产环境务必配置 github_token,避免触发速率限制

    线程安全说明:
        - 实例内部持有单个 requests.Session,Session 本身线程不安全
        - 多线程环境下建议每个线程持有独立的 GitHubApiClient 实例
        - 拦截器回调在请求线程中同步执行,需自行保证线程安全

    类常量:
        API_BASE_URL (str): GitHub 官方 API 根地址,默认 "https://api.github.com"
        RE_SHA256 (Pattern): 预编译正则,匹配 sha256: 前缀后的 64 位十六进制哈希
        RE_CHINESE (Pattern): 预编译正则,匹配 CJK 中文字符 Unicode 区间

    实例属性:
        api_base_url (str): 实际使用的 API 根地址,可自定义为兼容网关/镜像
        github_token (Optional[str]): GitHub 个人访问 token,None 表示匿名访问
        timeout (int): HTTP 请求超时秒数,默认 15 秒
        proxies (Dict[str, str]): 全局代理配置字典,空字典表示无代理
        verify_ssl (bool): 是否校验 HTTPS SSL 证书,默认 False;走代理时被强制关闭
        max_retries (int): 最大重试次数,0 表示关闭重试
        retry_sleep (float): 每次重试前休眠秒数,默认 15 秒
        logger (Any): 外部日志对象,None 则不输出任何内部日志
        session (requests.Session): requests 底层会话实例,维护连接池与状态
        default_headers (Dict[str, str]): 默认请求头,有 token 时自动注入 Bearer Authorization
        req_interceptors (List[Callable]): 请求拦截器回调列表,按注册顺序执行
        resp_interceptors (List[Callable]): 响应拦截器回调列表,按注册顺序执行
        retry_exceptions (tuple): 可触发重试的异常类型元组(Timeout, ConnectionError)
        has_token (bool): 只读属性,判断当前实例是否携带有效 token

    :param github_token: GitHub personal token;匿名访问传None
    :type github_token: Optional[str]
    :param timeout: 请求超时秒数
    :type timeout: int
    :param headers: 全局附加请求头字典
    :type headers: Optional[Dict[str, str]]
    :param proxies: 代理字典 {"http":"xxx", "https":"xxx"}
    :type proxies: Optional[Dict[str, str]]
    :param verify_ssl: 是否校验HTTPS SSL证书,False关闭校验
    :type verify_ssl: bool
    :param max_retries: 最大重试次数,0关闭重试
    :type max_retries: int
    :param retry_sleep: 重试等待休眠秒数
    :type retry_sleep: float
    :param api_base_url: GitHub API根地址,可配置为兼容网关/镜像地址
    :type api_base_url: Optional[str]
    :param logger: 外部日志对象
    :type logger: Any
    :return: 无返回值(构造完成得到客户端实例)
    :rtype: None
    :raises Exception: requests.Session()创建失败等底层异常会直接向上抛出
    """
    API_BASE_URL = "https://api.github.com"  # GitHub官方API根地址(类级默认常量)
    RE_SHA256 = re.compile(r"sha256[::]\s*([0-9a-fA-F]{64})")  # 预编译:匹配sha256:前缀后的64位十六进制哈希
    RE_CHINESE = re.compile(r"[\u4e00-\u9fff]")  # 预编译:匹配CJK中文字符Unicode区间

    def __init__(
        self,
        github_token: Optional[str] = None,
        timeout: int = 15,
        headers: Optional[Dict[str, str]] = None,
        proxies: Optional[Dict[str, str]] = None,
        verify_ssl: bool = False,
        max_retries: int = 0,
        retry_sleep: float = 15.0,
        api_base_url: Optional[str] = None,
        logger=None
    ):
        """
        初始化GitHubApiClient实例
        :param github_token: GitHub personal token;匿名访问传None
        :type github_token: Optional[str]
        :param timeout: 请求超时秒数
        :type timeout: int
        :param headers: 全局附加请求头字典
        :type headers: Optional[Dict[str, str]]
        :param proxies: 代理字典 {"http":"xxx", "https":"xxx"}
        :type proxies: Optional[Dict[str, str]]
        :param verify_ssl: 是否校验HTTPS SSL证书,False关闭校验
        :type verify_ssl: bool
        :param max_retries: 最大重试次数,0关闭重试
        :type max_retries: int
        :param retry_sleep: 重试等待休眠秒数
        :type retry_sleep: float
        :param api_base_url: GitHub API根地址,默认官方https://api.github.com,
                             可配置为兼容网关/镜像地址(末尾斜杠自动去除)
        :type api_base_url: Optional[str]
        :param logger: 外部日志对象
        :type logger: Any
        :return: 无返回值
        :rtype: None
        :raises Exception: requests.Session()创建失败等底层异常会直接向上抛出
        """
        self.api_base_url = (api_base_url or self.API_BASE_URL).rstrip("/")  # 自定义根地址优先,缺省用官方地址,并去除末尾斜杠
        self.github_token = github_token.strip() if (isinstance(github_token, str) and github_token.strip()) else None  # 空白token归一为None
        self.timeout = timeout  # 保存请求超时秒数
        self.proxies = proxies if proxies is not None else {}  # 代理字典,None归一为空字典
        self.verify_ssl = verify_ssl  # 保存SSL校验开关(代理存在时请求中联动关闭)
        self.max_retries = max_retries  # 保存最大重试次数,0不重试
        self.retry_sleep = retry_sleep  # 保存重试前休眠秒数
        self.logger = logger  # 注入外部日志器,None不打印日志
        self.session: requests.Session = requests.Session()  # 创建底层会话(连接池自动维护)
        self.default_headers: Dict[str, str] = {  # 初始化默认请求头
            "Accept": "application/vnd.github+json"  # GitHub推荐的API版本Accept头
        }
        if self.github_token:  # 携带有效token时
            self.default_headers["Authorization"] = f"Bearer {self.github_token}"  # 自动注入Bearer鉴权头
        if headers:  # 调用方传入附加头
            self.default_headers.update(headers)  # 合并覆盖默认头
        self.req_interceptors: List[Callable] = []  # 初始化请求拦截器回调链
        self.resp_interceptors: List[Callable] = []  # 初始化响应拦截器回调链
        self.retry_exceptions = (  # 定义可触发重试的异常类型元组
            requests.exceptions.Timeout,  # 请求超时异常
            requests.exceptions.ConnectionError  # 连接失败异常
        )

    @property
    def has_token(self) -> bool:
        """
        只读属性:判断当前实例是否携带有效的github_token

        :return: token非空返回True,匿名(None/空串)返回False
        :rtype: bool
        :raises: 无
        """
        return bool(self.github_token)  # 强制转布尔,None与空串均为False

    @staticmethod
    def _has_chinese(text: str) -> bool:
        """
        静态私有工具:判断字符串是否包含中文字符
        :param text: 输入源字符串
        :type text: str
        :return: True包含中文;False不包含中文
        :rtype: bool
        :raises: 无
        """
        if not isinstance(text, str):  # 非字符串入参不参与正则判断
            return False  # 直接返回False,避免None等类型报错
        return bool(GitHubApiClient.RE_CHINESE.search(text))  # 正则搜到中文字符即为True

    @staticmethod
    def _calc_display_name(asset_raw: Dict) -> str:
        """
        静态私有工具:计算asset对外展示名称;优先取label,label为空回退name
        ⚠️返回仅用于UI展示,**不是磁盘安全文件名**
        :param asset_raw: GitHub接口返回原始asset字典
        :type asset_raw: Dict
        :return: 展示名字符串
        :rtype: str
        :raises: 无
        """
        label = asset_raw.get("label", "").strip()  # 取label并去除首尾空白
        name = asset_raw.get("name", "")  # 取API原生name字段
        if label:  # label非空时优先展示
            return label  # 返回label完整展示名
        return name  # label为空回退原生name

    @staticmethod
    def extract_sha256_from_digest(digest_str: Optional[str]) -> Optional[str]:
        """
        静态工具:从digest字符串提取纯净sha256哈希;格式示例 sha256:abcdef...
        :param digest_str: asset的digest字段字符串
        :type digest_str: Optional[str]
        :return: 纯sha256十六进制字符串;格式错误/输入为空返回None
        :rtype: Optional[str]
        :raises: 无
        """
        if not digest_str or not isinstance(digest_str, str):  # 空值或非字符串无法提取
            return None  # 返回None
        if digest_str.startswith("sha256:"):  # 符合sha256:前缀格式
            return digest_str.replace("sha256:", "").strip()  # 去掉前缀并去除首尾空白
        return None  # 其他无法识别格式返回None

    @staticmethod
    def _normalize_release_assets(release_raw: Dict) -> Dict:
        """
        【底层统一标准化release的assets数组】
        作用:get_latest_release / get_release_by_tag 共用
        1. 保存 _raw_assets:原始完整asset副本
        2. name 保持 GitHub API 原始文件名（不覆盖），新增 display_name 存 label 展示名
        3. 存入 _original_name（与 name 相同，向后兼容）
        4. 解析填充 sha256
        5. 删除多余 label 字段
        :param release_raw: github api返回原始release字典
        :type release_raw: Dict
        :return: 处理完成的release字典
        :rtype: Dict
        :raises: 无
        """
        rel = copy.deepcopy(release_raw)  # 深拷贝入参,标准化过程绝不污染原始字典
        rel["_raw_assets"] = []  # 初始化原始资产副本列表
        for asset in rel.get("assets", []):  # 遍历release内资产(assets缺失时按空列表处理)
            raw_copy = asset.copy()  # 复制当前asset的原始字段
            rel["_raw_assets"].append(raw_copy)  # 留存原始副本,供上层业务取用

            # name 始终保持 GitHub API 返回的实际文件名，绝不被 label 覆盖
            asset["_original_name"] = asset.get("name", "")  # 备份GitHub原始name（与name相同，向后兼容）
            label_raw = asset.get("label", "").strip()  # 取label并去空白
            if label_raw:  # label非空（可能是中文展示名）
                asset["display_name"] = label_raw  # 新增 display_name 字段存 label 展示名（供UI显示）
            else:  # label为空
                asset["display_name"] = asset.get("name", "")  # 无label时 display_name 回退为文件名

            raw_digest = asset.get("digest")  # 取digest字段(可能为None)
            asset["sha256"] = GitHubApiClient.extract_sha256_from_digest(raw_digest)  # 解析出纯sha256哈希

            if "label" in asset:  # 对外输出结构中仍残留label字段
                del asset["label"]  # 删除label,保持对外字典干净
        return rel  # 返回标准化后的release字典

    def extract_sha256_from_body(self, body: str) -> List[Dict[str, str]]:
        """
        解析release正文body markdown文本,正则提取全部sha256哈希
        :param body: release描述正文
        :type body: str
        :return: [{"sha256":"xxx"}, ...] 哈希列表
        :rtype: List[Dict[str, str]]
        :raises: 无
        """
        if not body:  # 正文为空无需解析
            return []  # 返回空列表
        matches = self.RE_SHA256.findall(body)  # 正则提取全部64位哈希串
        res = []  # 准备结果列表
        for h in matches:  # 逐个规整哈希
            res.append({"sha256": h.lower()})  # 统一转为小写后包装成字典
        return res  # 返回哈希字典列表

    def set_headers(self, headers: Dict[str, str]) -> None:
        """
        更新实例全局默认请求头,合并覆盖

        :param headers: 需要合并更新的头字典
        :type headers: Dict[str, str]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        self.default_headers.update(headers)  # 以update方式合并/覆盖默认请求头

    def set_proxy(self, proxies: Optional[Dict[str, str]]) -> None:
        """
        设置全局代理;传入None清空代理

        :param proxies: 代理字典或None
        :type proxies: Optional[Dict[str, str]]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        if proxies is None:  # 传入None表示清空
            self.proxies = {}  # 重置为空字典
        else:  # 传入有效代理
            self.proxies = proxies  # 覆盖全局代理

    def add_req_interceptor(self, func: Callable[[Dict], Dict]) -> None:
        """
        注册请求拦截器;返回带error字典直接终止网络请求

        :param func: 请求拦截回调,入参请求字典
        :type func: Callable[[Dict], Dict]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        self.req_interceptors.append(func)  # 追加到请求拦截器链

    def add_resp_interceptor(self, func: Callable[[Dict], Dict]) -> None:
        """
        注册响应拦截器,对解析完成的结果字典二次处理

        :param func: 响应拦截回调,入参/出参均为结果字典
        :type func: Callable[[Dict], Dict]
        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        self.resp_interceptors.append(func)  # 追加到响应拦截器链

    def close(self) -> None:
        """
        关闭http会话连接池,释放TCP资源

        :return: 无返回值
        :rtype: None
        :raises: 无
        """
        if hasattr(self, "session") and self.session:  # 防御:session存在且有效才关闭
            self.session.close()  # 关闭底层会话归还连接

    def __enter__(self):
        """
        with上下文管理器入口

        :return: 返回客户端自身,供 as 变量使用
        :rtype: GitHubApiClient
        :raises: 无
        """
        return self  # 返回自身实例

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        with退出钩子,无论是否异常自动关闭会话

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
        self.close()  # 退出时无条件关闭会话

    def _build_url(self, endpoint: str) -> str:
        """
        私有方法:拼接接口完整url;传入完整http链接直接原样返回

        :param endpoint: 接口相对路径或完整http url
        :type endpoint: str
        :return: 完整请求url
        :rtype: str
        :raises: 无
        """
        if endpoint.lower().startswith("http"):  # 已是完整http(s)链接
            return endpoint  # 原样返回不拼接
        base = self.api_base_url  # 根地址(初始化时已去除尾斜杠)
        ep = endpoint.lstrip("/")  # 去掉endpoint头部斜杠
        return f"{base}/{ep}"  # 拼接成完整URL

    def _run_req_interceptors(self, req_args: Dict) -> Optional[Dict]:
        """
        私有方法:顺序执行请求拦截器,遇到error直接返回错误字典

        :param req_args: 即将发出的请求参数字典
        :type req_args: Dict
        :return: 拦截器返回的错误字典;全部放行时返回None
        :rtype: Optional[Dict]
        :raises: 无(回调自身异常不在此捕获)
        """
        for inter in self.req_interceptors:  # 按注册顺序遍历
            res = inter(req_args)  # 执行拦截回调
            if isinstance(res, dict) and "error" in res:  # 回调返回错误字典
                return res  # 中止并返回该错误
        return None  # 全部通过,None表示放行

    def _run_resp_interceptors(self, result: Dict) -> Dict:
        """
        私有方法:执行响应拦截器链,依次处理结果字典

        :param result: 解析后的响应结果字典
        :type result: Dict
        :return: 拦截器链加工完成的结果字典
        :rtype: Dict
        :raises: 无(回调自身异常不在此捕获)
        """
        resp_data = result  # 以入参作为链路初始结果
        for inter in self.resp_interceptors:  # 按注册顺序遍历
            resp_data = inter(resp_data)  # 每个拦截器加工上一步结果
        return resp_data  # 返回最终结果

    def _parse_json_response(self, resp: requests.Response) -> Dict[str, Any]:
        """
        私有方法:解析response;JSON解析失败做兼容处理

        :param resp: requests响应对象
        :type resp: requests.Response
        :return: 解析字典;彻底失败时返回带error键的错误字典
        :rtype: Dict[str, Any]
        :raises: 无
        """
        try:  # 优先标准JSON解析
            return resp.json()  # 返回requests解析出的JSON对象
        except (ValueError, json.JSONDecodeError):  # 非合法JSON时兜底
            raw_bytes = resp.content  # 取原始字节
            idx_start = raw_bytes.find(b"{")  # 首花括号位置
            idx_end = raw_bytes.rfind(b"}")  # 尾花括号位置
            json_candidate = None  # 二次解析结果占位
            if idx_start != -1 and idx_end != -1 and idx_end > idx_start:  # 区间合法才截取
                try:  # 尝试解析花括号片段
                    json_candidate = json.loads(raw_bytes[idx_start:idx_end+1])  # 截取首尾花括号间内容并加载
                except json.JSONDecodeError:  # 片段仍非法
                    pass  # 保持None
            if json_candidate is not None:  # 二次解析成功
                return json_candidate  # 返回提取出的JSON对象
            return {  # 彻底失败,返回标准化错误结构
                "_parse_ok": False,  # 解析失败标记
                "error": "response not valid json",  # 错误描述
                "status_code": resp.status_code,  # HTTP状态码
                "raw_text": resp.text  # 原始文本供排查
            }

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
        max_retry: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        底层通用请求入口,所有上层API都调用此方法

        :param method: HTTP大写方法字符串
        :type method: str
        :param endpoint: 接口相对路径或完整http url
        :type endpoint: str
        :param params: url查询参数字典
        :type params: Optional[Dict]
        :param data: form表单数据字典
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
        :param max_retry: 单次最大重试次数
        :type max_retry: Optional[int]
        :param kwargs: 预留扩展参数(本方法不透传进session.request)
        :type kwargs: Any
        :return: 解析结果字典;失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无(网络/HTTP异常内部捕获并转为错误字典)
        """
        full_url = self._build_url(endpoint)  # 拼接完整请求URL
        use_timeout = timeout if timeout is not None else self.timeout  # 单次超时优先
        use_proxy = proxies if proxies is not None else self.proxies  # 单次代理优先
        use_verify = verify if verify is not None else self.verify_ssl  # 单次verify优先
        # 联动:单次/全局任一存在非空代理,即关闭证书校验
        use_verify = False if use_proxy else use_verify  # 有代理强制False,无代理沿用取值
        use_max_retry = max_retry if max_retry is not None else self.max_retries  # 单侧重试次数优先
        current_retry = 0  # 当前已重试次数归零
        # 请求计时:成功日志带耗时(DEBUG级,不污染app.log)
        req_started_at = time.time()  # 记录请求开始时间戳
        final_headers = self.default_headers.copy()  # 复制默认头,避免污染实例属性
        if headers:  # 传入单次请求头
            final_headers.update(headers)  # 合并覆盖
        req_params = {  # 组装requests请求参数
            "method": method.upper(),  # 方法统一大写
            "url": full_url,  # 完整URL
            "params": params,  # 查询参数
            "data": data,  # 表单数据
            "json": json,  # JSON请求体
            "headers": final_headers,  # 最终请求头
            "proxies": use_proxy,  # 生效代理
            "timeout": use_timeout,  # 生效超时
            "verify": use_verify,  # 生效SSL开关
        }
        intercept_err = self._run_req_interceptors(req_params)  # 执行请求拦截器链
        if intercept_err:  # 拦截器返回错误字典
            if self.logger:  # 记录中止日志
                self.logger.error(f"❌ GitHub请求拦截器中止请求 url={full_url}, err={intercept_err.get('error') if isinstance(intercept_err, dict) else intercept_err}")  # 打印中止原因
            return intercept_err  # 不发请求直接返回错误
        if self.logger:  # 打印DEBUG请求行
            self.logger.debug(  # DEBUG日志:请求发出前状态
                f"🌐 → {method.upper()} {full_url} "  # 方法与URL
                f"(代理={'有' if use_proxy else '无'},verify={use_verify},token={'有' if self.has_token else '无'})"  # 代理/校验/token状态
            )
        resp: Optional[requests.Response] = None  # 响应对象占位(异常分支也要读取状态码)
        while True:  # 请求-重试循环,由内部return退出
            try:  # 捕获网络/HTTP异常
                resp = self.session.request(**req_params)  # 发起实际HTTP请求
                resp.raise_for_status()  # 4xx/5xx抛HTTPError
                result = self._parse_json_response(resp)  # 解析JSON响应
                result["_has_token"] = self.has_token  # 附带本次请求是否携带token的标记
                result = self._run_resp_interceptors(result)  # 执行响应拦截器链
                if self.logger:  # 记录成功调试日志
                    cost_ms = int((time.time() - req_started_at) * 1000)  # 计算耗时毫秒
                    self.logger.debug(  # DEBUG日志:成功响应
                        f"✅ ← {method.upper()} {full_url} HTTP {resp.status_code} "  # 方法/URL/状态码
                        f"{cost_ms}ms retry={current_retry} token={self.has_token}"  # 耗时/重试/token
                    )
                return result  # 返回结果字典
            except self.retry_exceptions as e:  # 超时/连接错误:可重试
                raw_err = str(e)  # 保存错误文本
                is_retry = True  # 标记可重试
            except requests.exceptions.HTTPError as e:  # HTTP状态错误:仅5xx重试
                raw_err = str(e)  # 保存错误文本
                is_retry = False  # 默认不重试
                if resp is not None:  # 有响应对象才判断状态码
                    is_retry = 500 <= resp.status_code <= 599  # 仅5xx服务端错误可重试
            except Exception as e:  # 其他未知异常:不重试
                raw_err = str(e)  # 保存错误文本
                is_retry = False  # 标记不可重试
            if current_retry >= use_max_retry or not is_retry:  # 达到重试上限或错误不可重试
                err_dict = {  # 组装最终错误字典
                    "error": raw_err,  # 错误文本
                    "url": full_url,  # 请求URL
                    "status_code": resp.status_code if resp is not None else None,  # 状态码(可能None)
                    "raw_exception": raw_err,  # 原始异常信息
                    "is_retryable": is_retry,  # 是否属于可重试类型
                    "retry_count": current_retry,  # 实际重试次数
                    "_has_token": self.has_token  # 附带token标记
                }
                err_dict = self._run_resp_interceptors(err_dict)  # 错误字典同样过一遍响应拦截器
                if self.logger:  # 记录最终失败日志
                    cost_ms = int((time.time() - req_started_at) * 1000)  # 计算总耗时
                    self.logger.error(  # ERROR日志:请求彻底失败
                        f"❌ GitHub请求最终失败 {method.upper()} {full_url} "  # 方法与URL
                        f"HTTP={err_dict.get('status_code')} {raw_err} 耗时{cost_ms}ms "  # 状态码/错误/耗时
                        f"重试{current_retry}次"  # 重试次数
                    )
                return err_dict  # 返回错误字典
            current_retry += 1  # 重试计数加1
            if self.logger:  # 记录重试警告
                self.logger.warning(  # WARNING日志:即将重试
                    f"⚠ GitHub请求失败,第{current_retry}/{use_max_retry}次重试 "  # 当前重试轮次
                    f"{method.upper()} {full_url} | {raw_err} | {self.retry_sleep}s后重试"  # 请求/错误/休眠秒数
                )
            time.sleep(self.retry_sleep)  # 按配置休眠后进入下一轮

    def api_get(self, endpoint: str, params: Optional[Dict] = None, headers: Optional[Dict[str, str]] = None, **kwargs) -> Dict[str, Any]:
        """
        通用GET接口封装

        :param endpoint: 接口路径或完整URL
        :type endpoint: str
        :param params: url查询参数字典
        :type params: Optional[Dict]
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param kwargs: 透传其他参数
        :type kwargs: Any
        :return: 解析结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        return self.request("GET", endpoint, params=params, headers=headers,** kwargs)  # 委托request发起GET

    def api_post(self, endpoint: str, json: Optional[Dict] = None, headers: Optional[Dict[str, str]] = None, **kwargs) -> Dict[str, Any]:
        """
        通用POST请求

        :param endpoint: 接口路径或完整URL
        :type endpoint: str
        :param json: json请求体字典
        :type json: Optional[Dict]
        :param headers: 单次请求头字典
        :type headers: Optional[Dict[str, str]]
        :param kwargs: 透传其他参数
        :type kwargs: Any
        :return: 解析结果字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        return self.request("POST", endpoint, json=json, headers=headers,** kwargs)  # 委托request发起POST

    def get_user_info(self, username: str,** kwargs) -> Dict[str, Any]:
        """
        获取GitHub用户信息 GET /users/{username}

        :param username: GitHub用户名
        :type username: str
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: 用户信息字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        ep = f"/users/{username}"  # 拼接用户信息endpoint
        return self.api_get(ep,** kwargs)  # 发起GET查询

    def get_repo_info(self, owner: str, repo: str,** kwargs) -> Dict[str, Any]:
        """
        获取仓库基础元数据 GET /repos/{owner}/{repo}

        :param owner: 仓库拥有者
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: 仓库信息字典,失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        ep = f"/repos/{owner}/{repo}"  # 拼接仓库信息endpoint
        return self.api_get(ep,** kwargs)  # 发起GET查询

    def list_repo_releases(self, owner: str, repo: str, per_page:int=10, page:int=1,** kwargs) -> Dict[str, Any]:
        """
        获取单页release列表元数据

        :param owner: 仓库拥有者
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param per_page: 每页条数
        :type per_page: int
        :param page: 页码,从1开始
        :type page: int
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: 本页release列表(正常为list),失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        ep = f"/repos/{owner}/{repo}/releases"  # 拼接release列表endpoint
        params = {"per_page": per_page, "page": page}  # 组装分页参数
        return self.api_get(ep, params=params,** kwargs)  # 发起GET查询

    def list_all_releases(self, owner: str, repo: str, per_page: int = 100, **kwargs) -> Dict[str, Any]:
        """
        自动分页拉取仓库全部release元数据;接口出错会返回已经拉取到的release列表

        :param owner: 仓库拥有者
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param per_page: 每页条数,默认100
        :type per_page: int
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: {"all_releases":[...], "total_fetched":n, "page_count":n, "_has_token":bool};失败附带error
        :rtype: Dict[str, Any]
        :raises: 无
        """
        all_items: List[Dict] = []  # 累计已拉取的全部release
        current_page = 1  # 页码从第1页开始
        page_count = 0  # 已成功拉取的页数
        while True:  # 分页循环,遇到末页/错误退出
            resp = self.list_repo_releases(owner, repo, per_page=per_page, page=current_page,**kwargs)  # 请求当前页
            if "error" in resp:  # 本页请求失败
                return {  # 返回错误同时尽量保留已拉取数据
                    "error": resp["error"],  # 透传错误文本
                    "url": resp.get("url"),  # 失败URL
                    "status_code": resp.get("status_code"),  # HTTP状态码
                    "all_releases": all_items,  # 已累计的release
                    "total_fetched": len(all_items),  # 已拉取条数
                    "page_count": page_count,  # 已完成页数
                    "_has_token": self.has_token  # token标记
                }
            page_count +=1  # 本页成功,完成页数加1
            page_data: Any = resp  # 本页数据
            if not isinstance(page_data, list) or len(page_data) == 0:  # 非列表或空页表示已到末页
                break  # 结束分页
            all_items.extend(page_data)  # 将本页数据追加到累计列表
            if len(page_data) < per_page:  # 本页不足一页,说明后面无更多数据
                break  # 结束分页
            current_page += 1  # 页码加1继续拉取
        return {  # 全部页拉取完成,返回汇总结构
            "all_releases": all_items,  # 全部release列表
            "total_fetched": len(all_items),  # 总条数
            "page_count": page_count,  # 总页数
            "_has_token": self.has_token  # token标记
        }

    def get_release_by_tag(self, owner: str, repo: str, tag: str,** kwargs) -> Dict[str, Any]:
        """
        根据tag名称获取单条Release元数据
        内部调用 _normalize_release_assets 统一标准化assets

        :param owner: 仓库拥有者
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param tag: release tag版本号,例如 v0.0.01
        :type tag: str
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: 标准化后的release字典;失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        ep = f"/repos/{owner}/{repo}/releases/tags/{tag}"  # 拼接按tag查询的endpoint
        rel = self.api_get(ep,** kwargs)  # 发起GET查询
        if "error" in rel:  # 接口失败
            return rel  # 错误字典直接透传
        rel = self._normalize_release_assets(rel)  # 成功则统一标准化assets
        return rel  # 返回标准化release

    def find_asset_by_display_name(self, rel: Dict, target_name: str) -> Optional[Dict]:
        """
        根据【展示名/label/文件名】查找原始asset元数据对象
        :param rel: 经过 _normalize_release_assets() 标准化的release字典,必须包含 _raw_assets
        :type rel: Dict
        :param target_name: 本地/UI传入的展示名称(label中文名或实际文件名)
        :type target_name: str
        :return: 返回原始完整raw-asset字典;找不到返回None
        :rtype: Optional[Dict]
        :raises: 无
        """
        if not isinstance(target_name, str):  # 非字符串名称无法匹配
            return None  # 直接返回找不到
        target = target_name.strip()  # 去除目标名称首尾空白
        raw_assets = rel.get("_raw_assets", [])  # 取标准化时保存的原始资产列表
        for raw in raw_assets:  # 逐个比对原始asset
            disp = self._calc_display_name(raw).strip()  # 计算该asset的对外展示名(label优先)
            raw_name = raw.get("name", "").strip()  # 取原始文件名
            if disp == target or raw_name == target:  # 展示名或文件名任一匹配
                return raw  # 返回原始完整asset字典
        return None  # 遍历完仍未找到

    def get_asset_by_label(self, owner: str, repo: str, tag: str, target_display_name: str,** kwargs) -> Dict[str, Any]:
        """
        高层接口:根据【本地中文label展示名】直接获取对应原始asset完整元数据
        适用场景:本地配置写中文显示名,自动比对API的label,拿到真实browser_download_url、digest、sha256
        :param owner: 仓库owner
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param tag: release tag版本号,例如 v0.0.01
        :type tag: str
        :param target_display_name: 本地配置/UI传入的中文展示名(label),例"Mi3R-SPI-离线IPK插件包.tar.gz"
        :type target_display_name: str
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return:
            找到:{"ok":True, "asset":原始raw_asset字典}
            找不到 / 接口异常:{"ok":False,"error":"xxx"}
        :rtype: Dict[str, Any]
        :raises: 无
        """
        rel = self.get_release_by_tag(owner, repo, tag,** kwargs)  # 先按tag拉取并标准化release
        if "error" in rel:  # release拉取失败
            return {  # 返回统一失败结构
                "ok": False,  # 失败标记
                "error": f"获取release失败: {rel.get('error')}"  # 附带上游错误
            }
        asset_raw = self.find_asset_by_display_name(rel, target_display_name)  # 按展示名查找原始asset
        if asset_raw is None:  # 未匹配到asset
            return {  # 返回统一失败结构
                "ok": False,  # 失败标记
                "error": f"根据展示名[{target_display_name}]未匹配到对应asset"  # 提示未匹配的名称
            }
        return {  # 匹配成功
            "ok": True,  # 成功标记
            "asset": asset_raw  # 原始完整asset字典(含browser_download_url等)
        }

    def get_asset_by_latest_label(self, owner: str, repo: str, target_display_name: str,** kwargs) -> Dict[str, Any]:
        """
        根据latest最新release + 本地中文label展示名获取asset元数据

        :param owner: 仓库owner
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param target_display_name: 本地配置/UI传入的中文展示名(label)
        :type target_display_name: str
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: 找到返回{"ok":True,"asset":原始asset};否则{"ok":False,"error":...}
        :rtype: Dict[str, Any]
        :raises: 无
        """
        rel = self.get_latest_release(owner, repo,** kwargs)  # 先拉取最新正式版release
        if "error" in rel:  # latest拉取失败
            return {  # 返回统一失败结构
                "ok": False,  # 失败标记
                "error": f"获取latest release失败: {rel.get('error')}"  # 附带上游错误
            }
        asset_raw = self.find_asset_by_display_name(rel, target_display_name)  # 按展示名查找asset
        if asset_raw is None:  # 未匹配到
            return {  # 返回统一失败结构
                "ok": False,  # 失败标记
                "error": f"latest版本中,展示名[{target_display_name}]未匹配asset"  # 提示未匹配名称
            }
        return {  # 匹配成功
            "ok": True,  # 成功标记
            "asset": asset_raw  # 原始完整asset字典
        }

    def get_release_asset_detail(self, owner: str, repo: str, asset_id:int,** kwargs) -> Dict[str, Any]:
        """
        获取单个asset元数据详情

        :param owner: 仓库owner
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param asset_id: GitHub资产数字ID
        :type asset_id: int
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: 标准化后的单个asset字典;失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        ep = f"/repos/{owner}/{repo}/releases/assets/{asset_id}"  # 拼接单资产endpoint
        asset = self.api_get(ep,** kwargs)  # 发起GET查询
        if "error" not in asset:  # 查询成功才做字段标准化
            asset["_original_name"] = asset.get("name","")  # 备份GitHub原始name
            label_raw = asset.get("label","").strip()  # 取label去空白
            if label_raw:  # label非空
                asset["name"] = label_raw  # name替换为label展示名
            raw_digest = asset.get("digest")  # 取digest字段
            asset["sha256"] = self.extract_sha256_from_digest(raw_digest)  # 解析纯sha256
            if "label" in asset:  # 存在label字段
                del asset["label"]  # 对外删除label
        return asset  # 返回asset详情(成功为标准化字典,失败为错误字典)

    def get_latest_release(self, owner: str, repo: str,** kwargs) -> Dict[str, Any]:
        """获取仓库最新正式版release元数据(/releases/latest)
        ✅现在也会执行assets标准化:label优先、_raw_assets、_original_name、sha256

        :param owner: 仓库owner
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: 标准化后的最新release字典;失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        ep = f"/repos/{owner}/{repo}/releases/latest"  # 拼接latest endpoint
        rel = self.api_get(ep,** kwargs)  # 发起GET查询
        if "error" in rel:  # 接口失败
            return rel  # 错误字典直接透传
        rel = self._normalize_release_assets(rel)  # 成功则统一标准化assets
        return rel  # 返回标准化release

    def get_release_assets(self, owner: str, repo: str, release_id:int,** kwargs) -> Dict[str, Any]:
        """
        根据release_id获取该release下全部asset元数据列表

        :param owner: 仓库owner
        :type owner: str
        :param repo: 仓库名
        :type repo: str
        :param release_id: GitHub release数字ID
        :type release_id: int
        :param kwargs: 透传其他请求参数
        :type kwargs: Any
        :return: asset元数据列表(正常为list);失败包含error键
        :rtype: Dict[str, Any]
        :raises: 无
        """
        ep = f"/repos/{owner}/{repo}/releases/{release_id}/assets"  # 拼接release资产列表endpoint
        return self.api_get(ep,** kwargs)  # 发起GET查询并直接返回


if __name__ == "__main__":  # 直接运行本文件时执行元数据查询自测
    print("===== GitHubApiClient 元数据查询自测 =====")  # 打印自测标题
    token_str = None  # 匿名访问(可替换为真实GitHub token以提升限额/读取Draft)
    with GitHubApiClient(github_token=token_str, max_retries=1, timeout=20) as gh:  # 构造客户端并由with自动管理生命周期
        print(f"🔑 当前实例是否携带token: {gh.has_token}")  # 打印token携带状态
        rel = gh.get_release_by_tag("itwuge", "Mi3R-SPI-Openwrt", tag="v0.0.01")  # 查询指定tag的release
        if "error" in rel:  # 查询失败分支
            print(f"❌获取tag release失败:{rel}")  # 打印错误详情
        else:  # 查询成功分支
            print(f"✅tag_name: {rel['tag_name']}")  # 打印tag名称
            for asset in rel["assets"]:  # 遍历标准化后的资产
                print(f"展示name:{asset['name']}, sha256:{asset.get('sha256')}")  # 打印展示名与解析出的sha256
