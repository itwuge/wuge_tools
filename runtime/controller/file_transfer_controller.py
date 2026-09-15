# -*- coding: utf-8 -*-
# ==============================================================================
# 文件名称: file_transfer_controller.py
# 文件路径: runtime/controller/file_transfer_controller.py
# ------------------------------------------------------------------------------
# 文件用途:
#   文件传输Tab控制器（File Transfer Controller），管理主窗口中"文件传输"
#   Tab页的所有业务逻辑，包括GitHub Release资产浏览、文件下载、
#   TUS协议文件上传等功能。
#
#   本控制器是纯粹的通用上传下载工具，与软件自更新业务【完全隔离】：
#   - 不拉取/比较版本号，不关心更新包文件名模板与当前平台
#   - 不做自动勾选/自动下载/安装移交；自动更新由独立更新助手负责
#   - 下载：浏览GitHub最新Release资产，勾选后通过通用多线程下载器落盘
#   - 上传：选择本地文件，经TUS v1.0协议分片上传到用户指定服务地址
# ------------------------------------------------------------------------------
# 架构定位:
#   位于MVC架构的【Controller层】，隶属于文件传输子模块：
#
#   ┌──────────────────────────────────────────────────────────────┐
#   │  MainWindow (View) — 文件传输Tab (main_window.py 中内联构建)  │
#   │   负责界面渲染、发出用户操作信号（sig_download_requested等） │
#   └────────────────────────┬─────────────────────────────────────┘
#                            │ 信号（Signal）
#   ┌────────────────────────▼─────────────────────────────────────┐
#   │           FileTransferController (本文件)                    │
#   │   信号绑定 → 任务编排 → 管理Worker → 结果回填View            │
#   └────────────┬───────────────────────────┬─────────────────────┘
#                │                           │
#   ┌────────────▼────────────┐  ┌───────────▼───────────────────┐
#   │  全局配置 (Model层)      │  │  Worker 线程（后台任务）       │
#   │  github.json /          │  │  ReleaseListWorker /          │
#   │  file_io.json           │  │  DownloadWorker /             │
#   │                          │  │  UploadWorker                 │
#   └─────────────────────────┘  └───────────────────────────────┘
#
#   视图内联在主窗口"文件传输"Tab（见main_window.py _build_tab_transfer）。
#   只读配置以系统配置落盘的 github.json / file_io.json 为唯一事实来源。
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 持有 MainWindow(View)，连接其文件传输Tab的所有用户交互信号
#   2. 创建/管理 ReleaseListWorker（资产浏览）、DownloadWorker（逐文件串行下载）、
#      UploadWorker（TUS上传）等子线程的生命周期
#   3. 将Worker的信号转换为View层的回填调用（进度、日志、结果等）
#   4. 提供忙碌状态查询和协作式停止方法，供主窗口关闭时调用
#   5. 维护下载队列状态，支持多文件串行下载
# ------------------------------------------------------------------------------
# 边界职责:
#   - 不写业务算法（具体业务在service层/Worker中实现）
#   - 不直接操作控件细节（只调View层公开接口）
#   - 不直接读写配置文件（配置通过MainController传入的全局配置获取）
#   - 与自更新业务完全隔离，不包含任何版本相关逻辑
# ------------------------------------------------------------------------------
# MVC定位:
#   Controller层 —— 文件传输Tab控制器。
#   负责文件传输Tab的业务逻辑编排，连接View层的用户交互信号，
#   管理各类Worker线程，并将结果回填到View层显示。
# ==============================================================================

# 导入 Python 标准库 logging 模块 —— 用于记录文件传输操作日志
import logging
# 导入 Python 标准库 os 模块 —— 用于文件路径操作和文件存在性检查
import os
# 导入 typing 模块中的 Optional 类型 —— 用于声明可能为None的类型
from typing import Optional

# 导入 PySide6 的 QObject 类 —— Qt基类，提供信号槽机制支持
# 导入 PySide6 的 Slot 装饰器 —— 用于将Python方法标记为Qt的槽函数
from PySide6.QtCore import QObject, Slot

# 导入 DownloadWorker 类 —— 文件下载后台工作线程
# 导入 UploadWorker 类 —— TUS协议上传后台工作线程
from runtime.workers.file_transfer_worker import DownloadWorker, UploadWorker
# 导入 ReleaseListWorker 类 —— GitHub Release资产列表拉取后台工作线程
from runtime.workers.release_list_worker import ReleaseListWorker

