# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: file_transfer_worker.py
# 归属: runtime/workers 后台任务线程层 —— 文件传输 Worker(下载/上传)
# ------------------------------------------------------------------------------
# 文件用途:
#   文件传输后台线程实现模块.调用 Service 层 FileDownloader/FileUploader,
#   在子线程执行通用 HTTP 多线程下载(DownloadWorker)与 TUS-v1.0 多线程分片
#   上传(UploadWorker),把底层字节进度换算为百分比、把最终结果以 Qt 信号
#   回传主线程.
# ------------------------------------------------------------------------------
# 架构定位:
#   本模块是 QThread 工作线程,通过 Signal 向 controller/view 发送事件,
#   不直接操作 UI 控件.只调用 service.file_downloade 业务封装与 app_config
#   代理解析,不写 UI、不直接改配置;与"GitHub Release 资产下载/自动更新"
#   流程完全解耦,作为通用文件传输能力存在.
#
#   关联组件:
#     - 上游: Controller(文件传输控制)
#     - 下游: runtime.model.app_config.resolve_effective_proxy、
#             runtime.workers.base_worker.BaseWorker、
#             service.file_downloade.ServiceFileDownloader/ServiceFileUploader
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 读取 file_io/github 配置(分片大小、并发数、超时、代理)
#   2. 以进度回调闭包做百分比去抖并发射 signal_progress
#   3. 下载/上传结果(分片计数、字节数、sha256 状态)经 signal_result 回传
#   4. 下载支持协作式取消(threading.Event);异常统一兜底为失败结果 dict
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只调用 service 业务层 + app_config 的代理解析,不写 UI,不直接改配置
#   - DownloadWorker 支持 request_cancel() 协作式取消
#   - UploadWorker 暂未实现取消机制(TUS 协议支持断点续传,取消逻辑较复杂)
# ------------------------------------------------------------------------------
# 线程模型:
#   - signal_log(str): 继承自 BaseWorker 的日志双写信号
#   - signal_progress(int): 进度百分比 0~100,在工作线程 emit
#   - signal_result(dict): 成败详情,在工作线程 emit
#   底层回调可能位于下载器/上传器内部工作线程,Worker 统一转为 Qt 信号,
#   由 Qt 自动以队列连接(QueuedConnection)跨线程投递给 UI 线程.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: threading、urllib3(关闭 InsecureRequestWarning 告警)、
#             typing、os(run 内延迟导入)
#   - 第三方: PySide6.QtCore.Signal
#   - 项目内:
#       runtime.model.app_config.resolve_effective_proxy
#       runtime.workers.base_worker.BaseWorker
#       run() 内延迟导入:
#           service.file_downloade.ServiceFileDownloader
#           service.file_downloade.ServiceFileUploader
# ==============================================================================

# ==============================================================================
# [文件传输 Worker - QThread 胶水层]
# 职责: 后台线程调用 Service 层 FileDownloader/FileUploader,把进度/结果以 Qt 信号回主线程
# 边界: 只调用 service 业务层 + comm_tools 的代理解析,不写 UI,不直接改配置
# 信号:
#   signal_log(str)       - 继承自 BaseWorker,日志双写(落盘+UI)
#   signal_progress(int)  - 进度百分比 0~100
#   signal_result(dict)   - 最终结果(成功/失败+详情)
# ==============================================================================
import sys  # 标准库:控制台单行实时刷新进度(\r 回车覆盖)
import threading  # 导入线程同步原语模块:用 Event 实现下载协作式取消标志
from typing import Any, Dict, Optional  # 从 typing 导入类型标注工具:用于任意值/字典/可选值的类型注解

import urllib3  # 导入 urllib3 HTTP 底层库:用于关闭 SSL 校验告警
from PySide6.QtCore import Signal  # 从 PySide6.QtCore 导入 Signal:Qt 信号定义工具,用于向 UI 线程回传事件

from runtime.model.app_config import resolve_effective_proxy  # 导入实际代理解析函数
from runtime.workers.base_worker import BaseWorker  # 导入 Worker 基类:提供 QThread 基础能力与日志双写功能

# 屏蔽关闭 SSL 警告(走代理时关闭 SSL 校验会产生告警)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 调用 urllib3 的禁用警告函数,关闭 verify=False 时的 InsecureRequestWarning 告警


