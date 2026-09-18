#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: file_downloade.py
# 归属: service 业务服务层 —— 文件下载与上传业务层封装
# ------------------------------------------------------------------------------
# 文件用途:
#   文件下载与上传的业务层封装;对底层 infrastructure.file_transfer.FileDownloader /
#   FileUploader 做轻量包装,透传进度回调与日志对象,对上(controller/worker)
#   提供统一的业务接口;本层不侵入底层分片/断点续传逻辑,仅做参数透传与结果日志留痕.
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 service 业务服务层;对上提供 ServiceFileDownloader / ServiceFileUploader
#   业务类,对下依赖 infrastructure.file_transfer 的 FileDownloader / FileUploader
#   完成实际传输;本层职责单一:参数透传、日志留痕、生命周期管理(with 上下文).
#
#   关联组件:
#     - 上游: controller/worker
#     - 下游: infrastructure.file_transfer.FileDownloader、FileUploader
# ------------------------------------------------------------------------------
# 核心功能:
#   [ServiceFileDownloader]业务下载封装
#   1. 构造时透传代理/超时/分片大小/并发数/日志/进度回调/取消事件到底层 FileDownloader
#   2. download() 委托底层执行下载,完成后 DEBUG 级别记录业务层返回值摘要
#   3. 提供 set_progress_callback 运行时切换进度回调
#   4. with 上下文自动释放底层会话
#   [ServiceFileUploader]业务上传封装(TUS-v1.0)
#   1. 构造时透传代理/超时/分片大小/并发数/日志/进度回调到底层 FileUploader
#   2. upload_file() 委托底层执行 TUS 断点分片上传,完成后 INFO 级别记录结果摘要
#   3. with 上下文自动释放底层会话
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅做参数透传与日志留痕,不侵入底层下载上传逻辑
#   - 上传依赖服务端必须支持 TUS-v1.0 协议
#   - 进度回调在工作线程中触发,若回调操作 UI 控件需调用方自行保证线程安全
# ------------------------------------------------------------------------------
# 线程模型:
#   网络 IO 与大文件读写阻塞,务必在 QThread 子线程调用,禁止在 UI 主线程直接调用;
#   进度回调在工作线程中触发,若回调操作 UI 控件需调用方自行保证线程安全;
#   每个实例持有独立底层传输组件,不同实例之间完全独立.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: typing
#   - 第三方: 无(底层依赖 requests)
#   - 项目内: infrastructure.file_transfer.FileDownloader、
#             infrastructure.file_transfer.FileUploader
# ==============================================================================
from typing import Dict, Any, Optional, Callable  # 标准库:类型注解,Dict字典/Any任意/Optional可空/Callable回调
from infrastructure.file_transfer import FileDownloader, FileUploader  # 项目内:底层文件下载器与上传器