# 创建模块级日志记录器，名称为"FileTransferController"，便于在日志中区分来源
_logger = logging.getLogger("FileTransferController")


# =========** FileTransferController =========
class FileTransferController(QObject):
    """文件传输Tab控制器类（通用上传下载工具，与自更新业务完全隔离）。

    本类负责文件传输Tab的所有业务逻辑编排，包括：
    - GitHub Release资产列表浏览
    - 多文件串行下载（队列管理）
    - TUS v1.0协议文件上传
    - Worker线程生命周期管理
    - 主窗口关闭时的协作式停止

    继承自QObject以支持Qt信号槽机制。

    Attributes:
        config (dict): 全局合并配置字典，以系统配置JSON为准，单一事实来源。
            包含github配置、代理配置、文件IO配置等。
        view (MainWindow): 主窗口视图实例，文件传输区接口为其公开方法。
            由构造函数传入，本类不负责创建。
        list_worker (Optional[ReleaseListWorker]): Release资产列表拉取线程。
            为None表示无列表拉取任务在运行。
        download_worker (Optional[DownloadWorker]): 当前单文件下载线程。
            多文件按队列串行下载，每次只有一个下载线程在运行。
            为None表示无下载任务在运行。
        upload_worker (Optional[UploadWorker]): TUS上传线程。
            为None表示无上传任务在运行。
        _assets (list): 最近一次拉取的Release资产列表缓存。
            下载时按display_name映射url/sha256等信息。
        _dl_queue (list): 下载队列，存储待下载的文件信息字典。
            每个字典包含 display_name、url、sha256 等字段。
        _dl_save_dir (str): 下载文件的保存目录路径。
        _dl_idx (int): 当前下载进度索引（已完成的文件数）。
        _dl_saved (list): 已成功下载的文件保存路径列表。
        _stopping (bool): 关窗停止标记。
            True=停止后丢弃所有结果回调，不再弹窗/续传。

    Raises:
        无。构造函数不抛出异常。
    """

    def __init__(self, config: dict, main_view):
        """FileTransferController 构造函数。

        初始化文件传输控制器，保存配置和视图引用，
        初始化各Worker线程的占位属性和下载队列状态，
        绑定View层的文件传输Tab信号，
        并回填只读配置到View层展示。

        注意：创建时仅回填只读配置，资产列表不再自动拉取，
        由用户点击"获取资产"按钮手动触发（fetch_assets方法）。

        Args:
            config (dict): 全局配置字典，包含GitHub、代理、文件IO等配置。
            main_view (MainWindow): 主窗口视图实例，必须已完成UI构建。

        Returns:
            None

        Raises:
            无。构造函数不抛出异常。
        """
        # 调用父类QObject的构造函数，初始化Qt对象基础
        super().__init__()
        # 保存全局配置引用，后续所有Worker都使用此配置
        self.config = config
        # 保存主窗口视图引用，通过它访问文件传输Tab的UI接口
        self.view = main_view
        # Release资产列表拉取线程，初始为None（无任务运行）
        self.list_worker: Optional[ReleaseListWorker] = None
        # 当前下载工作线程，初始为None（无任务运行）
        self.download_worker: Optional[DownloadWorker] = None
        # TUS上传工作线程，初始为None（无任务运行）
        self.upload_worker: Optional[UploadWorker] = None
        # 最近一次Release资产列表缓存（下载时按display_name映射url/sha256）
        self._assets: list = []
        # 下载队列状态：待下载文件列表
        self._dl_queue: list = []
        # 下载队列状态：文件保存目录
        self._dl_save_dir: str = ""
        # 下载队列状态：当前下载进度索引（已完成的文件数）
        self._dl_idx: int = 0
        # 下载队列状态：已成功下载的文件路径列表
        self._dl_saved: list = []
        # 关窗停止标记：停止后丢弃所有结果回调，不再弹窗/续传
        self._stopping: bool = False
        # 绑定View层文件传输Tab的信号到对应的槽函数
        self._bind_signals()
        # 创建时仅回填只读配置（GitHub/文件IO配置展示）
        # 资产列表不再自动拉取，由用户点击"获取资产"按钮手动触发
        self.refresh_config(config)

    # ------------------------------------------------------------------
    # 会话与信号绑定
    # ------------------------------------------------------------------
    def fetch_assets(self):
        """用户点击"获取资产"按钮：触发拉取最新Release资产列表。

        只读配置（GitHub/文件IO）由构造时/系统配置保存后的refresh_config回填，
        本方法只负责触发列表拉取，不再与Tab切换、版本检查等动作耦合。

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 调用内部方法开始拉取Release资产列表
        self.start_release_listing()

    def refresh_config(self, config: dict):
        """系统配置保存后由MainController调用：更新事实来源并刷新只读展示。

        当用户在系统配置Tab保存了GitHub或代理设置后，MainController会
        调用此方法同步更新文件传输控制器的配置，并刷新View层的只读配置展示。

        Args:
            config (dict): 最新的全局配置字典。

        Returns:
            None

        Raises:
            无。
        """
        # 更新本地配置引用（单一事实来源）
        self.config = config
        # 刷新View层的GitHub配置只读展示
        self.view.fill_github_config_view(self.config)
        # 刷新View层的文件IO配置只读展示
        self.view.fill_fileio_config_view(self.config)

    def _bind_signals(self):
        """绑定View层文件传输Tab的所有用户交互信号到对应的槽函数。

        视图信号只连接一次：控制器随主窗口存活，Tab不关闭/重建。

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 下载请求信号：用户勾选资产并点击下载按钮时触发
        # 连接到 _on_download_requested 槽函数
        self.view.sig_download_requested.connect(self._on_download_requested)
        # 上传请求信号：用户选择文件并点击上传按钮时触发
        # 连接到 _on_upload_requested 槽函数
        self.view.sig_upload_requested.connect(self._on_upload_requested)

    # ------------------------------------------------------------------
    # Release资产列表浏览(仅元数据,不含任何版本语义)
    # ------------------------------------------------------------------
    def start_release_listing(self):
        """开始拉取GitHub最新Release资产列表。

        执行流程：
        1. 检查是否已有列表拉取任务在运行（防止重复发起）
        2. 重置进度条、更新状态栏和日志
        3. 创建ReleaseListWorker并连接各种信号
        4. 启动Worker线程

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 检查是否已有列表拉取任务在运行
        if self.list_worker and self.list_worker.isRunning():
            # 正在运行则直接返回，不重复发起
            return
        # 记录日志：开始拉取Release资产列表
        _logger.info("📋 文件传输Tab:开始拉取GitHub最新Release资产列表")
        # 重置文件传输进度条为0%
        self.view.set_transfer_progress(0)
        # 设置状态栏提示
        self.view.set_status("正在获取GitHub Release资产……")
        # 在文件传输日志面板追加日志
        self.view.append_transfer_log("🔍 开始获取最新Release资产列表……")

        # 创建Release资产列表拉取工作线程，传入全局配置
        self.list_worker = ReleaseListWorker(self.config)
        # 连接Worker的日志信号到View的追加传输日志方法
        self.list_worker.signal_log.connect(self.view.append_transfer_log)
        # 连接Worker的进度信号到View的设置传输进度方法
        self.list_worker.signal_progress.connect(self.view.set_transfer_progress)
        # 连接Worker的资产列表信号到本类的资产列表处理槽函数
        self.list_worker.signal_asset_list.connect(self._on_asset_list)
        # 连接Worker的完成信号到本类的列表完成处理槽函数
        self.list_worker.signal_finish.connect(self._on_list_finished)
        # 启动Worker线程，开始拉取资产列表
        self.list_worker.start()

    @Slot(list)
    def _on_asset_list(self, asset_list: list):
        """处理资产列表拉取结果回调，回填到View层。

        通用工具不自动勾选任何资产（不做平台更新包匹配），
        完全由用户手动选择要下载的文件。

        Args:
            asset_list (list): Release资产列表，每个元素为字典，包含：
                - display_name (str): 显示名称
                - url (str): 下载URL
                - sha256 (str | None): 文件SHA256校验值
                - size (int): 文件大小（字节）
                - 其他元数据字段

        Returns:
            None

        Raises:
            无。
        """
        # 保存资产列表到本地缓存，供下载时按名称映射URL和SHA256
        self._assets = asset_list or []
        # 将资产列表回填到View层的下载列表中（供用户勾选）
        self.view.fill_download_assets(self._assets)
        # 记录日志：已回填资产数量
        _logger.info(f"📦 已回填Release资产{len(self._assets)}个(等待用户勾选)")

    @Slot(dict)
    def _on_list_finished(self, res: dict):
        """处理资产列表拉取完成回调（成功或失败）。

        Args:
            res (dict): 完成结果字典，包含：
                - ok (bool): 是否成功
                - msg (str): 结果消息（失败时为错误原因）

        Returns:
            None

        Raises:
            无。
        """
        # 检查是否成功
        if res.get("ok"):
            # 成功：更新状态栏
            self.view.set_status("Release资产已加载")
        else:
            # 失败：记录错误日志
            _logger.error(f"❌ Release资产列表拉取失败:{res.get('msg', '未知错误')}")
            # 更新状态栏为失败状态
            self.view.set_status("Release资产获取失败")
            # 非停止状态下才弹窗报错（关窗停止时静默丢弃）
            if not self._stopping:
                self.view.show_error("任务失败", res.get("msg", "未知错误"))

    # ------------------------------------------------------------------
    # 下载:勾选资产 → 逐文件串行(每个文件一个通用DownloadWorker)
    # ------------------------------------------------------------------
    @Slot(list, str)
    def _on_download_requested(self, display_names: list, save_dir: str):
        """处理下载请求：用户勾选资产并选择保存目录后点击下载。

        执行流程：
        1. 检查是否有任务在运行（防止并发）
        2. 根据显示名称从资产列表缓存中映射URL和SHA256
        3. 校验所有勾选的资产都在最新列表中（过期则提示刷新）
        4. 初始化下载队列状态
        5. 启动第一个文件的下载

        Args:
            display_names (list): 用户勾选的资产显示名称列表
            save_dir (str): 用户选择的文件保存目录路径

        Returns:
            None

        Raises:
            无。
        """
        # 检查是否有任务正在运行（下载/上传/列表拉取任一在运行都拒绝）
        if self.is_busy():
            # 弹出警告提示
            self.view.show_warning("提示", "任务正在执行,请等待完成！")
            # 直接返回
            return
        # 构建显示名称到下载URL的映射字典（从资产列表缓存中查找）
        name_url_map = {item.get("display_name", ""): item.get("url", "") for item in self._assets}
        # 构建显示名称到SHA256校验值的映射字典
        name_sha_map = {item.get("display_name", ""): item.get("sha256") for item in self._assets}
        # 初始化下载队列（待下载文件列表）
        queue = []
        # 初始化缺失列表（不在最新资产列表中的文件）
        missing = []
        # 遍历用户勾选的所有显示名称
        for name in display_names:
            # 检查名称是否在URL映射中（即是否在最新资产列表中）
            if name in name_url_map:
                # 在列表中：将文件信息加入下载队列
                queue.append({
                    "display_name": name,              # 显示名称
                    "url": name_url_map[name],         # 下载URL
                    "sha256": name_sha_map.get(name),  # SHA256校验值（可能为None）
                })
            else:
                # 不在列表中：加入缺失列表
                missing.append(name)
        # 检查是否有缺失的文件（资产列表已过期）
        if missing:
            # 弹出错误提示，告知用户资产列表已过期，需要刷新
            self.view.show_error(
                "下载失败",
                "资产列表已过期,以下文件不在最新Release中,请点击\"获取资产\"刷新后再试:\n"
                + "\n".join(f"· {n}" for n in missing),
            )
            # 直接返回，不执行下载
            return
        # 检查下载队列是否为空（理论上不应为空，做防御性检查）
        if not queue:
            # 弹出警告提示
            self.view.show_warning("提示", "未获取到可下载的资产列表！")
            # 直接返回
            return

        # 初始化下载队列：保存待下载文件列表
        self._dl_queue = queue
        # 初始化下载保存目录
        self._dl_save_dir = save_dir
        # 初始化下载进度索引（从第0个开始）
        self._dl_idx = 0
        # 初始化已下载文件路径列表
        self._dl_saved = []
        # 重置文件传输进度条为0%
        self.view.set_transfer_progress(0)
        # 设置状态栏提示
        self.view.set_status("开始下载文件……")
        # 禁用传输操作按钮（防止重复点击）
        self.view.set_transfer_enabled(False)
        # 在传输日志面板追加日志：本次下载文件总数和保存目录
        self.view.append_transfer_log(f"⬇️ 本次共{len(queue)}个文件,保存目录:{save_dir}")
        # 启动第一个文件的下载
        self._start_next_download()

    def _start_next_download(self):
        """启动队列中的下一个文件下载。

        队首文件下载完成后由结果槽调用，推进到下一个文件；
        全部完成则调用收尾方法。

        对每个下载文件：
        1. 创建DownloadWorker实例
        2. 连接日志、进度、结果信号
        3. 启动Worker线程
        4. 进度信号使用闭包包装，计算整体进度（考虑队列位置）

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 检查是否已完成所有文件的下载
        if self._dl_idx >= len(self._dl_queue):
            # 全部完成：调用全部下载完成的收尾方法
            self._on_all_downloads_finished()
            # 直接返回
            return
        # 获取当前要下载的文件信息（队首）
        item = self._dl_queue[self._dl_idx]
        # 当前文件在队列中的索引（从0开始）
        idx = self._dl_idx
        # 队列总文件数
        total = len(self._dl_queue)

        # 定义进度回调闭包：将单文件进度转换为整体进度
        # curr: 当前已下载字节数；tot: 文件总字节数
        def _on_progress(curr: int, tot: int):
            # 总字节数大于0时才计算（避免除零）
            if tot > 0:
                # 计算整体进度百分比：
                # (已完成的文件数 + 当前文件进度) / 总文件数 * 100
                overall = int((idx + curr / tot) / total * 100)
                # 更新View层的传输进度条，最大不超过99%（全部完成后设为100%）
                self.view.set_transfer_progress(min(overall, 99))

        # 在传输日志面板追加日志：开始下载第N个文件
        self.view.append_transfer_log(f"⬇️ [{idx + 1}/{total}] 开始下载:{item['display_name']}")
        # 创建下载工作线程，传入配置和下载参数
        worker = DownloadWorker(
            config=self.config,                   # 全局配置
            download_url=item["url"],             # 下载URL
            save_dir=self._dl_save_dir,           # 保存目录
            display_name=item["display_name"],    # 显示文件名
            expect_sha256=item["sha256"],         # 期望的SHA256校验值
        )
        # 连接Worker的日志信号到View的追加传输日志方法
        worker.signal_log.connect(self.view.append_transfer_log)
        # 连接Worker的进度信号到上面定义的进度回调闭包
        worker.signal_progress.connect(_on_progress)
        # 连接Worker的结果信号到本类的单文件下载完成槽函数
        worker.signal_result.connect(self._on_one_download_finished)
        # 保存当前下载Worker引用
        self.download_worker = worker
        # 启动Worker线程，开始下载
        worker.start()

    @Slot(dict)
    def _on_one_download_finished(self, res: dict):
        """处理单个文件下载完成的结果回调。

        执行流程：
        1. 如果正在停止（关窗），静默丢弃结果，不弹窗不续传
        2. 获取当前下载的文件名
        3. 下载失败：弹窗报错，清空队列，终止下载流程
        4. 下载成功：记录已保存路径，推进索引，启动下一个下载

        Args:
            res (dict): 下载结果字典，包含：
                - success (bool): 是否下载成功
                - error (str): 失败原因（失败时存在）
                - save_path (str): 保存的文件路径（成功时存在）
                - local_filename (str): 本地文件名

        Returns:
            None

        Raises:
            无。
        """
        # 关窗停止路径：静默丢弃结果，不弹窗不续传
        if self._stopping:
            # 直接返回，不做任何处理
            return
        # 获取当前下载文件的显示名称
        # 如果索引有效，从队列中获取；否则从结果中获取本地文件名
        if self._dl_idx < len(self._dl_queue):
            display_name = self._dl_queue[self._dl_idx]["display_name"]
        else:
            display_name = str(res.get("local_filename", ""))
        # 检查下载是否失败
        if not res.get("success"):
            # 下载失败：记录错误日志
            _logger.error(f"❌ 文件下载失败:{display_name} | {res.get('error', '未知错误')}")
            # 更新状态栏为失败状态
            self.view.set_status("下载失败")
            # 恢复传输操作按钮为可用状态
            self.view.set_transfer_enabled(True)
            # 弹出错误提示框
            self.view.show_error("下载失败", f"{display_name} 下载失败:{res.get('error', '未知错误')}")
            # 清空下载队列（失败后不继续下载后续文件）
            self._dl_queue = []
            # 直接返回
            return
        # 下载成功：获取保存路径，优先从结果中获取，否则用目录+显示名拼接
        save_path = res.get("save_path") or os.path.join(self._dl_save_dir, display_name)
        # 将保存路径加入已下载列表
        self._dl_saved.append(save_path)
        # 在传输日志面板追加日志：第N个文件下载完成
        self.view.append_transfer_log(
            f"✅ [{self._dl_idx + 1}/{len(self._dl_queue)}] {display_name} 下载完成"
        )
        # 下载索引+1，推进到下一个文件
        self._dl_idx += 1
        # 更新整体进度条（按已完成文件数/总文件数计算）
        self.view.set_transfer_progress(int(self._dl_idx / len(self._dl_queue) * 100))
        # 启动下一个文件的下载
        self._start_next_download()

    def _on_all_downloads_finished(self):
        """全部文件下载完成的收尾处理。

        执行流程：
        1. 记录完成日志
        2. 更新状态栏和进度条
        3. 恢复传输操作按钮
        4. 在日志面板追加完成信息
        5. 弹窗提示下载完成
        6. 清空下载队列

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 获取已下载文件总数
        total = len(self._dl_saved)
        # 记录日志：全部文件下载完成
        _logger.info(f"🎉 全部{total}个文件下载完成")
        # 更新状态栏
        self.view.set_status("下载任务已结束")
        # 恢复传输操作按钮为可用状态
        self.view.set_transfer_enabled(True)
        # 设置进度条为100%
        self.view.set_transfer_progress(100)
        # 在传输日志面板追加完成日志
        self.view.append_transfer_log(f"🎉 全部{total}个文件下载完成")
        # 弹出成功提示框，显示下载文件数和保存目录
        self.view.show_info("下载完成", f"全部{total}个文件已保存到:\n{self._dl_save_dir}")
        # 清空下载队列
        self._dl_queue = []

    # ------------------------------------------------------------------
    # 上传:本地文件 → 用户指定TUS服务地址(单文件)
    # ------------------------------------------------------------------
    @Slot(str, str)
    def _on_upload_requested(self, tus_endpoint: str, local_file_path: str):
        """处理上传请求：用户输入TUS服务地址并选择本地文件后点击上传。

        执行流程：
        1. 检查是否有任务在运行（防止并发）
        2. 校验本地文件是否存在
        3. 重置进度条、更新状态栏和日志
        4. 创建UploadWorker并连接信号
        5. 启动Worker线程执行TUS上传

        Args:
            tus_endpoint (str): TUS服务创建端点URL（上传目标地址）
            local_file_path (str): 本地待上传文件的路径

        Returns:
            None

        Raises:
            无。
        """
        # 检查是否有任务正在运行（下载/上传/列表拉取任一在运行都拒绝）
        if self.is_busy():
            # 弹出警告提示
            self.view.show_warning("提示", "任务正在执行,请等待完成！")
            # 直接返回
            return
        # 校验本地文件是否存在
        if not os.path.isfile(local_file_path):
            # 文件不存在：弹出错误提示
            self.view.show_error("上传失败", f"本地文件不存在:{local_file_path}")
            # 直接返回
            return

        # 重置文件传输进度条为0%
        self.view.set_transfer_progress(0)
        # 设置状态栏提示
        self.view.set_status("正在上传文件……")
        # 禁用传输操作按钮（防止重复点击）
        self.view.set_transfer_enabled(False)
        # 在传输日志面板追加日志：上传文件名和目标地址
        self.view.append_transfer_log(
            f"📤 上传文件:{os.path.basename(local_file_path)} -> {tus_endpoint}"
        )

        # 创建TUS上传工作线程，传入配置和上传参数
        worker = UploadWorker(
            config=self.config,                   # 全局配置
            tus_create_endpoint=tus_endpoint,     # TUS服务创建端点URL
            local_file_path=local_file_path,      # 本地文件路径
            metadata=None,                        # 额外元数据（暂无）
        )
        # 连接Worker的日志信号到View的追加传输日志方法
        worker.signal_log.connect(self.view.append_transfer_log)
        # 连接Worker的进度信号到View的设置传输进度方法
        worker.signal_progress.connect(self.view.set_transfer_progress)
        # 连接Worker的结果信号到本类的上传完成槽函数
        worker.signal_result.connect(self._on_upload_finished)
        # 保存当前上传Worker引用
        self.upload_worker = worker
        # 启动Worker线程，开始上传
        worker.start()

    @Slot(dict)
    def _on_upload_finished(self, res: dict):
        """处理上传完成的结果回调。

        Args:
            res (dict): 上传结果字典，包含：
                - success (bool): 是否上传成功
                - upload_url (str): 上传后的文件访问URL（成功时存在）
                - error (str): 失败原因（失败时存在）

        Returns:
            None

        Raises:
            无。
        """
        # 关窗停止路径：静默丢弃结果，不弹窗
        if self._stopping:
            # 直接返回，不做任何处理
            return
        # 更新状态栏
        self.view.set_status("上传任务已结束")
        # 恢复传输操作按钮为可用状态
        self.view.set_transfer_enabled(True)
        # 检查是否上传成功
        if res.get("success"):
            # 上传成功：记录日志
            _logger.info(f"🎉 文件上传成功:{res.get('upload_url', '')}")
            # 设置进度条为100%
            self.view.set_transfer_progress(100)
            # 弹出成功提示框，显示上传后的文件URL
            self.view.show_info(
                "上传完成",
                f"文件已上传到:\n{res.get('upload_url', '')}",
            )
        else:
            # 上传失败：记录错误日志
            _logger.error(f"❌ 文件上传失败:{res.get('error', '未知错误')}")
            # 弹出错误提示框
            self.view.show_error("上传失败", res.get("error", "未知错误"))

    # ------------------------------------------------------------------
    # 忙碌查询 / 协作停止(主窗口关闭时由 MainController 调用)
    # ------------------------------------------------------------------
    def is_busy(self) -> bool:
        """查询是否有任务正在运行。

        列表拉取、下载、上传任一线程仍在运行都算忙碌。
        供MainController在关闭窗口时调用，判断是否需要弹出确认框。

        Args:
            无参数。

        Returns:
            bool: True=有任务在运行；False=所有任务都已结束。

        Raises:
            无。
        """
        # 使用any()函数检查三个Worker中是否有正在运行的
        # 每个Worker需要同时满足：不为None 且 正在运行
        return any(
            w is not None and w.isRunning()
            for w in (self.list_worker, self.download_worker, self.upload_worker)
        )

    def stop_workers(self):
        """协作式中断所有运行中的Worker线程。

        中断策略：
        1. 先设置停止标记，后续结果回调会被静默丢弃
        2. 对支持协作取消的Worker（列表拉取、下载），优先调用request_cancel
        3. 不支持的调用requestInterruption
        4. 有界等待线程退出（默认3秒）
        5. 超时未退出才terminate强杀（最后手段，可能导致资源泄漏）

        terminate()会强杀线程，跳过finally块，可能导致HTTP响应/会话/
        线程池泄漏，故仅作为最后手段。
        UploadWorker底层暂无取消事件，走requestInterruption+等待。

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 设置停止标记：后续所有结果回调都会被静默丢弃，不再弹窗/续传
        self._stopping = True
        # 资产列表/下载器支持协作取消（在分片chunk边界检查取消标志并退出）
        # 遍历这两个Worker，逐个处理
        for w in (self.list_worker, self.download_worker):
            # 跳过None和未运行的Worker
            if w is None or not w.isRunning():
                continue
            # 如果Worker支持request_cancel方法（协作式取消）
            if hasattr(w, "request_cancel"):
                # 调用协作取消方法
                w.request_cancel()
            else:
                # 否则使用Qt标准的中断请求方法
                w.requestInterruption()
            # 等待线程退出，最多等3秒
            if not w.wait(3000):
                # 3秒内未退出：强制终止线程（最后手段）
                w.terminate()
                # 再等1秒确保线程完全结束
                w.wait(1000)
        # 处理上传器：先请求中断并给予优雅期，超时强杀兜底
        w = self.upload_worker
        # 检查上传Worker是否存在且正在运行
        if w is not None and w.isRunning():
            # 请求中断
            w.requestInterruption()
            # 等待线程退出，最多等3秒
            if not w.wait(3000):
                # 3秒内未退出：记录警告日志
                _logger.warning("🛑 上传线程3秒内未退出,terminate强制结束")
                # 强制终止线程
                w.terminate()
                # 再等1秒确保线程完全结束
                w.wait(1000)