# ==============================================================================
# [辅助函数]代理配置解析封装
# ==============================================================================
def _console_write(text: str) -> None:
    """安全控制台写入:windowed 打包(无控制台)时 sys.stdout 为 None,静默跳过不崩溃."""
    out = sys.stdout  # 当前标准输出;打包无控制台时为 None
    if out is None:  # 无控制台环境(打包 windowed)
        return  # 静默跳过,进度改由界面日志区显示
    try:  # 兜底:控制台被重定向/关闭等异常场景
        out.write(text)  # 写入文本(\r 回车实现单行刷新)
        out.flush()  # 立即刷新,保证实时可见
    except Exception:  # 任何写入异常
        pass  # 静默忽略,不影响下载主流程


def _build_proxies(proxy_cfg: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """统一解析实际代理:自动跟随系统代理 > 手动开关;无代理返回 None.

    对 resolve_effective_proxy 的薄封装,只返回代理映射而丢弃来源说明,
    供不需要展示代理来源的内部调用使用.

    :param proxy_cfg: 代理配置节字典,包含代理开关、地址、端口等信息
    :type proxy_cfg: Dict[str, Any]
    :return: requests 代理映射字典(键为协议、值为代理 URL 字符串);
             无代理时返回 None
    :rtype: Optional[Dict[str, str]]
    """
    proxies, _source = resolve_effective_proxy(proxy_cfg)  # 调用解析函数,来源说明在此处不需要,用下划线丢弃
    return proxies  # 仅返回代理映射字典供调用方使用


# ==============================================================================
# [辅助函数]文件传输配置安全读取
# ==============================================================================
def _fileio_cfg(config: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """从全局配置中安全取出 file_io(文件传输)配置节.

    对配置缺失做安全兜底,避免 KeyError,返回的字典可能为空,
    由调用方用默认值进一步兜底.

    :param config: 全局配置字典,可能为 None 或不含 file_io 节
    :type config: Dict[str, Dict[str, Any]]
    :return: file_io 配置字典;配置缺失时返回空字典(由调用方用默认值兜底)
    :rtype: Dict[str, Any]
    """
    return config.get("file_io", {}) if config else {}  # config 为空时返回空字典,否则取 file_io 节(缺省也为空字典)


# =========** [Download]Worker - 通用多线程文件下载 **=========
class DownloadWorker(BaseWorker):
    """通用多线程下载 Worker.

    与"GitHub Release 资产下载/自动更新"流程完全解耦,仅做通用 HTTP 文件下载.
    进度回调由底层 FileDownloader 在多线程工作线程内触发,Worker 把字节进度
    换算为百分比后通过 Qt 信号发往主线程,避免 UI 控件跨线程操作.
    支持协作式取消(通过 request_cancel 设置 threading.Event).
    """

    # ------------------------------------------------------------------
    # 类属性(Qt 信号定义)
    # ------------------------------------------------------------------
    # 进度信号:载荷为 0~100 的去抖后进度百分比整数(int),UI 线程据此刷新进度条
    signal_progress = Signal(int)  # 定义进度信号,参数类型为整数,表示当前完成百分比
    # 结果信号:载荷为下载结果 dict,含 success/save_path/local_filename/
    # total_bytes/used_mode/sha256_ok/error 等字段
    signal_result = Signal(dict)  # 定义结果信号,参数类型为字典,携带下载任务的最终结果

    # ------------------------------------------------------------------
    # 实例属性(在 __init__ 中初始化)
    # ------------------------------------------------------------------
    # self.config: dict 全局配置字典,含 file_io、proxy、github 等配置节
    # self.download_url: str 待下载文件的 URL 地址
    # self.save_dir: str 文件保存目录路径
    # self.display_name: str 界面展示用文件名(也作为缺省落盘文件名)
    # self.expect_sha256: Optional[str] 期望的 SHA256 校验值,None 表示不校验
    # self._cancel_event: threading.Event 协作式取消事件标志

    def __init__(
        self,
        config: Dict[str, Dict[str, Any]],
        download_url: str,
        save_dir: str,
        display_name: str,
        expect_sha256: Optional[str] = None,
        parent=None,
    ):
        """初始化通用下载 Worker.

        :param config: 全局配置字典(含 file_io、proxy、github 等配置节)
        :type config: Dict[str, Dict[str, Any]]
        :param download_url: 待下载文件的完整 URL 地址
        :type download_url: str
        :param save_dir: 文件保存的目录路径
        :type save_dir: str
        :param display_name: 界面展示用文件名(也作为缺省落盘文件名)
        :type display_name: str
        :param expect_sha256: 期望的 SHA256 校验值,None 表示不进行校验
        :type expect_sha256: Optional[str]
        :param parent: Qt 父对象,用于线程生命周期管理,
                       通常为控制器或窗口对象
        :type parent: QObject | None
        :return: 无返回值
        :rtype: None
        """
        super().__init__(parent)  # 调用 BaseWorker 父类构造函数,初始化 QThread 并创建按类名命名的 logger
        self.config = config  # 持有全局配置字典,供 run() 方法读取使用
        self.download_url = download_url  # 保存待下载文件的 URL 地址
        self.save_dir = save_dir  # 保存文件的保存目录路径
        self.display_name = display_name  # 保存界面展示用文件名
        self.expect_sha256 = expect_sha256  # 保存期望的 SHA256 校验值(可选)
        self._cancel_event = threading.Event()  # 创建协作式取消事件对象(初始未置位)

    def request_cancel(self):
        """主线程调用:设置取消事件,底层在当前 chunk 边界尽快退出.

        通过 threading.Event 实现协作式取消,不会强制终止线程,
        而是让底层下载器在下一个分片边界检测到取消标志后优雅退出.

        :return: 无返回值
        :rtype: None
        """
        self._cancel_event.set()  # 置位取消标志,底层下载器轮询后在分片边界检测到并退出

    def run(self):
        """QThread 线程入口:执行通用多线程文件下载.

        构造 ServiceFileDownloader 并调用其 download() 方法,通过进度回调
        实时上报下载进度,完成或失败后通过 signal_result 回传结果.
        所有异常在内部捕获并转为失败结果信号.

        :return: 无返回值;下载结果(成功或失败)均通过 signal_result(dict) 异步回传,
                 过程进度通过 signal_progress(int) 回传
        :rtype: None
        :raises Exception: 下载器构造/下载阶段异常在方法内部捕获并转成失败结果信号
        """
        try:  # 包裹整个下载流程,任何异常统一发射失败结果
            from service.file_downloade import ServiceFileDownloader  # 延迟导入多线程下载业务封装类

            fileio = _fileio_cfg(self.config)  # 调用辅助函数,取文件传输配置节(缺失时为空字典)
            chunk_size = int(fileio.get("DOWNLOAD_CHUNK_SIZE", 2 * 1024 * 1024))  # 取下载分片大小,缺省 2MB(调大减少慢启动次数)
            max_workers = int(fileio.get("DOWNLOAD_MAX_WORKERS", 16))  # 取下载并发线程数,缺省 16(多连接吃满带宽)
            proxy_cfg = self.config.get("proxy", {})  # 从全局配置中取代理配置节,缺省为空字典
            proxies, proxy_source = resolve_effective_proxy(proxy_cfg)  # 调用解析函数,得到实际代理与来源说明
            github_cfg = self.config.get("github", {})  # 从全局配置中取 github 配置节,复用超时和Token
            # 大文件下载需要更长的读取窗口: 基础超时 x4(与更新助手口径一致),
            # 避免慢速网络/代理下 15 秒无新数据就 Read timed out 频繁中断
            timeout = int(github_cfg.get("GITHUB_TIMEOUT", 15)) * 4  # 取请求超时秒数,缺省 60 秒
            request_headers: Dict[str, str] = {}  # 公开资产下载无需鉴权:携带失效Token反而触发401

            self._log(  # 双写日志提示下载开始信息
                f"📥 开始通用下载:{self.display_name} -> {self.save_dir} "  # 展示名与保存目录
                f"| 代理={proxy_source if proxies else '无'}"  # 实际代理来源或直连
            )

            last_pct = [-1]  # 闭包变量容器,用列表可变对象实现百分比去抖动

            def _on_progress(current_bytes: int, total_bytes: int):
                """底层下载器字节进度回调:换算百分比并去抖后发射进度信号.

                将已下载字节数换算为百分比,只有百分比发生变化时才发射信号,
                避免频繁发射导致界面卡顿.

                :param current_bytes: 已下载的字节数
                :type current_bytes: int
                :param total_bytes: 文件总字节数
                :type total_bytes: int
                :return: 无返回值
                :rtype: None
                """
                if total_bytes <= 0:  # 检查总大小是否未知(服务器未返回 Content-Length),未知时无法计算百分比
                    return  # 直接忽略本次回调,不发射进度信号
                pct = max(0, min(100, int(current_bytes * 100 / total_bytes)))  # 换算百分比并夹取到 0~100 范围内
                if pct != last_pct[0]:  # 判断百分比是否发生变化,变化了才发射信号(去抖,避免刷屏)
                    last_pct[0] = pct  # 更新最近已发射的百分比记录
                    self.signal_progress.emit(pct)  # 发射进度百分比信号
                    if pct > 0:  # 进度单行刷新:界面日志区 + 控制台(打包版无控制台时界面仍可见)
                        _prog_text = f"⬇️ 下载进度:{current_bytes / 1048576:.1f}M/{total_bytes / 1048576:.1f}M"  # 字节换算为M显示  # 进度文本:已下载/总共
                        self.signal_log.emit(_prog_text)  # 发界面日志信号:日志区原地替换最后一行,单行刷新不刷屏
                        _console_write(f"\r{_prog_text}")  # 控制台同一行实时刷新(开发模式可见)

            with ServiceFileDownloader(  # 构造下载器并使用 with 语句,保证退出时自动释放资源
                proxies=proxies,  # 传入代理映射(None 表示直连)
                timeout=timeout,  # 传入请求超时秒数
                verify_ssl=False,  # 关闭 SSL 校验(兼容代理环境)
                chunk_size=chunk_size,  # 传入分片大小
                max_workers=max_workers,  # 传入并发线程数
                logger=self._logger,  # 注入 Worker 的命名 logger,复用落盘通道
                progress_callback=_on_progress,  # 传入字节进度回调函数
                cancel_event=self._cancel_event,  # 传入协作式取消事件
                request_headers=request_headers,  # 下载请求头(空:公开资产无需鉴权)
            ) as dl:  # 下载器上下文实例
                result = dl.download(  # 调用下载方法,执行下载并返回结果字典
                    download_url=self.download_url,  # 传入下载地址
                    save_dir=self.save_dir,  # 传入保存目录
                    display_name=self.display_name,  # 传入展示/落盘文件名
                    expect_sha256=self.expect_sha256,  # 传入可选的 SHA256 期望值
                )

            if result.get("success"):  # 判断下载是否成功
                _console_write("\n")  # 进度行结束,换行再打完成日志
                self._log(  # 双写日志提示成功详情
                    f"✅ 下载完成:{result.get('local_filename')} "  # 本地文件名
                    f"| {result.get('total_bytes', 0)}字节 "  # 文件总字节数
                    f"| 模式={result.get('used_mode')} "  # 实际使用的下载模式
                    f"| sha256={'通过' if result.get('sha256_ok') else '不通过'}"  # 校验是否通过
                )
                self.signal_progress.emit(100)  # 成功时进度强制置满,确保进度条到达终点
            else:  # 下载器返回失败结果
                _console_write("\n")  # 进度行结束,换行再打失败日志
                self._log(f"❌ 下载失败:{result.get('error', '未知错误')}")  # 双写日志提示失败原因
            self.signal_result.emit(result)  # 无论成败都把结果字典回传给控制器

        except Exception as e:  # 捕获构造/下载阶段抛出的任何异常
            self._logger.error(f"❌ 下载Worker异常:{e}", exc_info=True)  # 落盘完整错误堆栈
            self._log(f"❌ 下载Worker异常:{type(e).__name__}:{e}")  # 双写日志提示异常类型与消息
            self.signal_result.emit({  # 发射兜底失败结果字典
                "success": False,  # 标记失败
                "save_path": None,  # 无保存路径
                "local_filename": self.display_name,  # 仍带展示名便于界面定位条目
                "total_bytes": 0,  # 字节数归零
                "error": str(e),  # 异常消息字符串
            })


# =========** [Upload]Worker - TUS 分片文件上传 **=========
class UploadWorker(BaseWorker):
    """通用 TUS-v1.0 多线程分片上传 Worker.

    服务端必须实现 TUS v1.0 协议,否则无法断点分片上传.
    进度回调由底层 FileUploader 在多线程工作线程内触发,Worker 把字节进度
    换算为百分比后通过 Qt 信号发往主线程.

    注意:TUS 协议天然支持断点续传,当前版本暂未实现协作式取消接口,
    如需取消可在上传完成前关闭窗口终止线程.
    """

    # ------------------------------------------------------------------
    # 类属性(Qt 信号定义)
    # ------------------------------------------------------------------
    # 进度信号:载荷为 0~100 的去抖后进度百分比整数(int),UI 线程据此刷新进度条
    signal_progress = Signal(int)  # 定义进度信号,参数类型为整数,表示当前完成百分比
    # 结果信号:载荷为上传结果 dict,含 success/upload_url/total_bytes/
    # uploaded_bytes/finished_chunk_count/total_chunk_count/error 等字段
    signal_result = Signal(dict)  # 定义结果信号,参数类型为字典,携带上传任务的最终结果

    # ------------------------------------------------------------------
    # 实例属性(在 __init__ 中初始化)
    # ------------------------------------------------------------------
    # self.config: dict 全局配置字典,含 file_io、proxy 等配置节
    # self.tus_create_endpoint: str TUS 协议创建上传会话的服务端 URL
    # self.local_file_path: str 待上传本地文件的完整路径
    # self.metadata: Optional[Dict[str, Any]] 随创建请求一并提交的 TUS 元数据

    def __init__(
        self,
        config: Dict[str, Dict[str, Any]],
        tus_create_endpoint: str,
        local_file_path: str,
        metadata: Optional[Dict[str, Any]] = None,
        parent=None,
    ):
        """初始化 TUS 分片上传 Worker.

        :param config: 全局配置字典(含 file_io、proxy 等配置节)
        :type config: Dict[str, Dict[str, Any]]
        :param tus_create_endpoint: TUS 协议创建上传会话的服务端 URL 端点
        :type tus_create_endpoint: str
        :param local_file_path: 待上传本地文件的完整路径
        :type local_file_path: str
        :param metadata: 随创建请求一并提交的 TUS 元数据字典,
                         None 表示无附加元数据
        :type metadata: Optional[Dict[str, Any]]
        :param parent: Qt 父对象,用于线程生命周期管理,
                       通常为控制器或窗口对象
        :type parent: QObject | None
        :return: 无返回值
        :rtype: None
        """
        super().__init__(parent)  # 调用 BaseWorker 父类构造函数,初始化 QThread 并创建按类名命名的 logger
        self.config = config  # 持有全局配置字典,供 run() 方法读取使用
        self.tus_create_endpoint = tus_create_endpoint  # 保存 TUS 创建会话端点 URL
        self.local_file_path = local_file_path  # 保存本地文件完整路径
        self.metadata = metadata  # 保存 TUS 元数据(可选)

    def run(self):
        """QThread 线程入口:执行 TUS-v1.0 多线程分片上传.

        构造 ServiceFileUploader 并调用其 upload_file() 方法,通过进度回调
        实时上报上传进度,完成或失败后通过 signal_result 回传结果.
        所有异常在内部捕获并转为失败结果信号.

        :return: 无返回值;上传结果(成功或失败)均通过 signal_result(dict) 异步回传,
                 过程进度通过 signal_progress(int) 回传
        :rtype: None
        :raises Exception: 上传器构造/上传阶段异常在方法内部捕获并转成失败结果信号
        """
        try:  # 包裹整个上传流程,任何异常统一发射失败结果
            from service.file_downloade import ServiceFileUploader  # 延迟导入 TUS 分片上传业务封装类

            fileio = _fileio_cfg(self.config)  # 调用辅助函数,取文件传输配置节(缺失时为空字典)
            chunk_size = int(fileio.get("UPLOAD_CHUNK_SIZE", 5 * 1024 * 1024))  # 取上传分片大小,缺省为 5MB
            max_workers = int(fileio.get("UPLOAD_MAX_WORKERS", 3))  # 取上传并发线程数,缺省为 3
            proxy_cfg = self.config.get("proxy", {})  # 从全局配置中取代理配置节,缺省为空字典
            proxies, proxy_source = resolve_effective_proxy(proxy_cfg)  # 调用解析函数,得到实际代理与来源说明

            import os as _os  # 函数内延迟导入 os 模块,仅用于截取文件名
            filename = _os.path.basename(self.local_file_path)  # 从完整路径中截取文件名,用于日志展示
            self._log(  # 双写日志提示上传开始信息
                f"📤 开始 TUS 分片上传:{filename} -> {self.tus_create_endpoint} "  # 文件名与目标端点
                f"| 代理={proxy_source if proxies else '无'}"  # 实际代理来源或直连
            )

            last_pct = [-1]  # 闭包变量容器,用列表可变对象实现百分比去抖动

            def _on_progress(uploaded_bytes: int, total_bytes: int):
                """底层上传器字节进度回调:换算百分比并去抖后发射进度信号.

                将已上传字节数换算为百分比,只有百分比发生变化时才发射信号,
                避免频繁发射导致界面卡顿.

                :param uploaded_bytes: 已上传的字节数
                :type uploaded_bytes: int
                :param total_bytes: 文件总字节数
                :type total_bytes: int
                :return: 无返回值
                :rtype: None
                """
                if total_bytes <= 0:  # 检查总大小是否未知,未知时无法计算百分比
                    return  # 直接忽略本次回调,不发射进度信号
                pct = max(0, min(100, int(uploaded_bytes * 100 / total_bytes)))  # 换算百分比并夹取到 0~100 范围内
                if pct != last_pct[0]:  # 判断百分比是否发生变化,变化了才发射信号(去抖)
                    last_pct[0] = pct  # 更新最近已发射的百分比记录
                    self.signal_progress.emit(pct)  # 发射进度百分比信号

            with ServiceFileUploader(  # 构造上传器并使用 with 语句,保证退出时自动释放资源
                proxies=proxies,  # 传入代理映射(None 表示直连)
                timeout=30,  # 上传请求固定 30 秒超时
                verify_ssl=False,  # 关闭 SSL 校验(兼容代理环境)
                chunk_size=chunk_size,  # 传入分片大小
                max_workers=max_workers,  # 传入并发线程数
                logger=self._logger,  # 注入 Worker 的命名 logger,复用落盘通道
                progress_callback=_on_progress,  # 传入字节进度回调函数
            ) as ul:  # 上传器上下文实例
                result = ul.upload_file(  # 调用上传方法,执行分片上传并返回结果字典
                    tus_create_endpoint=self.tus_create_endpoint,  # 传入 TUS 创建会话端点
                    local_file_path=self.local_file_path,  # 传入本地文件路径
                    metadata=self.metadata,  # 传入附加 TUS 元数据
                )

            if result.get("success"):  # 判断上传是否成功
                self._log(  # 双写日志提示成功详情
                    f"✅ 上传完成:{filename} "  # 文件名
                    f"| {result.get('finished_chunk_count')}/{result.get('total_chunk_count')}分片 "  # 完成分片数/总片数
                    f"| {result.get('uploaded_bytes', 0)}/{result.get('total_bytes', 0)}字节"  # 已传字节/总字节
                )
                self.signal_progress.emit(100)  # 成功时进度强制置满,确保进度条到达终点
            else:  # 上传器返回失败结果
                self._log(f"❌ 上传失败:{result.get('error', '未知错误')}")  # 双写日志提示失败原因
            self.signal_result.emit(result)  # 无论成败都把结果字典回传给控制器

        except Exception as e:  # 捕获构造/上传阶段抛出的任何异常
            self._logger.error(f"❌ 上传Worker异常:{e}", exc_info=True)  # 落盘完整错误堆栈
            self._log(f"❌ 上传Worker异常:{type(e).__name__}:{e}")  # 双写日志提示异常类型与消息
            self.signal_result.emit({  # 发射兜底失败结果字典
                "success": False,  # 标记失败
                "upload_url": None,  # 无上传地址
                "total_bytes": 0,  # 总字节数归零
                "uploaded_bytes": 0,  # 已传字节归零
                "finished_chunk_count": 0,  # 完成片数归零
                "total_chunk_count": 0,  # 总片数归零
                "error": str(e),  # 异常消息字符串
            })