class ServiceFileDownloader:
    """
    Service 业务下载封装类,面向 GUI/上层调用,封装底层 FileDownloader

    职责:
        - 构造时将代理/超时/分片/并发/日志/进度回调/取消事件透传到底层 FileDownloader
        - download() 直接委托底层执行,不修改下载逻辑
        - 完成后仅以 DEBUG 级别记录业务层返回值摘要(底层已输出详细完成日志)
        - 提供 set_progress_callback 支持运行时切换进度回调函数

    实例属性:
        _inner_downloader (FileDownloader): 底层文件下载器实例
        logger: 外部注入的日志对象
    """
    def __init__(
        self,
        proxies: Optional[Dict[str, str]] = None,
        timeout: int = 15,
        verify_ssl: bool = False,
        chunk_size: int = 1024 * 1024,
        max_workers: int = 4,
        logger=None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_event=None,
        request_headers: Optional[Dict[str, str]] = None,
    ):
        """
        初始化业务下载封装,创建底层 FileDownloader 实例

        :param proxies: 代理配置字典 {"http":"xxx","https":"xxx"};从 app_config 读取配置后组装传入
        :type proxies: Optional[Dict[str, str]]
        :param timeout: HTTP 请求超时秒数,默认 15 秒
        :type timeout: int
        :param verify_ssl: 是否校验 HTTPS SSL 证书,默认 False
        :type verify_ssl: bool
        :param chunk_size: 多线程分片下载单块字节大小,默认 1MB
        :type chunk_size: int
        :param max_workers: 多线程下载最大并发线程数,默认 4
        :type max_workers: int
        :param logger: 日志实例(具备 debug/info/warning/error 方法),None 关闭日志
        :type logger: Any
        :param progress_callback: 进度回调函数,签名 (current_bytes:int, total_bytes:int),None 不回调
        :type progress_callback: Optional[Callable[[int, int], None]]
        :param cancel_event: threading.Event 取消事件,外部 set 后下载在当前 chunk 边界尽快退出
        :type cancel_event: threading.Event | None
        :param request_headers: 透传到底层HEAD/GET请求的共用请求头,可用于GitHub Token认证
        :type request_headers: Optional[Dict[str, str]]
        """
        self._inner_downloader = FileDownloader(  # 创建底层文件下载器实例
            proxies=proxies,  # 透传代理配置
            timeout=timeout,  # 透传超时
            verify_ssl=verify_ssl,  # 透传 SSL 开关
            chunk_size=chunk_size,  # 透传分片大小
            max_workers=max_workers,  # 透传并发数
            logger=logger,  # 透传日志器
            progress_callback=progress_callback,  # 透传进度回调
            cancel_event=cancel_event,  # 透传取消事件
            request_headers=request_headers,  # 透传GitHub Token等共用请求头
        )
        self.logger = logger  # 保存日志器引用,供业务层日志留痕使用

    def set_progress_callback(self, callback):
        """
        运行时设置/更换实时进度回调函数

        :param callback: 进度回调函数,签名 (current_bytes:int, total_bytes:int);传 None 可取消回调
        :type callback: Callable[[int, int], None] | None
        """
        self._inner_downloader.progress_callback = callback  # 直接替换底层下载器的进度回调属性

    def download(
        self,
        download_url: str,
        save_dir: str,
        display_name: str,
        expect_sha256: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        业务层下载入口,委托底层 FileDownloader.download 执行

        :param download_url: 文件完整 HTTP 下载地址
        :type download_url: str
        :param save_dir: 文件保存目标目录,不存在时底层自动创建
        :type save_dir: str
        :param display_name: UI 展示文件名,底层自动清洗为磁盘安全文件名
        :type display_name: str
        :param expect_sha256: 预期 sha256 哈希字符串,None 表示跳过哈希校验
        :type expect_sha256: Optional[str]
        :return: 标准化下载结果字典(详见 FileDownloader.download 文档)
        :rtype: Dict[str, Any]
        """
        result = self._inner_downloader.download(  # 委托底层执行实际下载
            download_url=download_url,  # 透传下载 URL
            save_dir=save_dir,  # 透传保存目录
            display_name=display_name,  # 透传展示文件名
            expect_sha256=expect_sha256  # 透传期望哈希
        )
        if self.logger:  # 仅在注入日志器时输出业务层摘要
            # 底层 FileDownloader 已输出 🎉 完成汇总,这里仅 DEBUG 留业务层返回值
            self.logger.debug(  # DEBUG 级别记录业务层返回值摘要
                f"🔗 [ServiceFileDownloader]业务层返回 success={result['success']}, "  # 下载是否成功
                f"resume={result['resume_used']}, mode={result['used_mode']}, sha_ok={result['sha256_ok']}"  # 续传/模式/哈希
            )
        return result  # 返回底层标准化下载结果

    def close(self):
        """关闭底层下载器,释放 HTTP 连接池资源"""
        self._inner_downloader.close()  # 调用底层 close 释放会话

    def __enter__(self):
        """with 上下文管理器入口,返回自身实例"""
        return self  # 返回自身实例供 as 变量绑定

    def __exit__(self, exc_type, exc_val, exc_tb):
        """with 上下文管理器出口,无论是否异常都释放底层资源"""
        self.close()  # 退出时关闭底层下载器


class ServiceFileUploader:
    """
    Service 业务上传封装类,TUS-v1.0 多线程断点分片上传,封装底层 FileUploader

    ⚠️ 重要:服务端必须实现 TUS v1.0 协议,普通表单上传不适用本类.

    职责:
        - 构造时将代理/超时/分片大小/并发数/日志/进度回调透传到底层 FileUploader
        - upload_file() 委托底层执行 TUS 断点分片上传
        - 完成后以 INFO 级别记录上传结果摘要
        - with 上下文自动释放底层会话

    实例属性:
        _inner_uploader (FileUploader): 底层 TUS 文件上传器实例
        logger: 外部注入的日志对象
    """
    def __init__(
        self,
        proxies: Optional[Dict[str, str]] = None,
        timeout: int = 30,
        verify_ssl: bool = False,
        chunk_size: int = 5 * 1024 * 1024,
        max_workers: int = 3,
        logger=None,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ):
        """
        初始化业务上传封装,创建底层 FileUploader 实例

        :param proxies: 代理字典;上层从 app_config 读取 proxy 配置组装后传入
        :type proxies: Optional[Dict[str, str]]
        :param timeout: HTTP 请求超时秒数,上传建议 30 秒
        :type timeout: int
        :param verify_ssl: 是否校验 HTTPS SSL 证书
        :type verify_ssl: bool
        :param chunk_size: 单个上传分片字节大小,默认 5MB
        :type chunk_size: int
        :param max_workers: 分片上传最大并发线程数,默认 3
        :type max_workers: int
        :param logger: 日志实例,None 关闭日志
        :type logger: Any
        :param progress_callback: 上传进度回调 (uploaded_bytes:int, total_bytes:int),None 不回调
        :type progress_callback: Optional[Callable[[int, int], None]]
        """
        self._inner_uploader = FileUploader(  # 创建底层 TUS 文件上传器实例
            proxies=proxies,  # 透传代理配置
            timeout=timeout,  # 透传超时
            verify_ssl=verify_ssl,  # 透传 SSL 开关
            chunk_size=chunk_size,  # 透传分片大小
            max_workers=max_workers,  # 透传并发数
            logger=logger,  # 透传日志器
            progress_callback=progress_callback  # 透传进度回调
        )
        self.logger = logger  # 保存日志器引用

    def upload_file(
        self,
        tus_create_endpoint: str,
        local_file_path: str,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        业务层上传入口,委托底层 FileUploader.upload_file 执行 TUS 断点分片上传

        :param tus_create_endpoint: TUS 服务创建资源接口 URL
        :type tus_create_endpoint: str
        :param local_file_path: 本地待上传文件的完整路径
        :type local_file_path: str
        :param metadata: 附加元数据字典,值会被 base64 编码后拼入 Upload-Metadata 头
        :type metadata: Optional[Dict]
        :return: 标准化上传结果字典(详见 FileUploader.upload_file 文档)
        :rtype: Dict[str, Any]
        """
        result = self._inner_uploader.upload_file(  # 委托底层执行实际 TUS 上传
            tus_create_endpoint=tus_create_endpoint,  # 透传 TUS 创建端点
            local_file_path=local_file_path,  # 透传本地文件路径
            metadata=metadata  # 透传附加元数据
        )
        if self.logger:  # 仅在注入日志器时输出业务层摘要
            self.logger.info(  # INFO 级别记录上传结果摘要
                f"[ServiceFileUploader] 上传完成, success={result['success']}, "  # 上传是否成功
                f"finished_chunk={result['finished_chunk_count']}/{result['total_chunk_count']}, error={result['error']}"  # 分片进度与错误
            )
        return result  # 返回底层标准化上传结果

    def close(self):
        """关闭底层上传器,释放 HTTP 连接池资源"""
        self._inner_uploader.close()  # 调用底层 close 释放会话

    def __enter__(self):
        """with 上下文管理器入口,返回自身实例"""
        return self  # 返回自身实例供 as 变量绑定

    def __exit__(self, exc_type, exc_val, exc_tb):
        """with 上下文管理器出口,无论是否异常都释放底层资源"""
        self.close()  # 退出时关闭底层上传器


# 模块公开 API 符号表:控制 from service.file_downloade import * 的导出范围
__all__ = ["ServiceFileDownloader", "ServiceFileUploader"]  # 仅导出两个业务服务类
