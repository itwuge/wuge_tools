# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: update_worker.py
# 归属: updater_app/workers —— 检测与下载工作线程（Update Worker）
# ------------------------------------------------------------------------------
# 文件用途:
#   更新助手 Worker 层——Release 检测/更新包下载（QThread）。
#   在子线程中执行 GitHub Release 检测和更新包下载等耗时网络操作，
#   通过信号把进度、日志、结果抛回 Controller 更新 UI。
#   支持协作式取消（cooperative cancellation）。
# ------------------------------------------------------------------------------
# 架构定位:
#   Worker 层的检测与下载工作线程，在 QThread 子线程中执行网络 IO。
#   支持两种模式（mode 参数区分）:
#     - mode="check":    拉取最新 Release，发射 release 元数据 + 资产列表
#     - mode="download": 按资产名下载更新包到保存目录，支持协作式取消
#   同一 QThread 实例按顺序新建，避免重复 start 的问题。
#
#   关联组件:
#     - 上游: UpdateAssistantController（Controller，创建并管理本线程实例）
#     - 下游:
#       - service.github_update.ServiceGithubRelease（GitHub Release 服务）
#       - service.file_downloade.ServiceFileDownloader（文件下载服务）
#       - config_manager.resolve_effective_proxy（代理解析工具）
# ------------------------------------------------------------------------------
# 核心功能:
#   1. check 模式: 检测 GitHub 最新 Release，获取版本号、更新说明、资产列表
#   2. download 模式: 按资产名下载更新包，支持分片下载、sha256 校验、进度回调
#   3. 协作式取消: 通过 _cancel_event 事件通知线程退出
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只调用 service 业务层，不写 UI、不读写 json、不 import View
#   - 不实现网络请求算法，交给 service 层
#   - 不直接操作 UI 控件，通过信号回主线程
# ------------------------------------------------------------------------------
# 线程模型:
#   本类继承自 QThread，run() 方法在子线程中执行。
#   通过信号（signal_progress、signal_log、signal_release_meta、
#   signal_asset_list、signal_finish）与主线程通信，
#   Qt 会自动将信号排队到接收者所在线程的事件队列。
#   支持协作式取消: Controller 调用 request_cancel() 设置 _cancel_event，
#   网络请求/分片循环在边界检查事件并尽快退出。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging, os, threading
#   - 第三方: PySide6.QtCore
#   - 项目内:
#       updater_app.model.config_manager.resolve_effective_proxy
#       service.github_update.ServiceGithubRelease（延迟导入）
#       service.file_downloade.ServiceFileDownloader（延迟导入）
# ==============================================================================

# 导入日志记录库，用于记录工作线程的日志
import logging
# 导入操作系统接口库，用于路径拼接
import os
# 导入线程库，用于协作式取消的事件对象
import threading

# 导入 Qt 核心模块的 QThread（线程基类）和 Signal（信号类）
from PySide6.QtCore import QThread, Signal

# 从配置管理模块导入代理解析函数
from updater_app.model.config_manager import resolve_effective_proxy


