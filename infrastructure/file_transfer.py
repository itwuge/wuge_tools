#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: file_transfer.py
# 归属: infrastructure 基础设施层 —— 文件传输客户端(下载/上传)
# ------------------------------------------------------------------------------
# 文件用途:
#   文件传输客户端组件,拆分为 HTTP 文件下载器(FileDownloader)与 TUS-v1.0
#   断点分片上传器(FileUploader);负责大文件多线程分片下载/上传、断点续传、
#   线程安全进度回调、下载后 sha256 完整性校验以及 Windows 非法字符文件名清洗.
# ------------------------------------------------------------------------------
# 架构定位:
#   infrastructure 基础设施层通用能力;被 runtime/workers/file_transfer_worker
#   后台工作线程与 controller 控制层调用;仅依赖标准库与第三方
#   requests/urllib3,不反向依赖任何业务层.
#
#   关联组件:
#     - 上游: runtime/workers/file_transfer_worker、controller
#     - 下游: requests、urllib3(HTTP 传输)
# ------------------------------------------------------------------------------
# 核心功能:
#   [FileDownloader]文件下载器
#   1. HEAD 探测服务器是否支持 Range 分块、获取文件总大小
#   2. 优先多线程分片下载;分片异常时多线程内部重试(最多3次),不降级单线程
#   3. 内部做 Windows 非法字符文件名清洗
#   4. 下载完成本地文件 sha256 完整性校验
#   5. 返回标准化下载结果,标记实际使用模式 multithread / single_thread
#   6. 多线程模式内部线程安全计数,支持平滑进度回调
#   [FileUploader]TUS-v1.0 多线程断点分片上传器
#   1. 本地大文件分片切割
#   2. 查询服务端已上传分片,支持断点续传
#   3. 多线程并发上传分片块,线程安全上传进度回调
#   4. 全部分片上传完成发送合并请求完成上传
#   5. with 上下文自动释放 session 资源
#   注意:上传依赖服务端必须实现 TUS-v1.0 协议;普通表单上传不适用本断点上传类
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责文件传输(下载/上传),不涉及业务逻辑
#   - 下载返回标准化字典(success/save_path/local_filename/total_bytes/sha256_ok/used_mode/error)
#   - 上传返回标准化字典(success/upload_url/total_bytes/uploaded_bytes/error)
# ------------------------------------------------------------------------------
# 线程模型:
#   基于 requests 的 HTTP/HTTPS 短连接(非裸 socket);下载与上传均使用
#   ThreadPoolExecutor 线程池并发收发分片,以 list[0] 作为共享字节计数器并配合
#   threading.Lock 保证进度统计线程安全;每个组件内部持有独立 requests.Session
#   复用 TCP 连接池;取消机制通过外部 threading.Event 由工作线程在 chunk 边界
#   轮询实现.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: base64、hashlib、os、threading、concurrent.futures
#             (ThreadPoolExecutor, as_completed)、typing
#   - 第三方: requests、urllib3
#   - 项目内: 无(纯基础设施,不依赖业务层)
# ==============================================================================

import json  # JSON序列化:分片位图meta持久化,支撑大文件精确断点续传
import base64  # base64编解码:TUS协议规定 Upload-Metadata 头的值必须为 base64(UTF-8字节)
import hashlib  # 安全哈希库:用于计算下载文件的 sha256 摘要做完整性校验
import os  # 操作系统接口:路径拼接、目录创建、文件存在/大小查询、删除文件等
import time  # 时间戳:分片测速(自适应并发档位)
import threading  # 线程同步原语:Lock 保护共享计数器、Event 实现外部取消
from concurrent.futures import ThreadPoolExecutor, wait  # 线程池执行器并发收发分片;wait按完成事件动态补提交(自适应并发)
from typing import Any, Callable, Dict, List, Optional, Tuple  # 类型注解:Any任意类型、Callable回调、Dict/List/Optional/Tuple容器

import requests  # 第三方HTTP客户端:Session会话、GET/HEAD/POST/PATCH、stream流式下载
from requests.adapters import HTTPAdapter  # HTTP连接池适配器:调大池大小支持高并发分片下载
import urllib3  # requests底层依赖库;此处仅用于关闭SSL证书不安全警告

# 走代理时统一关闭SSL证书验证(见_effective_verify),抑制对应的InsecureRequestWarning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 全局抑制verify=False时刷屏的InsecureRequestWarning


def _effective_verify(verify_ssl: bool, proxies: Dict[str, str]) -> bool:
    """代理与证书联动:配置非空代理时关闭证书验证;无代理时使用verify_ssl原值

    :param verify_ssl: 调用方配置的SSL证书校验开关
    :type verify_ssl: bool
    :param proxies: 代理配置字典;为空字典(假值)表示未走代理
    :type proxies: Dict[str, str]
    :return: 实际传给requests的verify参数——有代理恒为False,无代理返回verify_ssl原值
    :rtype: bool
    """
    # 三元表达式:proxies非空(走代理,常见抓包代理自签证书)时强制False,否则沿用调用方开关
    return False if proxies else verify_ssl


def _b64_encode(text: str) -> str:
    """TUS协议要求metadata值必须base64编码(UTF-8字节)

    :param text: 待编码的原始文本(如文件名、自定义元数据值)
    :type text: str
    :return: base64编码后的ASCII字符串,可直接拼入Upload-Metadata请求头
    :rtype: str
    """
    # 先按UTF-8编码成字节再做base64,最后解码为ASCII字符串(TUS请求头只接受ASCII)
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _safe_local_filename(raw_name: str) -> str:
    """
    公共静态工具:清洗Windows非法文件名字符,返回磁盘安全文件名
    :param raw_name: 原始展示名字符串
    :type raw_name: str
    :return: 清洗后可直接用于磁盘保存的文件名
    :rtype: str
    """
    if not raw_name:  # None或空字符串等假值无法生成文件名
        return "unknown_file"  # 兜底固定文件名,保证磁盘写入始终有合法名字
    bad_chars = r'\/:*?"<>|'  # Windows文件名9个非法字符(\ / : * ? " < > |);原始字符串避免反斜杠转义
    s = raw_name  # 拷贝到局部变量,逐字符替换处理
    for c in bad_chars:  # 遍历每一个非法字符
        s = s.replace(c, "_")  # 将该非法字符统一替换为下划线
    return s  # 返回清洗后的磁盘安全文件名


def _calc_file_sha256(file_path: str) -> str:
    """
    公共静态工具:分块读取本地大文件,计算sha256哈希值
    :param file_path: 本地文件完整路径
    :type file_path: str
    :return: sha256十六进制小写哈希字符串
    :rtype: str
    :raises OSError: 文件不存在或无读取权限时由open/read抛出
    """
    h = hashlib.sha256()  # 创建空的sha256哈希对象,后续用update分块喂入数据
    with open(file_path, "rb") as f:  # 二进制只读打开;with语句保证文件描述符自动关闭
        while chunk := f.read(65536):  # 海象运算符:每次读64KB,读到EOF空字节b''时循环结束
            h.update(chunk)  # 将本块字节并入哈希状态(分块读取避免大文件一次性占满内存)
    return h.hexdigest()  # 输出64位十六进制小写摘要字符串


