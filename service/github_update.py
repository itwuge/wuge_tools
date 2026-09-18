# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: github_update.py
# 归属: service 业务服务层 —— GitHub Release 业务封装服务
# ------------------------------------------------------------------------------
# 文件用途:
#   GitHub Release 业务封装服务;调用底层 infrastructure.http_github.GitHubApiClient
#   获取最新 Release 元数据,并将底层标准化后的资产信息转换为上层业务
#   (版本检查、文件下载)所需的统一结构;当 GitHub Token 鉴权失败(HTTP 401)
#   时自动切换匿名模式重试一次,提升请求成功率.
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 service 业务服务层;对上(controller/worker)提供获取最新版本信息的
#   业务接口,对下依赖 infrastructure.http_github.GitHubApiClient 完成实际
#   API 调用;本模块仅做数据结构转换与鉴权降级重试,不做文件下载、不做磁盘操作.
#
#   关联组件:
#     - 上游: controller/worker(版本检查、文件下载)
#     - 下游: infrastructure.http_github.GitHubApiClient
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 获取仓库最新正式版 Release 元数据(版本号、发布说明、发布时间、资产列表)
#   2. Token 鉴权 401 时自动关闭旧客户端、重建匿名客户端重试一次
#   3. 将底层标准化资产转换为业务统一结构:url / display_name / original_name / sha256
#   4. 提供 with 上下文管理器,自动释放底层 HTTP 会话连接池
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅做数据结构转换与鉴权降级重试,不做文件下载、不做磁盘操作
#   - 401 降级时会销毁旧客户端并创建新匿名客户端,期间不涉及多线程共享
#   - 每次调用 get_latest_version_info 均使用实例持有的底层客户端
# ------------------------------------------------------------------------------
# 线程模型:
#   网络 IO 阻塞,务必在 QThread 子线程调用,禁止在 UI 主线程直接调用;
#   每次调用 get_latest_version_info 均使用实例持有的底层客户端,不创建新会话;
#   401 降级时会销毁旧客户端并创建新匿名客户端,期间不涉及多线程共享.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、typing
#   - 第三方: 无(底层依赖 requests,由 GitHubApiClient 间接引入)
#   - 项目内: infrastructure.http_github.GitHubApiClient
# ==============================================================================
import logging  # 标准库:记录版本查询过程的 info/debug/error 日志
from typing import Any, Dict, Optional  # 标准库:类型注解,Any 任意类型、Dict 字典、Optional 可空返回

from infrastructure.http_github import GitHubApiClient  # 项目内:底层 GitHub REST-API 元数据查询客户端