# ==============================================================================
# 类: AssistantUpdateWorker
# ==============================================================================
# =========** [AssistantUpdateWorker][Worker] =========
# 关联组件:
#   - [UpdateAssistantController][Controller]: Controller 创建并管理本线程实例
#   - [ServiceGithubRelease][Service]: 本线程调用 GitHub Release 检测服务
#   - [ServiceFileDownloader][Service]: 本线程调用文件下载服务
# ==============================================================================
class AssistantUpdateWorker(QThread):
    """
    更新助手检测与下载工作线程类，在子线程中执行 Release 检测和更新包下载。

    详细说明:
        继承自 QThread，支持两种运行模式（由 mode 参数指定）:
        - check 模式: 检测 GitHub 最新 Release，返回元数据和资产列表
        - download 模式: 下载指定的更新包文件，支持进度回调和协作式取消

        支持协作式取消（cooperative cancellation）:
        Controller 调用 request_cancel() 设置 _cancel_event，
        线程在网络请求边界和下载循环中检查该事件，尽快优雅退出。

    Signals:
        signal_progress (int): 进度信号，携带当前进度百分比
        signal_log (str): 日志信号，携带日志文本
        signal_release_meta (dict): Release 元数据信号，携带版本号、发布时间、更新说明
        signal_asset_list (list): 资产列表信号，携带所有 Release 资产信息
        signal_finish (dict): 完成信号，携带结果字典（ok、msg、saved_files 等）

    Attributes:
        _logger (logging.Logger): 日志器实例
        config (dict): 全量配置字典
        mode (str): 运行模式，"check" 或 "download"
        download_items (list): 要下载的资产文件名列表（download 模式使用）
        save_dir (str): 下载保存目录（download 模式使用）
        _cancel_event (threading.Event): 协作式取消事件对象
    """
    # 进度信号，参数为进度百分比整数
    signal_progress = Signal(int)
    # 日志信号，参数为日志文本字符串
    signal_log = Signal(str)
    # Release 元数据信号，参数为元数据字典
    signal_release_meta = Signal(dict)
    # 资产列表信号，参数为资产列表
    signal_asset_list = Signal(list)
    # 完成信号，参数为结果字典
    signal_finish = Signal(dict)

    def __init__(self, config: dict, mode: str = "check", parent=None):
        """
        初始化检测与下载工作线程。

        参数:
            config (dict): 全量配置字典，包含 version、github、proxy、file_io 等模块
            mode (str): 运行模式，"check" 或 "download"；默认为 "check"
            parent (QObject): 父对象；默认为 None

        返回值:
            None

        异常:
            无
        """
        # 调用父类 QThread 的构造函数
        super().__init__(parent)
        # 创建日志器实例，命名空间为 Updater.Worker
        self._logger = logging.getLogger("Updater.Worker")
        # 保存全量配置
        self.config = config
        # 保存运行模式
        self.mode = mode
        # 要下载的资产文件名列表（download 模式使用，初始为空）
        self.download_items = []
        # 下载保存目录（download 模式使用，初始为空）
        self.save_dir = ""
        # 协作式取消事件对象: Controller 在取消时 set，
        # 网络请求/分片循环在边界检查并尽快退出
        self._cancel_event = threading.Event()

    def request_cancel(self):
        """
        请求协作式取消当前任务。

        详细说明:
            设置 _cancel_event 事件标志，并请求线程中断。
            线程会在网络请求边界和下载循环中检查该事件，
            检测到后尽快优雅退出。

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 设置取消事件标志
        self._cancel_event.set()
        # 请求线程中断（Qt 标准取消机制）
        self.requestInterruption()

    def set_download_param(self, download_items: list, save_dir: str):
        """
        设置下载模式的参数（资产名列表和保存目录）。

        详细说明:
            download 模式下必须先调用此方法设置参数，
            再启动线程。check 模式不需要调用。

        参数:
            download_items (list): 要下载的资产文件名列表
            save_dir (str): 下载保存目录的绝对路径

        返回值:
            None

        异常:
            无
        """
        # 保存要下载的资产文件名列表
        self.download_items = download_items
        # 保存下载保存目录
        self.save_dir = save_dir

    def _log(self, msg: str):
        """
        内部日志辅助方法: 同时写文件日志和发射 UI 日志信号。

        参数:
            msg (str): 日志文本

        返回值:
            None

        异常:
            无
        """
        # 写入文件日志（INFO 级别）
        self._logger.info(msg)
        # 发射日志信号，通过信号槽回主线程更新 UI
        self.signal_log.emit(msg)

    def run(self):
        """
        线程主函数，在子线程中执行检测或下载任务。

        详细说明:
            根据 mode 参数执行不同的任务:
            1. 解析代理配置
            2. check 模式:
               - 创建 ServiceGithubRelease 服务
               - 获取最新 Release 信息
               - 发射 signal_release_meta 信号（版本号、发布时间、更新说明）
               - 发射 signal_asset_list 信号（资产列表）
               - 发射 signal_finish 信号（成功）
            3. download 模式（在 check 之后继续执行）:
               - 创建 ServiceFileDownloader 服务
               - 遍历下载列表，逐个下载文件
               - 每个文件下载进度通过 signal_progress 信号反馈
               - 下载完成后发射 signal_finish 信号（成功或失败）
            支持协作式取消: 在关键节点检查 _cancel_event，
            被取消时立即返回，不发射完成信号或发射取消状态。

        参数:
            无

        返回值:
            None（通过各种 signal 返回结果）

        异常:
            不向外抛出异常；所有异常都在内部捕获并通过 signal_finish 返回
        """
        try:
            # 延迟导入 service 模块（避免模块级循环依赖）
            from service.file_downloade import ServiceFileDownloader
            from service.github_update import ServiceGithubRelease

            # 从配置中提取版本配置
            version_cfg = self.config["version"]
            # 从配置中提取 GitHub 配置
            github_cfg = self.config["github"]
            # 解析实际生效的代理配置和代理来源
            proxies, proxy_source = resolve_effective_proxy(self.config["proxy"])
            # 获取仓库所有者
            gh_owner = github_cfg["GITHUB_REPO_OWNER"]
            # 获取仓库名称
            gh_repo = github_cfg["GITHUB_REPO_NAME"]
            # 记录任务开始日志
            self._logger.info(
                f"📦 更新助手任务开始(mode={self.mode}) | "
                f"仓库={gh_owner}/{gh_repo} | 代理来源={proxy_source}"
            )

            # =====** [Release检测阶段][Worker] =====
            # 创建 GitHub Release 服务实例
            gh_service = ServiceGithubRelease(
                # 仓库所有者
                repo_owner=gh_owner,
                # 仓库名称
                repo_name=gh_repo,
                # GitHub 访问令牌
                github_token=github_cfg.get("GITHUB_TOKEN"),
                # 请求超时时间
                timeout=github_cfg.get("GITHUB_TIMEOUT", 15),
                # 是否验证 SSL 证书（关闭以兼容自签名/中间人代理）
                verify_ssl=False,
                # 代理配置
                proxies=proxies,
                # API 基础地址（支持自定义网关/镜像）
                api_base_url=github_cfg.get("GITHUB_API_BASE_URL") or None,
            )
            try:
                # 调用服务获取最新版本信息
                info_ret = gh_service.get_latest_version_info()
                # 如果获取失败
                if not info_ret.get("ok"):
                    # 抛出运行时异常，由外层 try-except 捕获
                    raise RuntimeError(info_ret.get("error", "获取Release失败"))
                # 发射 Release 元数据信号（版本号、发布时间、更新说明）
                self.signal_release_meta.emit({
                    # 版本标签
                    "tag": info_ret.get("version_tag", ""),
                    # 发布时间
                    "publish_time": info_ret.get("publish_time", ""),
                    # 更新说明（Release body）
                    "release_note": info_ret.get("release_note", ""),
                })
                # 获取下载资产列表
                asset_list = info_ret.get("download_url_list", [])
                # 遍历资产列表，补充默认字段
                for item in asset_list:
                    # display_name 默认取 URL 的最后一段（实际文件名，用于模板匹配）
                    item.setdefault("display_name", item.get("url", "").rsplit("/", 1)[-1])
                    # label 默认与 display_name 相同（UI 展示名，可能是中文 label）
                    item.setdefault("label", item.get("display_name", ""))
                    # 原始名称默认为空（向后兼容）
                    item.setdefault("original_name", "")
                    # sha256 校验和默认为 None
                    item.setdefault("sha256", None)
                # 发射资产列表信号
                self.signal_asset_list.emit(asset_list)
            finally:
                # 确保服务关闭，释放资源
                gh_service.close()

            # 检查是否被取消
            if self._cancel_event.is_set():
                # 记录取消日志
                self._logger.warning("🛑 Release检查阶段被取消")
                # 直接返回，不发射完成信号
                return

            # 如果不是 download 模式（即 check 模式），任务完成
            if self.mode != "download":
                # 发射进度 100% 信号
                self.signal_progress.emit(100)
                # 发射完成信号（成功）
                self.signal_finish.emit({"ok": True, "msg": "Release信息加载完成",
                                        "asset_list": asset_list})
                # 返回结束线程
                return

            # =====** [下载阶段][Worker] =====
            # 如果没有下载参数（资产列表或保存目录为空），直接返回
            if not (self.download_items and self.save_dir):
                self.signal_finish.emit({"ok": True, "msg": "缺少下载参数"})
                return

            # 计算总文件数
            total_count = len(self.download_items)
            # 构建资产名到 URL 的映射字典
            name_url_map = {item["display_name"]: item["url"] for item in asset_list}
            # 构建资产名到 sha256 的映射字典
            name_sha_map = {item["display_name"]: item.get("sha256") for item in asset_list}
            # 初始化已保存文件路径列表
            saved_files: list = []

            # 获取文件 IO 配置
            file_io_cfg = self.config.get("file_io", {})
            # 创建文件下载器服务实例
            downloader = ServiceFileDownloader(
                # 代理配置
                proxies=proxies,
                # HTTP 请求超时秒数（大文件下载需要更长的读取窗口）
                timeout=github_cfg.get("GITHUB_TIMEOUT", 15) * 4,
                # 取消事件对象（用于协作式取消）
                cancel_event=self._cancel_event,
                # 日志器实例
                logger=self._logger,
                # 下载分片大小
                chunk_size=file_io_cfg.get("DOWNLOAD_CHUNK_SIZE", 1 * 1024 * 1024),
                # 下载最大并发数
                max_workers=file_io_cfg.get("DOWNLOAD_MAX_WORKERS", 4),
            )

            # 内部函数: 创建进度回调（闭包捕获文件索引）
            def _make_progress_cb(file_idx: int):
                """生成第 file_idx 个文件的进度回调函数"""
                def _cb(curr: int, total: int):
                    # 如果总大小大于 0，计算整体进度百分比
                    if total > 0:
                        # 整体进度 = (已完成文件数 + 当前文件进度) / 总文件数 * 100
                        overall = int((file_idx + curr / total) / total_count * 100)
                        # 发射进度信号，限制最大值为 99（完成时再设为 100）
                        self.signal_progress.emit(min(overall, 99))
                return _cb

            try:
                # 遍历所有要下载的文件
                for idx, asset_name in enumerate(self.download_items):
                    # 检查是否被取消
                    if self._cancel_event.is_set():
                        self._logger.warning("🛑 下载循环被取消,停止后续文件")
                        return
                    # 检查资产名是否存在于映射中
                    if asset_name not in name_url_map:
                        raise RuntimeError(f"资产不存在:{asset_name}")
                    # 记录开始下载日志
                    self._log(f"⬇️ [{idx + 1}/{total_count}] 开始下载:{asset_name}")
                    # 设置当前文件的进度回调
                    downloader.set_progress_callback(_make_progress_cb(idx))
                    # 调用下载器下载文件
                    dl_res = downloader.download(
                        # 下载 URL
                        download_url=name_url_map[asset_name],
                        # 保存目录
                        save_dir=self.save_dir,
                        # 显示名称
                        display_name=asset_name,
                        # 期望的 sha256 校验和
                        expect_sha256=name_sha_map.get(asset_name),
                    )
                    # 下载失败的情况
                    if not dl_res.get("success"):
                        # 如果是被取消导致的，直接返回
                        if self._cancel_event.is_set():
                            return
                        # 否则抛出异常
                        raise RuntimeError(f"{asset_name} 下载失败:{dl_res.get('error')}")
                    # 记录下载完成日志
                    self._log(
                        f"✅ [{idx + 1}/{total_count}] {asset_name} 下载完成,"
                        f"sha256校验:{dl_res.get('sha256_ok')}"
                    )
                    # 获取保存路径，没有则用目录 + 文件名拼接
                    save_path = dl_res.get("save_path") or os.path.join(self.save_dir, asset_name)
                    # 将保存路径加入列表
                    saved_files.append(save_path)
                    # 更新整体进度（当前文件完成）
                    self.signal_progress.emit(int((idx + 1) / total_count * 100))
            finally:
                # 确保下载器关闭，释放资源
                downloader.close()

            # 下载完成后再次检查是否被取消
            if self._cancel_event.is_set():
                return
            # 记录全部下载完成日志
            self._log(f"🎉 全部 {total_count} 个文件下载完成")
            # 发射完成信号（成功，携带已保存文件列表）
            self.signal_finish.emit({"ok": True, "msg": "全部文件下载完成", "saved_files": saved_files})

        except Exception as e:
            # 捕获所有异常
            # 如果是被取消导致的异常，静默返回
            if self._cancel_event.is_set():
                self._logger.warning(f"🛑 更新任务因取消退出:{e}")
                return
            # 记录错误日志（带堆栈）
            self._logger.error(f"❌ 更新任务异常:{e}", exc_info=True)
            # 发射错误日志信号
            self.signal_log.emit(f"❌ 任务异常:{e}")
            # 发射完成信号（失败）
            self.signal_finish.emit({"ok": False, "msg": str(e)})