def _build_chunks(total_size: int, base_chunk: int) -> List[list]:
    """按自适应分片大小构建分片位图列表 [[s,e,ok],...];大文件自动放大分片,限制分片数<=512"""
    piece = max(int(base_chunk), 1)  # 基础分片大小(默认1MB),至少1字节
    piece_count = (total_size + piece - 1) // piece  # 按基础大小估算分片数
    max_pieces = 128  # 目标分片数上限(16并发*8批缓冲):超过则放大分片,减少请求/慢启动/调度开销
    if piece_count > max_pieces:  # 分片过多:放大分片,让中大型文件自动用大分片
        piece = max(piece, (total_size + max_pieces - 1) // max_pieces)  # 按128片上限反推分片大小
    chunks: List[list] = []  # 分片位图列表
    start = 0  # 分片游标
    while start < total_size:  # 游标未到文件尾持续切片
        end = min(start + piece - 1, total_size - 1)  # 本片结尾,最后一片收敛到文件末
        chunks.append([start, end, False])  # 初始全部未完成
        start = end + 1  # 游标前进
    return chunks  # 返回位图


def _load_meta(meta_path: str) -> Optional[Dict[str, Any]]:
    """读取分片位图meta;文件缺失/损坏/结构非法时返回None(调用方将全量重建)"""
    try:  # 读取与解析可能因文件缺失/JSON损坏抛异常
        with open(meta_path, "r", encoding="utf-8") as f:  # 读meta文件
            data = json.load(f)  # 解析JSON
        if not isinstance(data, dict):  # 结构必须为字典
            return None
        chunks = data.get("chunks")  # 取分片列表
        if not isinstance(chunks, list) or not chunks:  # 分片列表必须非空
            return None
        for item in chunks:  # 校验每个分片结构
            if not (isinstance(item, list) and len(item) == 3):  # 必须为[s,e,ok]三元组
                return None
        return data  # 校验通过返回meta
    except Exception:  # 任何异常(缺文件/坏JSON/结构错)都按无meta处理
        return None


def _save_meta(meta_path: str, total_size: int, chunks: List[list]) -> None:
    """原子落盘分片位图(写临时文件后os.replace),避免进程崩溃留下半写JSON"""
    tmp = meta_path + ".tmp"  # 临时文件路径(同目录保证同卷原子替换)
    try:  # 写盘失败不应中断下载主流程,仅尽力而为
        with open(tmp, "w", encoding="utf-8") as f:  # 写临时文件
            json.dump({"total": total_size, "chunks": chunks}, f, ensure_ascii=False)  # 序列化位图
        os.replace(tmp, meta_path)  # 原子替换,旧meta被完整覆盖
    except OSError:  # IO失败(磁盘满/权限等):保留内存位图继续下载,中断后最多失去本次标记
        pass


def _drop_meta(meta_path: str) -> None:
    """删除分片位图文件(下载完成/全量重建时调用);不存在时静默忽略"""
    try:  # 删除失败不影响主流程
        if os.path.exists(meta_path):  # 存在才删除
            os.unlink(meta_path)  # 删除位图
    except OSError:  # 删除失败(占用/权限):忽略,下次加载时会被total校验兜住
        pass



class FileDownloader:
    """
    文件下载器:支持HTTP-Range标准断点续传;优先多线程分片,异常时多线程重试(最多3次)不降级;带sha256完整性校验
    仅接收基础类型参数:url、字符串名称、哈希字符串;不传入业务dict结构体

    工作流程:
        1. HEAD 探测服务端文件大小与 Range 支持情况
        2. 本地已有文件且大小足够 → 直接校验 sha256,通过则"秒传"返回
        3. 支持 Range 且本地有残文件 → 断点续传,从已有字节后继续
        4. 支持 Range → 多线程分片并发下载(线程池+共享计数器+锁)
        5. 多线程分片异常 → 多线程重试(最多3次,重试全量重下);仅不支持 Range 时单线程全量兜底
        6. 下载完成 → 计算 sha256 并与期望值比对(若提供)
        7. 全程可通过 cancel_event 外部取消,chunk 边界尽快退出

    线程安全说明:
        - progress_callback 在工作线程中触发,若回调操作 UI 控件需调用方自行保证线程安全
        - cancel_event 由外部线程 set、内部工作线程 is_set() 轮询,Event 本身线程安全
        - 多线程下载时共享计数器 list[0] 由 threading.Lock 保护累加操作
        - 每个 FileDownloader 实例持有独立 session,不同实例之间完全独立

    实例属性(全部可在 __init__ 中配置):
        proxies (Dict[str, str]): 代理配置字典,键为协议(http/https),值为代理URL
        timeout (int): 单次 HTTP 请求超时秒数,默认 15 秒
        verify_ssl (bool): 是否校验 SSL 证书,默认 False;走代理时被强制关闭
        chunk_size (int): 多线程分片下载单块字节大小,默认 1MB
        max_workers (int): 多线程下载最大并发线程数,默认 4
        logger (logging.Logger | None): 外部注入的日志对象,None 则不输出任何日志
        progress_callback (Callable[[int, int], None] | None): 进度回调函数,签名(已下载字节, 总字节)
        cancel_event (threading.Event | None): 外部取消事件对象,set 后下载在 chunk 边界尽快退出
        session (requests.Session): 组件内部独立 requests 会话,复用 TCP 连接池
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
        cancel_event: Optional[threading.Event] = None,
        request_headers: Optional[Dict[str, str]] = None,
    ):
        """
        创建FileDownloader实例
        :param proxies: 代理字典 {"http":"xxx","https":"xxx"},None代表无代理
        :type proxies: Optional[Dict[str, str]]
        :param timeout: http请求超时,单位秒
        :type timeout: int
        :param verify_ssl: 是否校验HTTPS SSL证书
        :type verify_ssl: bool
        :param chunk_size: 分片下载每块字节大小,默认1MB
        :type chunk_size: int
        :param max_workers: 多线程下载最大并发工作线程数量
        :type max_workers: int
        :param logger: 外部日志实例,None关闭日志输出
        :type logger: logging.Logger | None
        :param progress_callback: 进度回调 (current_bytes:int, total_bytes:int), None不回调
            注意:多线程下载时由工作线程触发,若回调操作UI控件需调用方自行保证线程安全
        :type progress_callback: Optional[Callable[[int, int], None]]
        :param cancel_event: 取消事件,外部set后下载循环在当前chunk边界尽快退出;
            注意:事件由工作线程轮询、外部线程设置,调用方无需自行加锁
        :type cancel_event: Optional[threading.Event]
        :param request_headers: 所有HEAD/GET请求共用的附加请求头,例如GitHub Token认证头
        :type request_headers: Optional[Dict[str, str]]
        """
        self.proxies: Dict[str, str] = proxies if proxies is not None else {}  # None归一化为空字典,便于直接bool判断是否有代理
        self.timeout: int = timeout  # 单次HTTP请求超时秒数
        self.verify_ssl: bool = verify_ssl  # SSL证书校验开关(实际是否生效还要看是否走代理)
        self.chunk_size: int = chunk_size  # 多线程模式下每个分片的字节大小
        self.max_workers: int = max_workers  # 线程池最大并发工作线程数
        self.logger = logger  # 外部注入日志器,None表示静默不打日志
        self.progress_callback = progress_callback  # 进度回调函数,签名(已下载字节, 总字节)
        self.cancel_event: Optional[threading.Event] = cancel_event  # 外部取消事件,工作线程轮询其set状态
        self.request_headers: Dict[str, str] = dict(request_headers or {})  # 复制共用请求头,避免修改调用方字典
        self.session = requests.Session()  # 组件私有requests会话,多次请求复用底层TCP连接池
        # GitHub release 资产下载对缺 UA / 非浏览器 UA 的请求会返回 403(尤其重定向到
        # release-assets.githubusercontent.com 后),调用方未显式提供 UA 时注入浏览器 UA
        if "User-Agent" not in self.request_headers:  # 仅在调用方未自定义 UA 时补默认
            self.session.headers["User-Agent"] = (  # 兼容 GitHub/CDN 的浏览器 UA
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            )
        # 调大连接池:urllib3 默认单主机池大小 10,16 线程并发下载会触发
        # "Connection pool is full, discarding connection" 警告并拖慢速度
        _adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=0)  # 32主机池/单主机64连接,重试由上层控制
        self.session.mount("https://", _adapter)  # HTTPS 走大池
        self.session.mount("http://", _adapter)  # HTTP 走大池

    def _is_cancelled(self) -> bool:
        """是否已收到外部取消信号

        :return: 注入了cancel_event且其被set时返回True;未注入或未set返回False
        :rtype: bool
        """
        # 短路求值:未注入事件视为永不取消;注入则读取Event内部布尔标志(Event本身线程安全,无需加锁)
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _verify_complete_file(self, save_path: str, total_size: int, expect_sha256: Optional[str],
                              local_exist_bytes: int, resume_used: bool, used_mode: Optional[str]):
        """本地文件已完整时统一走秒传校验:计算哈希、比对期望值并返回结果字典.

        哈希不符时删除损坏文件并返回 None,调用方继续走重建/重新下载路径.

        :param save_path: 已完整的本地文件路径
        :type save_path: str
        :param total_size: 服务端文件总字节
        :type total_size: int
        :param expect_sha256: 期望哈希;None 表示跳过比对
        :type expect_sha256: Optional[str]
        :param local_exist_bytes: 调用前本地已有字节数(进度语义)
        :type local_exist_bytes: int
        :param resume_used: 本次是否发生过续传(进度语义)
        :type resume_used: bool
        :param used_mode: 实际下载模式标记(秒传/位图全完成场景如实记录)
        :type used_mode: Optional[str]
        :return: 成功结果字典;哈希不符已删文件时返回 None
        :rtype: Optional[Dict[str, Any]]
        """
        calc_sha256: Optional[str] = None  # 本地实际算出的哈希,先置空
        sha256_ok = True  # 校验结论默认通过(未提供期望哈希时恒为True)
        if os.path.exists(save_path):  # 防御性二次确认文件确实存在
            calc_sha256 = _calc_file_sha256(save_path)  # 计算本地完整文件sha256
            if expect_sha256 is not None:  # 调用方提供期望哈希才做比对,否则视为通过
                sha256_ok = (calc_sha256.lower() == expect_sha256.lower())  # 双方转小写后严格比对
                if not sha256_ok:  # 字节数够但哈希不符:本地文件内容损坏
                    if self.logger:  # 记录哈希不符详情,便于排查
                        self.logger.warning(  # 输出期望/实际哈希对照警告
                            f"⚠ 本地文件哈希不匹配,删除后重新下载:{os.path.basename(save_path)} "  # 警告第一行:文件名
                            f"expect={expect_sha256}, actual={calc_sha256}"  # 警告第二行:期望与实际哈希
                        )
                    os.unlink(save_path)  # 删除损坏文件
                    return None  # 返回空,调用方继续重建/重下
        return {  # 文件完整且校验通过(或未要求校验),直接成功返回,省掉重复下载
            "success": True,  # IO成功(文件已在磁盘且有效)
            "save_path": save_path,  # 完整本地路径
            "local_filename": os.path.basename(save_path),  # 磁盘文件名
            "total_bytes": total_size,  # 服务端总字节
            "local_exist_bytes": local_exist_bytes,  # 本地已有字节
            "resume_used": resume_used,  # 是否曾续传
            "calc_sha256": calc_sha256,  # 本地实算哈希(可能为None)
            "expect_sha256": expect_sha256,  # 期望哈希
            "sha256_ok": sha256_ok,  # 校验结论
            "used_mode": used_mode,  # 实际模式(秒传/位图全完成如实记录)
            "error": None  # 无错误
        }

    def _download_chunk(self, url: str, start: int, end: int, temp_file_path: str,
                        total_size: int, shared_counter: List[int], lock: threading.Lock):
        """
        私有线程任务:单一分块Range下载函数,stream流式读取,统计实时下载字节
        :param url: 文件下载url
        :type url: str
        :param start: 分片起始字节偏移
        :type start: int
        :param end: 分片结束字节偏移
        :type end: int
        :param temp_file_path: 本地文件路径,r+b模式seek到对应位置写入
        :type temp_file_path: str
        :param total_size: 文件总字节
        :type total_size: int
        :param shared_counter: 线程间共享计数器 list[0]存放已下载字节
        :type shared_counter: List[int]
        :param lock: threading.Lock 线程锁
        :type lock: threading.Lock
        :return: 无显式返回;成功时分片字节已写入文件指定区间
        :rtype: None
        :raises RuntimeError: 下载过程中外部设置取消事件时抛出"下载已被取消"
        :raises requests.HTTPError: 服务端返回4xx/5xx时由raise_for_status抛出
        """
        headers = dict(self.request_headers)  # 复制认证等共用请求头,避免并发线程互相修改
        headers["Range"] = f"bytes={start}-{end}"  # 追加本分片Range闭区间请求头
        resp = self.session.get(  # 通过复用会话发起GET;stream=True时请求头返回后body不会立即下载
            url,  # 文件下载地址
            headers=headers,  # 携带Range头,服务端支持时返回206 Partial Content
            proxies=self.proxies,  # 代理配置
            timeout=self.timeout,  # 请求超时
            verify=_effective_verify(self.verify_ssl, self.proxies),  # 经代理/证书联动计算后的实际verify值
            stream=True  # 开启流式模式:响应体按需iter_content读取,避免整个分片进入内存
        )
        try:
            resp.raise_for_status()  # 状态码>=400抛HTTPError;200/206均视为正常
            with open(temp_file_path, "r+b") as f:  # 读写二进制打开已预分配文件;多线程共享同一文件描述符各自seek
                f.seek(start)  # 文件写指针定位到本分片起始偏移,保证各线程写各自区间互不覆盖
                last_report = 0  # 本分片最近一次已上报的累计字节,用于节流
                for chunk in resp.iter_content(chunk_size=1024 * 64):  # 以64KB为单位迭代网络数据
                    if self._is_cancelled():  # 在每个64KB边界轮询取消信号,实现“尽快退出”
                        raise RuntimeError("下载已被取消")  # 抛异常跳出本分片任务,finally中关闭响应
                    if chunk:  # 保活心跳等场景iter_content可能产出空块,空块不写不计
                        f.write(chunk)  # 在当前seek位置写入64KB,文件指针随之后移
                        c_len = len(chunk)  # 本网络块实际字节数
                        with lock:  # 加锁保护共享计数器,防止并发累加丢失更新
                            shared_counter[0] += c_len  # 全局已下载字节累加
                            curr = shared_counter[0]  # 锁内拷贝最新累计值,保证回调读到的数字自洽
                        # 节流:每累计512KB才触发一次回调,大文件(数GB)避免几万次UI信号风暴
                        if (self.progress_callback and total_size > 0  # 有回调且总大小已知
                                and curr - last_report >= 512 * 1024):  # 累计增量达到512KB阈值
                            self.progress_callback(curr, total_size)  # 在工作线程中触发平滑进度回调
                            last_report = curr  # 更新最近上报点
        finally:
            resp.close()  # 无论成功/取消/异常都显式关闭响应,归还连接到连接池

    def download(
        self,
        download_url: str,
        save_dir: str,
        display_name: str,
        expect_sha256: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        自适应下载主入口;支持HTTP-RANGE断点续传;优先多线程分片,异常时多线程重试(最多3次);仅不支持Range时单线程全量兜底;下载完成执行sha256校验
        :param download_url: 文件完整http下载地址
        :type download_url: str
        :param save_dir: 文件保存目标目录,不存在自动创建
        :type save_dir: str
        :param display_name: 原始展示文件名(UI展示用,内部自动清洗得到磁盘安全名称)
        :type display_name: str
        :param expect_sha256: 预期sha256哈希字符串;传入None跳过哈希校验
        :type expect_sha256: Optional[str]
        :return: 标准化下载结果字典
            success:IO是否写入磁盘成功
            save_path:本地完整路径
            local_filename:磁盘文件名
            total_bytes:服务端文件总字节
            local_exist_bytes:调用前本地已经存在的字节数
            resume_used:bool,True代表本次启用断点续传
            calc_sha256:本地计算sha256
            expect_sha256:预期哈希
            sha256_ok:哈希校验结果
            used_mode:"multithread"/"single_thread"
            error:错误信息
        :rtype: Dict[str, Any]
        """
        safe_name = _safe_local_filename(display_name)  # 清洗展示名中的Windows非法字符,得到磁盘文件名
        save_path = os.path.join(save_dir, safe_name)  # 拼接目标目录与文件名,形成完整本地保存路径(最终成品路径)
        # 下载临时文件路径:下载过程中写入 .part 文件,完成后重命名为正式文件
        # 这样可以区分"正在下载的残文件"(大小<total_size,可续传)和"已完成的文件"(可秒传)
        part_path = save_path + ".part"
        os.makedirs(save_dir, exist_ok=True)  # 确保保存目录存在,已存在时不报错
        local_exist_bytes = 0  # 下载前本地残文件已有字节数,作为断点续传判断依据
        resume_used = False  # 本次是否真正启用断点续传的标志
        total_size = 0  # 服务端文件总字节,HEAD探测成功后填充
        if self.logger:  # 仅在注入日志器时输出开始日志
            self.logger.info(  # 记录下载任务起点与代理/哈希配置
                f"📥 开始下载:{display_name} -> {save_path} "  # 日志正文第一行:展示名与目标路径
                f"(代理={'有' if self.proxies else '无'}"  # 日志第二行:当前是否走代理
                f"{',期望sha256已提供' if expect_sha256 else ',不校验sha256'})"  # 日志第三行:是否提供期望哈希
            )

        # 进入网络请求前先响应一次取消
        if self._is_cancelled():  # 任务尚未发起任何网络请求即被取消,走快速失败路径
            if self.logger:  # 有日志器则记录警告
                self.logger.warning(f"🛑 下载未开始即被取消:{display_name}")  # 输出未开始即取消的警告
            return {  # 返回标准化失败字典(所有路径字段保持一致,方便上层统一读取)
                "success": False,  # IO未成功
                "save_path": save_path,  # 计划保存路径(文件未必生成)
                "local_filename": safe_name,  # 清洗后的磁盘文件名
                "total_bytes": 0,  # 未探测到总大小
                "local_exist_bytes": local_exist_bytes,  # 本地已有字节(此时尚为0)
                "resume_used": resume_used,  # 未进入续传
                "calc_sha256": None,  # 未计算哈希
                "expect_sha256": expect_sha256,  # 原样回传期望哈希
                "sha256_ok": False,  # 失败恒为False
                "used_mode": None,  # 未使用任何下载模式
                "error": "下载已被取消"  # 固定取消错误文案
            }

        # ------------------------------------------------------------------
        # 分支0:正式文件已存在 → 校验哈希(秒传判断)
        # 只有正式文件(save_path)存在才走秒传逻辑;.part 文件走续传逻辑
        # ------------------------------------------------------------------
        if os.path.exists(save_path):  # 正式文件已存在,可能是上次下载完成后未清理,或用户手动放置
            local_exist_bytes = os.path.getsize(save_path)  # 读取正式文件大小
        else:
            local_exist_bytes = 0  # 无正式文件

        # ------------------------------------------------------------------
        # GitHub API 资产下载接口预解析(仅当 URL 为 api.github.com 资产接口时)
        # api.github.com/repos/{owner}/{repo}/releases/assets/{id} 需带
        # Accept: application/octet-stream 才会 302 重定向到 release-assets CDN
        # 签名直链(可达且快,支持 Range 分片,18 分钟有效)。只预解析一次,
        # 后续 HEAD 探测与全部分片共用该直链,避免每个分片各消耗一次 API 配额
        # (GitHub 匿名限流 60 次/小时,分片多时会耗尽)。
        # ------------------------------------------------------------------
        if "/releases/assets/" in download_url:
            try:
                _probe_headers = dict(self.request_headers)  # 复制共用请求头
                _probe_headers.setdefault("Accept", "application/octet-stream")  # 资产接口必须此头才返回302
                _resp = self.session.get(  # 不跟随重定向,手动读取 Location
                    download_url, headers=_probe_headers, proxies=self.proxies,
                    timeout=self.timeout, verify=_effective_verify(self.verify_ssl, self.proxies),
                    stream=True, allow_redirects=False,
                )
                _loc = _resp.headers.get("Location", "")  # 签名直链(带jwt签名与有效期)
                _resp.close()  # 立即释放连接(stream=True 时必须显式关闭)
                if _resp.status_code in (301, 302, 303, 307, 308) and _loc:
                    download_url = _loc  # 换用 CDN 签名直链,后续HEAD/分片全走直链
                    if self.logger:
                        self.logger.info(f"🔗 已解析GitHub资产CDN直链:{display_name}")
                else:  # 未按预期返回重定向:保持原URL,后续流程会给出具体错误
                    if self.logger:
                        self.logger.warning(f"⚠️ GitHub资产接口未返回重定向(status={_resp.status_code}),将直连原URL:{display_name}")
            except Exception as e:  # 解析失败不致命:退回原URL走正常流程
                if self.logger:
                    self.logger.warning(f"⚠️ GitHub资产直链解析失败,将直连原URL:{display_name} | {type(e).__name__}:{str(e)}")

        # HEAD探测服务端信息
        try:  # HEAD请求本身可能因DNS/超时/连接拒绝抛异常,需单独捕获
            head_resp = self.session.head(  # HEAD只取响应头不下载body:查总大小与Range支持情况
                download_url,  # 文件下载地址(HEAD与GET同一URL)
                headers=dict(self.request_headers),  # 携带GitHub Token等共用认证头
                proxies=self.proxies,  # 代理配置
                timeout=self.timeout,  # 探测超时
                verify=_effective_verify(self.verify_ssl, self.proxies),  # 代理联动后的证书校验开关
                allow_redirects=True  # 允许跟随3xx重定向,确保拿到最终资源的响应头
            )
        except Exception as e:  # 捕获网络层异常(连接错误/超时/TLS失败等)
            if self.logger:  # 有日志器记录异常类型与消息
                self.logger.error(f"❌ HEAD探测请求异常:{display_name} | {type(e).__name__}:{str(e)}")  # 输出HEAD异常详情
            return {  # HEAD失败:无法获知资源信息,直接返回标准失败结果
                "success": False,  # IO失败
                "save_path": save_path,  # 计划保存路径
                "local_filename": safe_name,  # 磁盘文件名
                "total_bytes": 0,  # 总大小未知
                "local_exist_bytes": local_exist_bytes,  # 本地已有字节
                "resume_used": resume_used,  # 未续传
                "calc_sha256": None,  # 未算哈希
                "expect_sha256": expect_sha256,  # 期望哈希原样回传
                "sha256_ok": False,  # 失败
                "used_mode": None,  # 未进入下载模式
                "error": f"HEAD探测请求失败:{str(e)}"  # 包装后的错误信息
            }

        if head_resp.status_code >= 400:  # HEAD返回4xx/5xx:资源不存在或无权限等
            if self.logger:  # 记录HTTP错误状态码
                self.logger.error(f"❌ HEAD探测返回HTTP {head_resp.status_code}:{display_name}")  # 输出状态码错误日志
            return {  # 返回与其他失败路径结构一致的字典
                "success": False,  # IO失败
                "save_path": save_path,  # 计划保存路径
                "local_filename": safe_name,  # 磁盘文件名
                "total_bytes": 0,  # 总大小不可用
                "local_exist_bytes": local_exist_bytes,  # 本地已有字节
                "resume_used": resume_used,  # 未续传
                "calc_sha256": None,  # 未算哈希
                "expect_sha256": expect_sha256,  # 期望哈希回传
                "sha256_ok": False,  # 失败
                "used_mode": None,  # 未进入下载模式
                "error": f"HEAD status code {head_resp.status_code}"  # 携带HTTP状态码的错误串
            }

        total_size = int(head_resp.headers.get("content-length", 0))  # 读Content-Length;chunked等场景缺失时按0处理
        accept_ranges = head_resp.headers.get("accept-ranges", "")  # 读Accept-Ranges头;值为"bytes"才支持Range分片

        # ------------------------------------------------------------------
        # 分支1:正式文件大小 >= 服务端总大小 → 校验哈希(秒传/重下判断)
        # 只有正式文件才走秒传逻辑,因为 .part 文件可能因预分配导致大小=total_size但内容不完整
        # ------------------------------------------------------------------
        if total_size > 0 and local_exist_bytes >= total_size and os.path.exists(save_path):
            if self.logger:  # 记录"本地已完整,开始验哈希"
                self.logger.info(f"✅ 本地文件已完整,校验哈希:{safe_name} ({local_exist_bytes}字节)")  # 输出完整性校验日志
            verified = self._verify_complete_file(  # 统一走秒传校验(哈希通过即返回,不符则删文件)
                save_path, total_size, expect_sha256, local_exist_bytes, False, None)
            if verified is not None:  # 校验通过(或未要求校验):直接成功返回,省掉重复下载
                return verified  # 返回秒传结果字典
            local_exist_bytes = 0  # 哈希不符:损坏文件已删,重置本地已有字节数

        # ------------------------------------------------------------------
        # ------------------------------------------------------------------
        # 分支2:分片位图(meta)驱动的断点续传探测
        # .part.meta 记录每个分片是否完成,中断后续传只补未完成分片;
        # 无 meta(旧版/全新)一律全量重建,避免旧 .part 预分配假大小/空洞导致续传损坏
        # ------------------------------------------------------------------
        part_exist_bytes = 0  # .part 残文件已有字节数
        download_ok = False  # 下载阶段成功标志(多线程任一尝试成功即True;单线程仅限不支持Range场景)
        used_mode: Optional[str] = None  # 实际使用的下载模式字符串,成功后填充
        error_msg: Optional[str] = None  # 下载失败时的错误信息
        multithread_attempted = False  # 是否实际进入过多线程分片阶段
        meta_path = part_path + ".meta"  # 分片位图文件路径
        meta_chunks: Optional[List[list]] = None  # meta分片列表[[s,e,ok],...];None=无有效位图
        if total_size > 0 and accept_ranges.lower() == "bytes":  # 支持Range才可能分片续传
            meta = _load_meta(meta_path)  # 读取分片位图(结构非法返回None)
            if meta is not None and meta.get("total") != total_size:  # 远端文件更新导致总大小变化
                meta = None  # 位图作废,走全量重建
            if meta is not None and os.path.exists(part_path):  # 有效位图且残片存在:按位图续传
                meta_chunks = meta["chunks"]  # 恢复分片列表
                assert meta_chunks is not None and len(meta_chunks) > 0  # 位图结构已由 _load_meta 校验,此处仅做类型收窄
                pending_done = sum(1 for _s, _e, ok in meta_chunks if ok)  # 已标记完成的分片数
                pending_total = len(meta_chunks)  # 分片总数
                if 0 < pending_done < pending_total:  # 有已完成也有未完成:进入精确续传
                    resume_used = True  # 标记本次启用续传
                    part_exist_bytes = sum(e - s + 1 for s, e, ok in meta_chunks if ok)  # 已完成字节作为进度起点
                    if self.logger:  # 记录按位图续传
                        self.logger.info(  # 输出续传日志
                            f"♻️ 分片位图续传:{safe_name},已完成{pending_done}/{pending_total}片,"  # 位图续传摘要
                            f"补下{total_size - part_exist_bytes}字节")  # 待补字节数
                elif pending_done == pending_total and os.path.exists(part_path):  # 位图全完成且残片存在:内容已完整
                    # 取消/中断恰发生在"所有分片下载完成、改名尚未执行"的窗口:直接改名并复用秒传校验,
                    # 避免退化成全量重下(此前实测:此场景会白下整个文件)
                    try:
                        if os.path.exists(save_path):  # 正式文件已存在先删除
                            os.unlink(save_path)
                        os.rename(part_path, save_path)  # .part → 正式文件
                        _drop_meta(meta_path)  # 下载完成,删除分片位图
                    except OSError:  # 改名失败(权限/占用):退化为全量重建
                        pass
                    if os.path.exists(save_path):  # 改名成功:走秒传校验
                        verified = self._verify_complete_file(  # 哈希通过即返回,不符则删文件继续重建
                            save_path, total_size, expect_sha256, total_size, False, "multithread")
                        if verified is not None:  # 校验通过:直接返回成功
                            return verified  # 位图全完成秒传返回
                else:  # 全未完成或改名失败:重建全量
                    resume_used = False  # 标记全量
                    part_exist_bytes = 0  # 进度归零
                    meta_chunks = None  # 丢弃无效位图
            else:  # 无有效位图:全量重建,删除旧 .part(防空洞/预分配假大小)
                resume_used = False  # 标记全量
                part_exist_bytes = 0  # 进度归零
                if os.path.exists(part_path):  # 清理旧残片
                    os.unlink(part_path)  # 删除旧 .part
                with open(part_path, "wb") as f:  # 新建空文件占位,供truncate预分配
                    pass
                meta_chunks = _build_chunks(total_size, self.chunk_size)  # 按自适应分片大小构建位图
                _save_meta(meta_path, total_size, meta_chunks)  # 落盘位图,供中断后续传
        else:  # 不支持Range/大小未知:单线程全量,无法续传
            resume_used = False  # 标记全量
            part_exist_bytes = 0  # 进度归零
            if os.path.exists(part_path):  # 清理旧 .part
                os.unlink(part_path)

        # --------多线程分片下载(meta驱动,失败只重试未完成分片,写入 .part 文件)--------
        # 失败策略: 不做单线程降级;任务内只重试失败的分片(最多 MAX_MULTITHREAD_RETRIES 轮),
        # 每个分片完成即标记位图并落盘,进度单调不回退;中断后下次按位图只补缺失分片.
        MAX_MULTITHREAD_RETRIES = 3  # 多线程最大重试轮数(含首轮)
        if total_size > 0 and accept_ranges.lower() == "bytes" and meta_chunks is not None:  # 支持Range且有位图:多线程
            pending = []  # 待下载分片三元组列表(起始,结束,位图下标)
            for idx, (_s, _e, ok) in enumerate(meta_chunks):  # 遍历位图
                if not ok:  # 仅未完成分片进入待下载队列
                    pending.append((_s, _e, idx))
            attempt = 0  # 当前轮次
            shared_counter = [part_exist_bytes]  # 共享计数器,循环外定义,各轮延续累加,进度单调不回退
            lock = threading.Lock()  # 保护shared_counter与位图标记的互斥锁
            # 自适应并发: 初始12条连接测速,加档后总吞吐仍提升>15%则继续加,直到上限self.max_workers
            adaptive_min = min(12, self.max_workers)  # 起始并发(下限)
            current_w = adaptive_min  # 当前并发档位
            v1_ewma: Optional[float] = None  # 单连接速度指数滑动平均(字节/秒),None=尚未测到
            prev_total: Optional[float] = None  # 上一档总吞吐估计,用于判定是否继续加档
            while attempt < MAX_MULTITHREAD_RETRIES and pending and not download_ok:  # 重试循环
                attempt += 1  # 计次
                multithread_attempted = True  # 进入多线程分片阶段,置位标记
                prev_total = None  # 每轮重新测速判定(重试轮分片少,从当前档起步)
                if attempt == 1 and not resume_used:  # 首轮且非续传:预分配文件到总大小
                    with open(part_path, "wb") as f:  # wb打开占位空文件
                        f.truncate(total_size)  # 稀疏预分配,分片可直接seek到任意偏移写入
                queue = list(pending)  # 本轮待下载队列(动态提交),元素(起始,结束,位图下标)
                pending = []  # 本轮失败分片收集(下一轮只重试这些)
                futures: dict = {}  # future -> (起始,结束,位图下标,提交时刻)
                done_count = 0  # 本轮已完成分片数(测速判定用)
                executor = ThreadPoolExecutor(max_workers=self.max_workers)  # 线程池上限=max_workers

                def _submit_more():  # 按当前档位补提交分片(队列非空且活跃future数<current_w)
                    while len(futures) < current_w and queue:
                        _s, _e, _idx = queue.pop(0)  # 队首取未下载分片
                        futures[executor.submit(  # 提交到线程池
                            self._download_chunk,  # 分片工作函数
                            download_url,  # 位置参数1:下载URL
                            _s,  # 位置参数2:本片起始偏移
                            _e,  # 位置参数3:本片结束偏移
                            part_path,  # 位置参数4:共享 .part 文件路径
                            total_size,  # 位置参数5:文件总字节
                            shared_counter,  # 位置参数6:共享计数器
                            lock  # 位置参数7:线程锁
                        )] = (_s, _e, _idx, time.time())  # 记录分片信息与提交时刻(测速用)

                try:
                    _submit_more()  # 首轮按初始档位(12)提交
                    while futures:  # 有在飞分片就持续收尾
                        done, _not = wait(futures, return_when="FIRST_COMPLETED", timeout=2)  # 等任意分片完成(超时防卡死)
                        if not done:  # 超时无完成: 在飞分片仍在运行,检查取消后继续等
                            if self._is_cancelled():  # 超时期间收到取消: 提前退出
                                break
                            continue
                        for fut in done:  # 逐个处理已完成分片
                            _s, _e, _idx, t0 = futures.pop(fut)  # 取分片信息与提交时刻
                            exc = fut.exception()  # 任务内异常;None=成功
                            if exc:  # 分片失败(含取消): 记入重试列表
                                pending.append((_s, _e, _idx))
                                if not error_msg:  # 仅保留第一个错误文本用于返回
                                    error_msg = str(exc)
                            else:  # 分片成功
                                done_count += 1  # 本轮完成数+1
                                with lock:  # 锁内更新位图(与其他分片互斥)
                                    meta_chunks[_idx][2] = True  # 标记完成
                                    _save_meta(meta_path, total_size, meta_chunks)  # 落盘位图
                                elapsed = time.time() - t0  # 本分片实际耗时
                                if elapsed > 0.1:  # 耗时太短测量无意义,跳过
                                    spd = (_e - _s + 1) / elapsed  # 本分片单连接速度(字节/秒)
                                    v1_ewma = spd if v1_ewma is None else 0.7 * v1_ewma + 0.3 * spd  # EWMA平滑
                        # 自适应加档判定: 本轮完成>=2个分片且队列非空且未锁档
                        if (v1_ewma is not None and queue and done_count >= 2
                                and prev_total != float("inf")):
                            cur_total = current_w * v1_ewma  # 当前档总吞吐估计
                            if prev_total is None:  # 首档: 记录基准,不调档
                                prev_total = cur_total
                            elif current_w < self.max_workers and cur_total > prev_total * 1.15:  # 加档后总吞吐仍提升>15%
                                prev_total = cur_total  # 更新基准
                                current_w = min(current_w + 4, self.max_workers)  # 加一档(4条)
                                if self.logger:  # 记录加档
                                    self.logger.debug(  # 输出自适应加档日志
                                        f"🚀 自适应加档:{current_w - 4}→{current_w}连接,单连接~{v1_ewma / 1024 / 1024:.1f}MB/s")
                            else:  # 加档无收益或已达上限: 锁定当前档位
                                prev_total = float("inf")  # 不再尝试加档
                        _submit_more()  # 完成一批后按当前档位补提交
                finally:
                    for fut in futures:  # 取消尚未开始的分片任务
                        fut.cancel()
                    executor.shutdown(wait=True)  # 等待在飞分片结束并回收线程池
                if self._is_cancelled():  # 外部取消:记录并退出重试循环
                    if self.logger:
                        self.logger.info(f"🛑 多线程下载已被外部取消:{safe_name}")
                    download_ok = False
                    break
                if pending:  # 存在失败分片:本轮未完成
                    download_ok = False
                    if attempt < MAX_MULTITHREAD_RETRIES:  # 还有轮次:记录并继续
                        if self.logger:
                            self.logger.warning(
                                f"⚠ 多线程下载异常(第{attempt}轮,{len(pending)}个分片失败),稍后重试:{safe_name} | err={error_msg}")
                    else:  # 重试轮次耗尽:删除 .part 与位图,防止下次续传踩空洞
                        if os.path.exists(part_path):
                            try:
                                os.unlink(part_path)
                            except OSError:
                                pass
                        _drop_meta(meta_path)  # 删除位图,下次全量重建
                        if self.logger:
                            self.logger.error(
                                f"❌ 多线程下载失败(已重试{MAX_MULTITHREAD_RETRIES}轮,已清理残片):{safe_name} | err={error_msg}")
                else:  # 本轮全部分片成功
                    download_ok = True  # 多线程下载成功
                    used_mode = "multithread"  # 记录实际模式为多线程
                    if self.logger:
                        self.logger.debug(f"🧵 多线程分片下载完成 resume={resume_used} url={download_url}")

        # 已取消则直接返回失败(响应/会话由各分片finally释放)
        if self._is_cancelled():  # 取消状态下不再继续
            if self.logger:  # 记录取消
                self.logger.warning(f"🛑 下载已取消:{safe_name}")  # 输出取消日志
            return {  # 取消失败结果
                "success": False,  # IO失败
                "save_path": save_path,  # 正式路径(文件未必存在)
                "local_filename": safe_name,  # 磁盘文件名
                "total_bytes": total_size,  # 已探测到的总大小
                "local_exist_bytes": part_exist_bytes,  # .part 文件已有字节
                "resume_used": resume_used,  # 是否曾尝试续传
                "calc_sha256": None,  # 未完成下载,不算哈希
                "expect_sha256": expect_sha256,  # 期望哈希回传
                "sha256_ok": False,  # 失败
                "used_mode": used_mode,  # 已记录的模式(如multithread)
                "error": "下载已被取消"  # 固定取消文案
            }

        # --------单线程全量下载(仅服务端不支持 Range 或大小未知时兜底,非降级)--------
        if not download_ok and not multithread_attempted:  # 从未进入多线程(不支持Range/大小未知)才走单线程
            used_mode = "single_thread"  # 标记实际模式
            resp = None  # 预声明响应对象,确保finally中可安全判断关闭
            try:
                headers = dict(self.request_headers)  # 复制共用请求头;此场景无法续传,不带Range
                resp = self.session.get(  # 流式GET下载
                    download_url,  # 下载地址
                    headers=headers,  # 请求头
                    proxies=self.proxies,  # 代理配置
                    timeout=self.timeout,  # 请求超时
                    verify=_effective_verify(self.verify_ssl, self.proxies),  # 代理联动后的verify
                    stream=True  # 流式读取,边收边写
                )
                resp.raise_for_status()  # 非2xx抛HTTPError
                downloaded = 0  # 全量下载进度从0起算
                with open(part_path, "wb") as f:  # 覆盖写入 .part 文件
                    for chunk in resp.iter_content(chunk_size=1024*64):  # 每64KB迭代一次网络数据
                        if self._is_cancelled():  # chunk边界响应外部取消
                            raise RuntimeError("下载已被取消")  # 抛异常中断下载
                        if chunk:  # 跳过保活产生的空块
                            f.write(chunk)  # 写入 .part 文件
                            downloaded += len(chunk)  # 累加已下载字节
                            if self.progress_callback and total_size > 0:  # 有回调且总大小已知
                                self.progress_callback(downloaded, total_size)  # 回调单线程下载进度
                download_ok = True  # 流式循环正常结束:下载成功
            except Exception as e:  # 单线程阶段异常(含取消)
                download_ok = False  # 标记下载失败
                error_msg = str(e)  # 记录错误文本
                if self._is_cancelled():  # 由取消引发
                    if self.logger:  # 记录取消信息
                        self.logger.info(f"🛑 单线程下载被取消:{safe_name}")  # 输出单线程取消日志
                elif self.logger:  # 真实下载失败
                    self.logger.error(f"❌ 单线程流式下载失败:{safe_name} | {type(e).__name__}:{error_msg}")  # 输出错误类型与详情
            finally:
                # 流式响应必须显式关闭,避免连接池耗尽(取消/异常路径同样生效)
                if resp is not None:  # 响应对象已创建才需要关闭
                    resp.close()  # 关闭响应,归还连接

        # 下载阶段被取消,不做sha256校验直接返回(.part 残文件保留,供下次续传)
        if self._is_cancelled():  # 单线程阶段被取消
            if self.logger:  # 记录跳过校验
                self.logger.warning(f"🛑 下载阶段取消,跳过sha256校验:{safe_name}")  # 输出取消跳过哈希警告
            return {  # 取消失败结果(.part 残文件保留,可下次续传)
                "success": False,  # IO未成功完成
                "save_path": save_path,  # 正式路径(文件未必存在)
                "local_filename": safe_name,  # 磁盘文件名
                "total_bytes": total_size,  # 服务端总大小
                "local_exist_bytes": part_exist_bytes,  # .part 文件已有字节
                "resume_used": resume_used,  # 是否使用续传
                "calc_sha256": None,  # 未校验
                "expect_sha256": expect_sha256,  # 期望哈希回传
                "sha256_ok": False,  # 失败
                "used_mode": used_mode,  # 实际模式标记
                "error": "下载已被取消"  # 取消错误文案
            }

        if not download_ok:  # 多线程与单线程均失败:最终失败
            if self.logger:  # 记录最终失败
                self.logger.error(f"❌ 文件下载最终失败:{safe_name} | {error_msg}")  # 输出最终失败错误
            return {  # 最终失败字典
                "success": False,  # IO失败
                "save_path": save_path,  # 正式路径(.part 残文件保留,供下次续传)
                "local_filename": safe_name,  # 磁盘文件名
                "total_bytes": total_size,  # 服务端总大小
                "local_exist_bytes": part_exist_bytes,  # .part 文件已有字节
                "resume_used": resume_used,  # 是否续传
                "calc_sha256": None,  # 下载未完成不算哈希
                "expect_sha256": expect_sha256,  # 期望哈希回传
                "sha256_ok": False,  # 失败
                "used_mode": used_mode,  # 实际尝试过的模式
                "error": error_msg  # 底层错误信息
            }

        # ------------------------------------------------------------------
        # 下载 IO 完成:将 .part 文件重命名为正式文件,然后校验 sha256
        # ------------------------------------------------------------------
        # .part → 正式文件:原子重命名(同卷),完成后才走哈希校验
        try:
            if os.path.exists(save_path):  # 正式文件已存在(如上次秒传残留),先删除
                os.unlink(save_path)
            os.rename(part_path, save_path)  # .part → 正式文件
            _drop_meta(meta_path)  # 下载完成,删除分片位图(下次全新下载)
        except OSError as e:
            if self.logger:
                self.logger.error(f"❌ .part 重命名失败:{safe_name} | {e}")
            return {
                "success": False,
                "save_path": save_path,
                "local_filename": safe_name,
                "total_bytes": total_size,
                "local_exist_bytes": part_exist_bytes,
                "resume_used": resume_used,
                "calc_sha256": None,
                "expect_sha256": expect_sha256,
                "sha256_ok": False,
                "used_mode": used_mode,
                "error": f".part 重命名失败:{e}",
            }

        # 下载IO完成,执行sha256校验(校验正式文件)
        calc_sha256: Optional[str] = None  # 本地实算哈希,先置空
        sha256_ok = True  # 校验结论默认通过
        if os.path.exists(save_path):  # 理论上成功必存在,防御性判断
            calc_sha256 = _calc_file_sha256(save_path)  # 对完整下载文件计算sha256
            if expect_sha256 is not None:  # 提供了期望哈希才比对
                sha256_ok = (calc_sha256.lower() == expect_sha256.lower())  # 小写后严格比对
                if self.logger:  # 根据比对结果分级日志
                    if sha256_ok:  # 一致:校验通过
                        self.logger.info(f"🔒 SHA256校验通过:{safe_name}")  # 输出校验通过日志
                    else:  # 不一致:下载内容与预期不符
                        self.logger.error(  # error级别报告哈希不符
                            f"⚠ SHA256校验不通过:{safe_name} expect={expect_sha256}, actual={calc_sha256}"  # 输出期望/实际哈希
                        )
            else:  # 未提供期望哈希:跳过比对
                if self.logger:  # debug级别记录实际哈希
                    self.logger.debug(f"ℹ 未提供期望SHA256,跳过校验:{safe_name} actual={calc_sha256}")  # 输出跳过校验调试日志

        # 哈希校验失败: 删除损坏文件并返回失败,避免调用方把损坏安装包当成功使用
        if expect_sha256 is not None and not sha256_ok:
            try:
                if os.path.exists(save_path):
                    os.unlink(save_path)
            except OSError:
                pass
            if self.logger:  # 记录删除与失败原因
                self.logger.error(
                    f"❌ SHA256校验不通过,已删除损坏文件:{safe_name} "
                    f"expect={expect_sha256}, actual={calc_sha256}")
            return {  # 校验失败结果
                "success": False,  # 明确失败,调用方不得继续安装
                "save_path": save_path,  # 文件已被删除,路径仅供日志
                "local_filename": safe_name,  # 磁盘文件名
                "total_bytes": total_size,  # 服务端总大小
                "local_exist_bytes": part_exist_bytes,  # 续传前已有字节
                "resume_used": resume_used,  # 是否续传
                "calc_sha256": calc_sha256,  # 本地实算哈希
                "expect_sha256": expect_sha256,  # 期望哈希
                "sha256_ok": False,  # 校验不通过
                "used_mode": used_mode,  # 实际下载模式
                "error": f"SHA256校验不通过: expect={expect_sha256}, actual={calc_sha256}",  # 失败原因
            }

        if self.logger:  # 输出下载任务汇总日志
            self.logger.info(  # 包含大小/模式/续传/哈希结论
                f"🎉 文件下载完成:{safe_name} | {total_size}字节 | 模式={used_mode} | "  # 汇总第一行:文件名/大小/模式
                f"断点续传={'是' if resume_used else '否'} | sha256={'通过' if sha256_ok else '不通过'}"  # 汇总第二行:续传与哈希
            )
        return {  # 标准化成功结果
            "success": True,  # IO写入成功
            "save_path": save_path,  # 完整本地路径
            "local_filename": safe_name,  # 磁盘文件名
            "total_bytes": total_size,  # 文件总字节
            "local_exist_bytes": part_exist_bytes,  # 下载前 .part 已有字节
            "resume_used": resume_used,  # 本次是否续传
            "calc_sha256": calc_sha256,  # 本地实算哈希
            "expect_sha256": expect_sha256,  # 期望哈希
            "sha256_ok": sha256_ok,  # 哈希校验结论
            "used_mode": used_mode,  # 实际下载模式multithread/single_thread
            "error": None  # 无错误
        }

    def close(self):
        """关闭内部requests会话,释放连接池资源

        :return: 无返回值
        :rtype: None
        """
        if hasattr(self, "session") and self.session:  # 防御:session属性存在且非空(避免__init__异常场景报错)
            self.session.close()  # 关闭Session,释放底层连接池资源

    def __enter__(self):
        """with上下文管理器入口

        :return: 返回下载器实例自身,供 as 变量绑定
        :rtype: FileDownloader
        """
        return self  # with FileDownloader(...) as dl 中的dl即本实例

    def __exit__(self, exc_type, exc_val, exc_tb):
        """with上下文管理器出口:无论块内是否抛异常都关闭会话

        :param exc_type: with块内异常类型;正常退出为None
        :type exc_type: type | None
        :param exc_val: 异常实例;正常退出为None
        :type exc_val: BaseException | None
        :param exc_tb: 异常追溯对象traceback;正常退出为None
        :type exc_tb: types.TracebackType | None
        :return: 无返回值(不吞异常,异常照常向外传播)
        :rtype: None
        """
        self.close()  # 退出with块时释放Session连接资源


class FileUploader:
    """
    TUS-v1.0 多线程断点分片上传客户端
    ⚠️服务端必须支持TUS v1.0协议,否则无法使用断点分片上传

    工作流程:
        1. POST 创建上传资源端点 → 服务端返回 201 + Location 头(资源URL)
        2. HEAD 查询服务端已接收字节偏移(Upload-Offset) → 实现断点续传
        3. 从断点偏移处开始,边读文件边提交 PATCH 分片任务到线程池
        4. 多线程并发上传各分片,每片完成后累加共享计数器并触发进度回调
        5. 全部分片上传完成后返回成功结果(TUS协议中服务端自动合并,无需额外请求)

    TUS 协议要点:
        - 创建: POST + Tus-Resumable:1.0 + Upload-Length + Upload-Metadata → 201 Location
        - 查询: HEAD + Tus-Resumable:1.0 → Upload-Offset 响应头
        - 上传: PATCH + Tus-Resumable:1.0 + Upload-Offset + Content-Type:application/offset+octet-stream → 204
        - 元数据值必须 base64 编码(UTF-8 字节),格式为 "key base64value,key2 base64value2"

    线程安全说明:
        - progress_callback 在工作线程中触发,操作 UI 需调用方自行保证线程安全
        - 共享进度计数器 list[0] 由 threading.Lock 保护,确保并发累加不丢失
        - 文件读取在主线程串行进行(seek + read),提交任务并发执行
        - 每个 FileUploader 实例持有独立 session,不同实例之间完全独立

    实例属性(全部可在 __init__ 中配置):
        proxies (Dict[str, str]): 代理配置字典,键为协议(http/https),值为代理URL
        timeout (int): 单次 HTTP 请求超时秒数,默认 30 秒(上传建议比下载更大)
        verify_ssl (bool): 是否校验 SSL 证书,默认 False;走代理时被强制关闭
        chunk_size (int): 单个上传分片字节大小,默认 5MB
        max_workers (int): 分片上传并发线程数,默认 3
        logger (logging.Logger | None): 外部注入的日志对象,None 则静默不打日志
        progress_callback (Callable[[int, int], None] | None): 进度回调函数,签名(已上传字节, 总字节)
        session (requests.Session): 组件内部独立 HTTP 会话,复用底层连接池
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
        :param proxies: 代理字典
        :type proxies: Optional[Dict[str, str]]
        :param timeout: http请求超时秒,上传建议调大
        :type timeout: int
        :param verify_ssl: 是否校验ssl证书
        :type verify_ssl: bool
        :param chunk_size: 上传分片大小,默认5MB
        :type chunk_size: int
        :param max_workers: 分片上传最大并发线程
        :type max_workers: int
        :param logger: 外部日志
        :type logger: logging.Logger | None
        :param progress_callback: 上传进度回调(已上传字节,总字节),None不回调
            注意:多线程上传时由工作线程触发,若回调操作UI控件需调用方自行保证线程安全
        :type progress_callback: Optional[Callable[[int, int], None]]
        """
        self.proxies: Dict[str, str] = proxies if proxies is not None else {}  # None归一化为空字典,便于bool判断代理
        self.timeout: int = timeout  # 单次HTTP请求超时秒数(上传大包建议比下载更大)
        self.verify_ssl: bool = verify_ssl  # SSL证书校验开关(走代理时被_effective_verify强制关闭)
        self.chunk_size: int = chunk_size  # 单个上传分片字节大小,默认5MB
        self.max_workers: int = max_workers  # PATCH分片并发线程数
        self.logger = logger  # 外部注入日志器,None静默
        self.progress_callback = progress_callback  # 进度回调(已上传字节, 总字节)
        self.session = requests.Session()  # 组件私有HTTP会话,复用连接池

    def _create_upload_resource(self, create_endpoint: str, file_size: int, filename: str, metadata: Optional[Dict] = None) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        TUS 创建上传资源 (OPTIONS + POST)
        :param create_endpoint: tus创建资源接口地址
        :type create_endpoint: str
        :param file_size: 本地文件总字节
        :type file_size: int
        :param filename: 原始文件名
        :type filename: str
        :param metadata: 额外元数据字典
        :type metadata: Optional[Dict]
        :return: (ok:bool, upload_resource_url:str, error:str|None)
        :rtype: Tuple[bool, Optional[str], Optional[str]]
        """
        headers = {  # TUS创建资源请求头集合
            "Tus-Resumable": "1.0",  # 协议魔数:固定"1.0"声明TUS v1.0,服务端据此识别协议
            "Upload-Length": str(file_size),  # 整个文件实体总字节(十进制字符串),TUS要求创建时声明
            "Upload-Metadata": f"filename {_b64_encode(filename)}"  # 元数据格式"键 空格 base64值",文件名必须base64编码
        }
        if metadata:  # 调用方提供了自定义元数据
            meta_parts = [f"{k} {_b64_encode(str(v))}" for k, v in metadata.items()]  # 每项编码为"key base64value"
            headers["Upload-Metadata"] = headers["Upload-Metadata"] + "," + ",".join(meta_parts)  # 用逗号把自定义项追加到filename之后
        try:
            resp = self.session.post(  # POST创建端点;TUS成功规范返回201 Created
                create_endpoint,  # TUS创建资源URL
                headers=headers,  # TUS协议请求头
                proxies=self.proxies,  # 代理
                timeout=self.timeout,  # 超时
                verify=_effective_verify(self.verify_ssl, self.proxies)  # 代理联动后的证书校验
            )
            resp.raise_for_status()  # 非2xx抛HTTPError
            resource_url = resp.headers.get("Location")  # TUS规定新资源URL放在Location响应头
            if not resource_url:  # 缺少Location则后续HEAD/PATCH无地址可用
                return False, None, "TUS服务端未返回Location上传资源地址"  # 返回协议不合规错误
            return True, resource_url, None  # 创建成功:(True, 资源URL, 无错误)
        except Exception as e:  # 网络错误/HTTP错误码等
            return False, None, str(e)  # 创建失败:(False, 无URL, 错误文本)

    def _get_server_offset(self, upload_resource_url: str) -> Tuple[bool, int, Optional[str]]:
        """
        HEAD查询TUS服务端已经接收的字节偏移,用于断点续传
        :param upload_resource_url: tus上传资源url
        :type upload_resource_url: str
        :return: (ok, offset:int, error)
        :rtype: Tuple[bool, int, Optional[str]]
        """
        headers = {"Tus-Resumable": "1.0"}  # TUS所有请求必须携带协议版本头
        try:
            resp = self.session.head(  # HEAD不发送body,只查询资源当前状态
                upload_resource_url,  # 创建阶段拿到的资源URL
                headers=headers,  # TUS协议头
                proxies=self.proxies,  # 代理
                timeout=self.timeout,  # 超时
                verify=_effective_verify(self.verify_ssl, self.proxies)  # 代理联动后的证书校验
            )
            resp.raise_for_status()  # 非2xx抛异常
            offset_str = resp.headers.get("Upload-Offset", "0")  # TUS断点核心响应头:服务端已连续接收的字节数,缺失按0
            offset = int(offset_str)  # 转为整数偏移
            return True, offset, None  # 查询成功:(True, 已上传偏移, 无错误)
        except Exception as e:  # 网络/HTTP异常
            return False, 0, str(e)  # 查询失败:(False, 偏移0, 错误文本)

    def _upload_single_chunk(self, upload_resource_url: str, chunk_data: bytes,
                              chunk_start_offset: int, total_bytes: int, shared_counter: List[int], lock: threading.Lock) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        单一片块 PATCH上传任务,给线程池调用;上传完成累加字节计数,触发进度回调
        :param upload_resource_url: tus上传资源地址
        :type upload_resource_url: str
        :param chunk_data: 分片二进制字节
        :type chunk_data: bytes
        :param chunk_start_offset: 当前分片起始字节偏移
        :type chunk_start_offset: int
        :param total_bytes: 文件总字节
        :type total_bytes: int
        :param shared_counter: 共享计数器 list[0]
        :type shared_counter: List[int]
        :param lock: 线程锁
        :type lock: threading.Lock
        :return: (ok, new_offset, error)
        :rtype: Tuple[bool, Optional[int], Optional[str]]
        """
        headers = {  # TUS分片PATCH请求头
            "Tus-Resumable": "1.0",  # 协议版本魔数
            "Upload-Offset": str(chunk_start_offset),  # 关键状态机字段:本片首字节在整个文件中的偏移,服务端据此校验乱序/重复
            "Content-Type": "application/offset+octet-stream"  # TUS规定的PATCH内容类型:带偏移的字节流
        }
        try:
            resp = self.session.patch(  # TUS用PATCH(非PUT/POST)向资源追加本片字节
                upload_resource_url,  # 上传资源URL
                data=chunk_data,  # 本片二进制数据作为请求体
                headers=headers,  # 含Upload-Offset的TUS头
                proxies=self.proxies,  # 代理
                timeout=self.timeout,  # 超时
                verify=_effective_verify(self.verify_ssl, self.proxies)  # 代理联动后的证书校验
            )
            resp.raise_for_status()  # 非2xx抛异常;TUS成功通常返回204 No Content
            chunk_len = len(chunk_data)  # 本片字节数
            with lock:  # 加锁更新共享进度
                shared_counter[0] += chunk_len  # 累加已上传字节
                curr_uploaded = shared_counter[0]  # 锁内拷贝累计值供回调使用
            if self.progress_callback and total_bytes > 0:  # 有回调且总字节已知
                self.progress_callback(curr_uploaded, total_bytes)  # 工作线程触发线程安全进度回调

            new_offset = int(resp.headers.get("Upload-Offset", 0))  # 读服务端确认后的新偏移(应=本片起点+本片长度)
            return True, new_offset, None  # 本片成功:(True, 新偏移, 无错误)
        except Exception as e:  # 网络/协议异常
            return False, None, str(e)  # 本片失败:(False, 无新偏移, 错误文本)

    def upload_file(
        self,
        tus_create_endpoint: str,
        local_file_path: str,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        对外主入口:多线程断点分片上传本地文件(TUS-v1.0协议)
        :param tus_create_endpoint: TUS服务创建资源接口url
        :type tus_create_endpoint: str
        :param local_file_path: 需要上传本地文件完整路径
        :type local_file_path: str
        :param metadata: 附加上传元数据字典
        :type metadata: Optional[Dict]
        :return: 上传结果标准化字典
        :rtype: Dict[str, Any]
        """
        if not os.path.exists(local_file_path):  # 前置校验:本地文件必须存在
            if self.logger:  # 记录文件不存在错误
                self.logger.error(f"❌ 上传失败,本地文件不存在:{local_file_path}")  # 输出文件缺失日志
            return {  # 返回失败标准字典
                "success": False,  # 上传失败
                "upload_url": None,  # 尚未创建资源
                "total_bytes": 0,  # 无法获知大小
                "uploaded_bytes": 0,  # 已上传0
                "finished_chunk_count": 0,  # 完成0片
                "total_chunk_count": 0,  # 总片数0
                "error": f"本地文件不存在:{local_file_path}"  # 文件不存在错误
            }
        file_size = os.path.getsize(local_file_path)  # 获取本地文件总字节
        filename = os.path.basename(local_file_path)  # 从完整路径提取纯文件名
        if self.logger:  # 记录上传开始
            self.logger.info(  # 输出文件名/大小/目标端点
                f"📤 开始TUS分片上传:{filename} ({file_size}字节) -> {tus_create_endpoint}"  # 上传开始日志
            )
        # 1. 创建tus上传资源
        create_ok, upload_url, create_err = self._create_upload_resource(tus_create_endpoint, file_size, filename, metadata)  # POST创建并解包三元组
        if not create_ok or upload_url is None:  # 创建失败或服务端未给Location
            if self.logger:  # 记录创建失败
                self.logger.error(f"❌ 创建TUS上传资源失败:{filename} | {create_err}")  # 输出创建失败错误
            return {  # 创建失败结果
                "success": False,  # 失败
                "upload_url": None,  # 无资源URL
                "total_bytes": file_size,  # 本地大小已知,仍回传
                "uploaded_bytes": 0,  # 未上传
                "finished_chunk_count": 0,  # 0片完成
                "total_chunk_count": 0,  # 尚未计算
                "error": f"创建上传资源失败:{create_err}"  # 包装创建错误
            }
        # 2. 获取服务端已上传偏移(断点)
        offset_ok, current_offset, offset_err = self._get_server_offset(upload_url)  # HEAD查询断点并解包
        if not offset_ok:  # 查询断点失败
            if self.logger:  # 记录断点查询失败
                self.logger.error(f"❌ 获取服务端断点偏移失败:{filename} | {offset_err}")  # 输出偏移查询错误
            return {  # 偏移查询失败结果
                "success": False,  # 失败
                "upload_url": upload_url,  # 资源已创建,回传其URL
                "total_bytes": file_size,  # 文件总大小
                "uploaded_bytes": 0,  # 无法确认已传字节,按0
                "finished_chunk_count": 0,  # 0片完成
                "total_chunk_count": 0,  # 未计算
                "error": f"获取断点偏移失败:{offset_err}"  # 包装偏移错误
            }
        total_chunk_count = (file_size + self.chunk_size - 1) // self.chunk_size  # 向上取整计算总分片数(整除技巧避免math.ceil)
        finished_chunk = 0  # 已成功完成的分片计数

        # 断点续传:初始化共享计数器 = 服务端已上传字节
        shared_counter = [current_offset]  # 进度计数器初值为服务端断点偏移,进度回调从断点开始显示
        lock = threading.Lock()  # 保护并发累加的互斥锁

        # 边读边上传:读一个分片提交一个上传任务,不攒全部分片到内存
        try:
            with open(local_file_path, "rb") as f:  # 二进制只读打开待上传文件
                f.seek(current_offset)  # 关键断点动作:文件读指针直接跳到服务端已收偏移,跳过已上传部分
                with ThreadPoolExecutor(max_workers=self.max_workers) as pool:  # with管理线程池,退出时自动shutdown
                    pending: List = []  # 保存全部future,读完后统一收尾取结果
                    while True:  # 持续读分片直到EOF
                        chunk_bytes = f.read(self.chunk_size)  # 读取一片(默认5MB)字节
                        if not chunk_bytes:  # 读到b''表示文件结束
                            break  # 退出读取循环
                        start_off = current_offset  # 快照本片起始偏移(提交任务前记录)
                        current_offset += len(chunk_bytes)  # 主线程串行推进全局偏移(单线程读取,无竞争)
                        fut = pool.submit(  # 向线程池提交本片PATCH任务
                            self._upload_single_chunk,  # 分片上传工作函数
                            upload_url,  # 资源URL
                            chunk_bytes,  # 本片数据
                            start_off,  # 本片起始偏移
                            file_size,  # 文件总大小(进度回调用)
                            shared_counter,  # 共享计数器
                            lock  # 进度锁
                        )
                        pending.append(fut)  # 登记future待稍后收结果
                    for fut in pending:  # 全部分片已提交,按提交顺序逐个等待结果
                        ok, new_off, err = fut.result()  # result()阻塞获取;工作线程异常会在此重新抛出
                        if not ok:  # 某分片返回失败
                            raise RuntimeError(f"分片上传失败:{err}")  # 抛异常中断整个上传,跳到外层except
                        finished_chunk += 1  # 本片成功,完成计数+1
            if self.logger:  # 记录全部分片完成
                self.logger.info(  # 输出分片完成统计
                    f"🎉 分片上传完成:{filename} | {finished_chunk}/{total_chunk_count}分片,"  # 日志第一行:完成片数/总片数
                    f"断点起始偏移={current_offset}"  # 日志第二行:结束时偏移(等于文件大小)
                )
            return {  # 上传成功标准字典
                "success": True,  # 全部成功
                "upload_url": upload_url,  # TUS资源URL
                "total_bytes": file_size,  # 文件总字节
                "uploaded_bytes": file_size,  # 全部字节已上传
                "finished_chunk_count": finished_chunk,  # 完成分片数
                "total_chunk_count": total_chunk_count,  # 总分片数
                "error": None  # 无错误
            }
        except Exception as e:  # 读取/提交/任一分片失败
            if self.logger:  # 记录上传异常与进度
                self.logger.error(f"❌ 分片上传异常:{filename} | 已完成{finished_chunk}/{total_chunk_count} | {e}")  # 输出异常与已完成片数
            return {  # 失败结果(服务端保留断点,下次可续传)
                "success": False,  # 失败
                "upload_url": upload_url,  # 资源URL仍有效,可用于续传
                "total_bytes": file_size,  # 文件总大小
                "uploaded_bytes": shared_counter[0],  # 取共享计数器当前值作为已上传字节(断点进度)
                "finished_chunk_count": finished_chunk,  # 已完成片数
                "total_chunk_count": total_chunk_count,  # 总片数
                "error": str(e)  # 错误文本
            }

    def close(self):
        """关闭http会话释放资源

        :return: 无返回值
        :rtype: None
        """
        if hasattr(self, "session") and self.session:  # 防御:确认session属性存在且非空
            self.session.close()  # 关闭Session,归还连接池资源

    def __enter__(self):
        """with上下文管理器入口

        :return: 返回上传器实例自身,供 as 变量绑定
        :rtype: FileUploader
        """
        return self  # with FileUploader(...) as ul 中的ul即本实例

    def __exit__(self, exc_type, exc_val, exc_tb):
        """with上下文管理器出口:无论块内是否异常都关闭会话

        :param exc_type: with块内异常类型;正常退出为None
        :type exc_type: type | None
        :param exc_val: 异常实例;正常退出为None
        :type exc_val: BaseException | None
        :param exc_tb: 异常追溯对象traceback;正常退出为None
        :type exc_tb: types.TracebackType | None
        :return: 无返回值(不吞异常,异常照常向外传播)
        :rtype: None
        """
        self.close()  # 退出with块时释放Session连接资源