class ServiceGithubRelease:
    """
    GitHub Release 业务服务类,封装底层 GitHubApiClient,提供统一的最新版本查询接口

    工作流程:
        1. 使用构造时传入的 github_token 创建底层 GitHubApiClient
        2. 调用 get_latest_release 获取最新正式版 Release
        3. 若返回 HTTP 401(Token 鉴权失败):关闭旧客户端、用匿名 token 重建客户端并重试一次
        4. 将底层标准化后的 assets 列表转换为业务统一结构
        5. 返回包含版本号、发布说明、发布时间、资产下载列表的结果字典

    实例属性:
        repo_owner (str): GitHub 仓库所有者名称
        repo_name (str): GitHub 仓库项目名
        timeout (int): HTTP 请求超时秒数
        verify_ssl (bool): 是否校验 HTTPS SSL 证书
        proxies (Optional[Dict[str, str]]): 代理配置字典
        api_base_url (Optional[str]): GitHub API 根地址,可配置为兼容网关/镜像
        logger (logging.Logger): 日志对象
        _cli (GitHubApiClient): 底层 GitHub API 客户端实例
    """
    def __init__(
        self,
        repo_owner: str,
        repo_name: str,
        github_token: Optional[str] = None,
        timeout: int = 15,
        verify_ssl: bool = False,
        proxies: Optional[Dict[str, str]] = None,
        api_base_url: Optional[str] = None,
        logger=None
    ):
        """
        初始化 GitHub Release 业务服务实例

        :param repo_owner: GitHub 仓库所有者名称,例如 "itwuge"
        :type repo_owner: str
        :param repo_name: GitHub 仓库项目名,例如 "Mi3R-SPI-Openwrt"
        :type repo_name: str
        :param github_token: GitHub 个人访问 token;匿名访问传 None
        :type github_token: Optional[str]
        :param timeout: HTTP 请求超时秒数,默认 15 秒
        :type timeout: int
        :param verify_ssl: 是否校验 HTTPS SSL 证书,默认 False
        :type verify_ssl: bool
        :param proxies: 代理字典 {"http":"xxx","https":"xxx"},None 表示无代理
        :type proxies: Optional[Dict[str, str]]
        :param api_base_url: GitHub API 根地址,可配置为兼容网关/镜像;None 使用官方地址
        :type api_base_url: Optional[str]
        :param logger: 外部日志实例,None 时使用模块默认 logger
        :type logger: Any
        """
        self.repo_owner = repo_owner  # 保存仓库所有者名称
        self.repo_name = repo_name  # 保存仓库项目名
        self.timeout = timeout  # 保存请求超时秒数
        self.verify_ssl = verify_ssl  # 保存 SSL 证书校验开关
        self.proxies = proxies  # 保存代理配置字典
        self.api_base_url = api_base_url  # 保存 API 根地址
        self.logger = logger if logger else logging.getLogger(__name__)  # 未传 logger 时使用模块默认 logger
        self._cli = GitHubApiClient(  # 创建底层 GitHub API 客户端实例
            github_token=github_token,  # 传入 token(匿名为 None)
            timeout=timeout,  # 传入超时
            verify_ssl=verify_ssl,  # 传入 SSL 开关
            proxies=proxies,  # 传入代理
            api_base_url=api_base_url,  # 传入 API 根地址
            logger=self.logger  # 传入日志器
        )

    def get_latest_version_info(self) -> Dict[str, Any]:
        """
        获取仓库最新正式版 Release 信息

        处理逻辑:
            1. 调用底层 get_latest_release 拉取最新 Release
            2. 若返回 HTTP 401(Token 鉴权失败),自动切换匿名模式重试一次
            3. 成功时转换 assets 为业务统一结构并返回
            4. 失败时返回 ok=False 的错误结构

        :return: 统一结果字典,字段如下:
            - ok (bool): 是否获取成功
            - version_tag (str): 版本 tag 名称,失败时为 None
            - release_note (str): Release 发布说明正文
            - publish_time (str): 发布时间字符串
            - download_url_list (list): 资产下载列表,每项含 url/display_name/original_name/sha256
            - error (str | None): 错误信息,成功时为 None
        :rtype: Dict[str, Any]
        """
        self.logger.info(  # 记录版本查询起点与鉴权状态
            f"🌐 获取GitHub最新Release:{self.repo_owner}/{self.repo_name} "  # 日志第一行:仓库路径
            f"({'带Token' if self._cli.has_token else '匿名'})"  # 日志第二行:当前鉴权模式
        )
        raw_ret = self._cli.get_latest_release(self.repo_owner, self.repo_name)  # 调用底层获取最新 Release

        # token鉴权401,切换匿名模式重试
        if "error" in raw_ret and raw_ret.get("status_code") == 401:  # 判断是否为 Token 鉴权失败
            self.logger.warning("⚠ GitHub Token鉴权失败(401),切换匿名模式重新请求")  # 记录降级警告
            self._cli.close()  # 关闭旧的带 Token 客户端,释放连接池
            self._cli = GitHubApiClient(  # 重建匿名客户端(github_token=None)
                github_token=None,  # 匿名访问,不携带 Token
                timeout=self.timeout,  # 复用原超时配置
                verify_ssl=self.verify_ssl,  # 复用原 SSL 配置
                proxies=self.proxies,  # 复用原代理配置
                api_base_url=self.api_base_url,  # 复用原 API 根地址
                logger=self.logger  # 复用日志器
            )
            raw_ret = self._cli.get_latest_release(self.repo_owner, self.repo_name)  # 用匿名客户端重新请求

        if "error" in raw_ret:  # 底层返回错误(匿名重试后仍失败或其他错误)
            self.logger.error(f"❌ 获取Release失败:{raw_ret.get('error', 'unknown error')}")  # 记录错误详情
            return {  # 返回统一错误结构
                "ok": False,  # 标记失败
                "version_tag": None,  # 无版本号
                "release_note": "",  # 无发布说明
                "publish_time": "",  # 无发布时间
                "download_url_list": [],  # 空资产列表
                "error": raw_ret.get("error", "unknown error")  # 透传错误信息
            }

        assets = raw_ret.get("assets", [])  # 取出底层标准化后的资产列表
        dl_list = []  # 准备业务统一结构的资产列表
        for asset in assets:  # 遍历每个资产做结构转换
            # http_github 标准化后:name 保持实际文件名,display_name 存 label 展示名(可能是中文)
            # 业务层 display_name 用实际文件名(用于模板匹配和下载定位)
            # 业务层 label 用底层 display_name(中文展示名,供 UI 显示)
            actual_name = asset.get("name", "")  # GitHub API 实际文件名(标准化后不再被 label 覆盖)
            ui_label = asset.get("display_name", "")  # 底层 display_name 字段(label 展示名,可能中文)
            browser_url = asset.get("browser_download_url", "")  # 浏览器直接下载 URL(github.com 主站)
            asset_id = asset.get("id")  # GitHub 资产数字 ID,用于 API 下载接口
            # GitHub 的 browser_download_url 指向 github.com 主站(国内被墙/极慢,直连必超时),
            # 改用 api.github.com 资产下载接口:302 重定向到 release-assets CDN 签名直链
            # (可达且快,支持 Range 分片);下载器内部会对该 URL 预解析一次签名直链
            api_dl_url = ""
            if asset_id and self.repo_owner and self.repo_name:
                api_dl_url = (f"https://api.github.com/repos/{self.repo_owner}/"
                              f"{self.repo_name}/releases/assets/{asset_id}")
            dl_list.append({  # 组装业务统一结构
                "url": api_dl_url or browser_url,  # 优先 API 资产下载接口(302→CDN直链)
                "display_name": actual_name,  # 实际文件名(用于模板匹配和下载)
                "label": ui_label,  # UI 展示名(label 中文名,无 label 时与 display_name 相同)
                "original_name": asset.get("_original_name", ""),  # 向后兼容字段
                "sha256": asset.get("sha256"),  # 解析出的 sha256 哈希(可能为 None)
                "asset_id": asset_id  # 资产数字 ID(api 下载接口用,可能为 None)
            })

        self.logger.info(  # 记录获取成功的汇总信息
            f"✅ Release获取成功:tag={raw_ret.get('tag_name', '')},"  # 日志第一行:版本 tag
            f"发布时间={raw_ret.get('published_at', '')},资产{len(dl_list)}个"  # 日志第二行:发布时间与资产数
        )
        return {  # 返回统一成功结构
            "ok": True,  # 标记成功
            "version_tag": raw_ret.get("tag_name", ""),  # 版本 tag 名称
            "release_note": raw_ret.get("body", ""),  # Release 正文发布说明
            "publish_time": raw_ret.get("published_at", ""),  # ISO 格式发布时间
            "download_url_list": dl_list,  # 转换后的资产下载列表
            "error": None  # 无错误
        }

    def close(self):
        """关闭底层 GitHub API 客户端,释放 HTTP 连接池资源"""
        if hasattr(self, "_cli"):  # 防御:确保 _cli 属性存在(避免 __init__ 异常场景报错)
            self._cli.close()  # 关闭底层客户端会话

    def __enter__(self):
        """with 上下文管理器入口,返回自身实例供 as 变量绑定"""
        return self  # 返回自身实例

    def __exit__(self, exc_type, exc_val, exc_tb):
        """with 上下文管理器出口,无论是否异常都释放底层资源"""
        self.close()  # 退出时关闭底层客户端
