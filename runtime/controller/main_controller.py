# -*- coding: utf-8 -*-
# ==============================================================================
# 文件名称: main_controller.py
# 文件路径: runtime/controller/main_controller.py
# ------------------------------------------------------------------------------
# 文件用途:
#   主窗口控制器（Main Controller），是整个应用的核心控制器类。
#   本文件负责连接主窗口视图（MainWindow）与数据模型（MainModel），
#   处理用户交互事件、编排业务逻辑、管理后台工作线程（Worker）的生命周期。
# ------------------------------------------------------------------------------
# 架构定位:
#   位于MVC架构的【Controller层】核心位置，是主窗口MVC三层的"大脑"：
#
#   ┌──────────────────────────────────────────────────────────────┐
#   │                    MainWindow (View)                        │
#   │   负责界面渲染、发出用户操作信号（sig_xxx）                  │
#   └────────────────────────┬─────────────────────────────────────┘
#                            │ 信号（Signal）
#   ┌────────────────────────▼─────────────────────────────────────┐
#   │               MainController (本文件)                        │
#   │   信号绑定 → 业务编排 → 调用Model/Worker → 结果回填View      │
#   └────────────┬───────────────────────────┬─────────────────────┘
#                │                           │
#   ┌────────────▼────────────┐  ┌───────────▼───────────────────┐
#   │     MainModel (Model)   │  │  Worker 线程（后台任务）       │
#   │  配置读写/账号管理/      │  │  SignMailWorker /             │
#   │  签到记录/版本信息       │  │  BatchSignWorker /            │
#   │                          │  │  VersionCheckWorker           │
#   └─────────────────────────┘  └───────────────────────────────┘
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 持有 MainWindow(View) 与 MainModel(Model)，连接View层全部用户交互信号
#   2. 启动时让Model加载数据并回填到View（初始数据填充）
#   3. 表单保存（Tab1路径+邮件、Tab2 GitHub+代理）：先做输入校验，
#      再调用Model落盘，并刷新全局配置
#   4. 账号管理：增删账号、选中账号显示详情
#   5. 签到功能：单账号签到（后台SignMailWorker）、批量签到（BatchSignWorker）
#   6. 版本检测与更新：后台检测、确认后移交外部更新助手
#   7. 文件传输Tab控制器（FileTransferController）的懒加载与管理
#   8. 主窗口关闭确认：检查后台任务运行状态，协作式中断后关闭
# ------------------------------------------------------------------------------
# 边界职责:
#   - 不创建控件、不写样式（渲染归View层）
#   - 不直接读写json文件、不直接发网络请求（数据归Model，IO归Worker）
#   - 仅负责业务逻辑的编排与协调，不实现具体的业务算法
#   - 文件传输Tab的具体逻辑由 FileTransferController 负责，本类仅做懒加载
#     与系统配置变更时的刷新通知
# ------------------------------------------------------------------------------
# MVC定位:
#   Controller层 —— 主窗口控制器，是主窗口MVC架构的核心协调者。
#   接收View层的用户操作信号，调用Model层处理数据、启动Worker执行后台任务，
#   并将结果回填到View层显示。
# ==============================================================================

# 导入 Python 标准库 logging 模块 —— 用于记录控制器操作日志，
# 日志会随 root 的 app.log FileHandler 落盘到日志文件中
import logging
# 导入 typing 模块中的 TYPE_CHECKING 和 Optional 类型
# TYPE_CHECKING 用于类型注解时的条件导入（避免运行时循环导入）
# Optional 用于声明可能为None的类型
from typing import TYPE_CHECKING, Optional

# 导入 PySide6 的 Slot 装饰器 —— 用于将Python方法标记为Qt的槽函数，
# 以便与信号（Signal）连接
from PySide6.QtCore import Slot
# 导入 PySide6 的 QApplication 类 —— Qt应用全局实例，用于退出应用等全局操作
from PySide6.QtWidgets import QApplication

# 导入 MainModel 类 —— 主窗口数据模型，负责所有数据的读写操作
from runtime.model.main_model import MainModel
# 导入 format_user_info_text 函数 —— 用于格式化账号详情文本，
# 界面面板、签到邮件、结果文件三处共用同一格式化函数
from runtime.model.sign_tools import format_user_info_text
# 导入 common_dialog 模块 —— 通用对话框工具，提供确认框等UI组件
from runtime.view import common_dialog
# 导入 MainWindow 类 —— 主窗口视图，负责界面渲染与用户交互
from runtime.view.main_window import MainWindow
# 导入 BatchSignWorker 类 —— 批量签到后台工作线程
from runtime.workers.batch_sign_worker import BatchSignWorker
# 导入 SignMailWorker 类 —— 单账号签到后台工作线程（含邮件通知功能）
from runtime.workers.sign_mail_worker import SignMailWorker
# 导入 VersionCheckWorker 类 —— 版本检测后台工作线程
from runtime.workers.version_check_worker import VersionCheckWorker

# TYPE_CHECKING 在运行时恒为False，此处的导入仅用于静态类型检查（IDE提示、mypy等）
# 运行时在函数内懒导入，避免控制器包间循环导入
if TYPE_CHECKING:
    # 仅用于类型标注:运行时在函数内懒导入，避免控制器包间循环导入
    from runtime.controller.file_transfer_controller import FileTransferController

# 创建模块级日志记录器，名称为"MainController"，便于在日志中区分来源
# 日志会随 root 的 app.log FileHandler 落盘到日志文件
_logger = logging.getLogger("MainController")


# =========** MainController =========
class MainController:
    """主窗口控制器类。

    本类是主窗口MVC架构的核心协调者，负责：
    - 连接View层的所有用户交互信号
    - 调用Model层进行数据读写
    - 管理各类后台Worker线程的生命周期
    - 将业务结果回填到View层显示

    Attributes:
        model (MainModel): 主窗口数据模型实例，负责所有数据的读写操作。
            由构造函数传入，本类不负责创建。
        view (MainWindow): 主窗口视图实例，负责界面渲染与用户交互信号发出。
            由构造函数传入，本类不负责创建。
        sign_worker (Optional[SignMailWorker]): 当前单账号签到工作线程。
            为None表示无签到任务在运行。
        batch_worker (Optional[BatchSignWorker]): 当前批量签到工作线程。
            为None表示无批量签到任务在运行。
        transfer_controller (Optional[FileTransferController]): 文件传输Tab
            控制器实例，懒加载模式（用户首次点击"获取资产"时才创建）。
            与自更新业务完全隔离。为None表示尚未创建。
        version_check_worker (Optional[VersionCheckWorker]): 版本检测工作线程。
            为None表示无版本检测任务在运行。
        _check_silent (bool): 标记当前版本检测是否为静默模式。
            True=启动后静默检测（无新版/失败不弹窗）；
            False=用户手动点击检查（必反馈）。
        _self_update_in_progress (bool): 标记是否正在进行自更新流程。
            True=已移交给更新助手，关闭窗口时跳过确认直接退出。

    Raises:
        无。构造函数不抛出异常，异常在各业务方法中处理。
    """

    def __init__(self, model: MainModel, view: MainWindow):
        """MainController 构造函数。

        初始化主窗口控制器，保存Model和View的引用，
        初始化各Worker线程的占位属性，
        绑定View层的信号到对应的槽函数，
        并加载初始数据回填到View层。

        Args:
            model (MainModel): 主窗口数据模型实例，必须已完成初始化。
            view (MainWindow): 主窗口视图实例，必须已完成UI构建。

        Returns:
            None

        Raises:
            无。构造函数不抛出异常。
        """
        # 保存数据模型引用，后续通过它进行所有数据的读写操作
        self.model = model
        # 保存视图引用，后续通过它进行UI操作和信号连接
        self.view = view
        # 单账号签到工作线程，初始为None（无任务运行）
        self.sign_worker: Optional[SignMailWorker] = None
        # 批量签到工作线程，初始为None（无任务运行）
        self.batch_worker: Optional[BatchSignWorker] = None
        # 文件传输Tab控制器，懒加载模式，初始为None（尚未创建）
        # 使用字符串类型注解（forward reference）避免循环导入问题
        self.transfer_controller: Optional["FileTransferController"] = None
        # 版本检测工作线程，初始为None（无任务运行）
        self.version_check_worker: Optional[VersionCheckWorker] = None
        # 版本检测静默模式标记，初始为False（非静默）
        self._check_silent: bool = False
        # 自更新流程进行中标记，初始为False（未在更新）
        self._self_update_in_progress: bool = False
        # 绑定View层所有信号到对应的槽函数
        self._bind_signals()
        # 加载初始数据并回填到View层
        self._load_initial_data()

    # ------------------------------------------------------------------
    # 绑定 & 初始化
    # ------------------------------------------------------------------
    def _bind_signals(self):
        """绑定View层所有用户交互信号到对应的槽函数。

        本方法在构造函数中调用一次，建立View与Controller之间的信号连接。
        包括：Tab配置保存、账号管理、签到操作、版本更新、文件传输、窗口关闭等。

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # ---- 配置管理Tab独立保存信号 ----
        self.view.sig_save_path.connect(self._on_save_path)      # 路径配置保存 -> 路径独立处理槽
        self.view.sig_save_mail.connect(self._on_save_mail)      # 邮件配置保存 -> 邮件独立处理槽
        self.view.sig_save_github.connect(self._on_save_github)  # GitHub配置保存 -> GitHub独立处理槽
        self.view.sig_save_fileio.connect(self._on_save_fileio)  # 下载配置保存 -> 文件IO独立处理槽
        self.view.sig_save_proxy.connect(self._on_save_proxy)    # 代理配置保存 -> 代理独立处理槽
        # 检测系统代理按钮信号，连接到 _on_detect_proxy 槽函数
        self.view.sig_detect_proxy.connect(self._on_detect_proxy)

        # ---- 账号管理相关信号 ----
        # 添加账号信号，连接到 _on_add_account 槽函数
        self.view.sig_add_account.connect(self._on_add_account)
        # 删除账号信号，连接到 _on_del_account 槽函数
        self.view.sig_del_account.connect(self._on_del_account)
        # 账号选中信号，连接到 _on_account_selected 槽函数
        self.view.sig_account_selected.connect(self._on_account_selected)

        # ---- 签到与版本更新相关信号 ----
        # 单账号签到开始信号，连接到 _on_sign_started 槽函数
        self.view.sig_sign_started.connect(self._on_sign_started)
        # 批量签到信号，连接到 _on_sign_batch 槽函数
        self.view.sig_sign_batch.connect(self._on_sign_batch)
        # 打开更新检测信号，连接到 _on_open_update 槽函数
        self.view.sig_open_update.connect(self._on_open_update)

        # ---- 文件传输Tab相关信号 ----
        # 用户点击"获取资产"按钮信号，连接到 _on_fetch_assets 槽函数
        # 懒创建传输器并拉取Release资产列表
        self.view.sig_fetch_assets_requested.connect(self._on_fetch_assets)

        # ---- 窗口关闭相关信号 ----
        # 窗口关闭请求信号，连接到 _on_close_requested 槽函数
        self.view.sig_close_requested.connect(self._on_close_requested)

    def _load_initial_data(self):
        """加载初始数据并回填到主窗口。

        本方法在构造函数中调用，从Model层加载表单初始数据（配置、账号列表等），
        然后调用View层的方法将数据回填到界面上。

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。异常由Model层抛出，此处不捕获（启动期失败应直接暴露）。
        """
        # 记录日志：开始加载初始数据
        _logger.info("📂 加载表单初始数据并回填主窗口...")
        # 从Model层加载表单数据（配置、账号列表、邮件设置等）
        data = self.model.load_form_data()
        # 将加载的数据回填到View层的表单控件中
        self.view.fill_form_data(data)
        # 记录日志：初始数据回填完成，包含账号数和邮件开关状态
        _logger.info(
            f"✅ 初始数据回填完成:账号数={len(data.get('accounts', []))},"
            f"邮件开关={'开' if data.get('mail', {}).get('enable_mail') else '关'}"
        )
        # 启动时无选中账号，第三栏（今日签到获取）显示"暂无"占位
        self.view.update_selected_gain("")

    def _refresh_account_board(self):
        """刷新账号列表和全账号统计（前两栏），并同步第三栏。

        刷新内容包括：
        1. 账号列表（第一栏）：从Model获取最新账号列表并刷新
        2. 全账号签到统计（第二栏）：从Model获取签到汇总数据
        3. 当前选中账号的今日签到获取（第三栏）：同步为当前选中账号的数据
           无选中账号或未签到时界面显示"暂无"

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 刷新账号列表和签到统计（前两栏）：从Model获取最新数据并传给View
        self.view.refresh_accounts(
            self.model.list_accounts(), self.model.get_checkin_summary()
        )
        # 获取当前选中的账号邮箱（从View层获取当前列表选中项）
        selected = self.view.current_selected_account()
        # 更新第三栏（今日签到获取）：显示选中账号的今日签到收益
        # 如果未选中或未签到，Model会返回空字符串，View显示"暂无"
        self.view.update_selected_gain(self.model.get_selected_today_gain(selected))

    # ------------------------------------------------------------------
    # 配置管理Tab:五类配置独立保存
    # ------------------------------------------------------------------
    @Slot(dict)
    def _on_save_path(self, payload: dict):
        """保存路径配置并根据目录变化结果提示是否需要重启。

        Args:
            payload (dict): 数据目录全路径及各业务子目录相对路径。

        Returns:
            None

        Raises:
            无。模型异常在本方法内记录并转换为界面错误提示。
        """
        try:  # 调用Model保存路径配置并统一处理重启提示
            ret = self.model.save_path_config(payload)
            tip = "路径配置已保存！"
            if ret.get("data_root_changed"):
                tip += ("\n\n⚠ 数据目录已更改，重启程序后生效。程序会复制现有数据到新目录，"
                        "旧目录保留不删。")
            elif ret.get("need_restart"):
                tip += "\n\n⚠ 配置目录或日志目录已更改，重启程序后生效。"
            self.view.show_info("保存成功", tip)
        except Exception as e:
            _logger.error(f"❌ 路径配置保存异常:{e}", exc_info=True)
            self.view.show_error("保存失败", f"路径配置保存异常:{e}")

    @Slot(dict)
    def _on_save_mail(self, payload: dict):
        """校验SMTP端口并独立保存邮件开关及SMTP配置。

        Args:
            payload (dict): 邮件开关、SMTP服务器、端口、邮箱及授权码。

        Returns:
            None
        """
        try:  # SMTP端口必须是可转换为整数的文本
            payload["smtp_port"] = int(str(payload.get("smtp_port", "")).strip())
        except ValueError:
            self.view.show_warning("输入校验", "SMTP端口必须填写数字！")
            return
        try:
            self.model.save_mail_config(payload)
            self.view.show_info("保存成功", "邮件配置已保存！")
        except Exception as e:
            _logger.error(f"❌ 邮件配置保存异常:{e}", exc_info=True)
            self.view.show_error("保存失败", f"邮件配置保存异常:{e}")

    @Slot(dict)
    def _on_save_github(self, payload: dict):
        """独立保存GitHub仓库、令牌及更新检查配置。

        Args:
            payload (dict): GitHub仓库信息、令牌、更新包模板及自动检查开关。

        Returns:
            None
        """
        try:  # 保存成功后同步刷新已创建的文件传输控制器
            self.model.save_github_only(payload)
            self.view.show_info("保存成功", "GitHub配置已保存！")
            self._refresh_transfer_config()
        except Exception as e:
            _logger.error(f"❌ GitHub配置保存异常:{e}", exc_info=True)
            self.view.show_error("保存失败", f"GitHub配置保存异常:{e}")

    @Slot(dict)
    def _on_save_fileio(self, payload: dict):
        """校验并独立保存下载/上传分片大小与并发数。

        Args:
            payload (dict): 下载、上传分片大小与最大并发数文本。

        Returns:
            None
        """
        try:  # 四个文件IO参数均要求为整数
            for key in ("download_chunk", "download_workers", "upload_chunk", "upload_workers"):
                payload[key] = int(str(payload.get(key, "")).strip())
        except ValueError:
            self.view.show_warning("输入校验", "下载配置必须全部填写数字！")
            return
        try:
            self.model.save_fileio_only(payload)
            self.view.show_info("保存成功", "下载配置已保存！")
            self._refresh_transfer_config()
        except Exception as e:
            _logger.error(f"❌ 下载配置保存异常:{e}", exc_info=True)
            self.view.show_error("保存失败", f"下载配置保存异常:{e}")

    @Slot(dict)
    def _on_save_proxy(self, payload: dict):
        """校验代理端口并独立保存自动检测或手动代理配置。

        Args:
            payload (dict): 代理模式开关、HTTP/HTTPS地址及端口。

        Returns:
            None
        """
        auto_detect = bool(payload.get("auto_detect", False))  # 是否启用运行时自动检测系统代理
        port_text = str(payload.get("proxy_port", "")).strip()
        try:
            payload["proxy_port"] = int(port_text) if port_text else (0 if auto_detect else 38457)
        except ValueError:
            self.view.show_warning("输入校验", "代理端口必须填写数字！")
            return
        try:
            self.model.save_proxy_only(payload)
            tip = "代理配置已保存！"
            if auto_detect:
                tip += "\n\n当前为自动检测模式，运行时实时跟随系统代理。"
            self.view.show_info("保存成功", tip)
            self._refresh_transfer_config()
        except Exception as e:
            _logger.error(f"❌ 代理配置保存异常:{e}", exc_info=True)
            self.view.show_error("保存失败", f"代理配置保存异常:{e}")

    def _refresh_transfer_config(self):
        """若文件传输控制器已创建,立即同步最新全局配置。

        :return: 无返回值
        :rtype: None
        """
        if self.transfer_controller is not None:          # 懒加载控制器未创建时无需额外处理
            self.transfer_controller.refresh_config(     # 将Model最新配置推送给文件传输模块
                self.model.get_global_config()
            )

    @Slot()
    def _on_detect_proxy(self):
        """读取操作系统代理设置并回填到表单。

        当用户点击"检测系统代理"按钮时调用，从操作系统读取当前启用的
        代理设置，解析后回填到手动代理表单中，并弹出检测结果提示。

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。所有异常在方法内捕获并处理。
        """
        # 记录日志：用户点击了检测系统代理按钮
        _logger.info("🔍 用户点击\"检测系统代理\",开始读取操作系统代理设置")
        # 使用 try-except 包裹检测操作，捕获可能的异常
        try:
            # 调用Model层检测系统代理，返回代理字典和检测来源
            # proxies: 代理配置字典，如 {"http": "http://host:port", "https": "..."}
            # source: 检测来源说明（如注册表、环境变量等）
            proxies, source = self.model.detect_system_proxy()
        except Exception as e:
            # 捕获检测过程中的所有异常
            # 记录错误日志
            _logger.error(f"❌ 检测系统代理异常:{e}", exc_info=True)
            # 在View层弹出错误提示框
            self.view.show_error("检测系统代理", f"检测异常:{e}")
            # 直接返回
            return
        # 检查是否检测到了代理配置
        if not proxies:
            # 未检测到已启用的系统代理
            # 记录信息日志
            _logger.info(f"ℹ️ \"检测系统代理\"完成:未检测到已启用的系统代理(来源:{source})")
            # 在View层弹出提示：未检测到系统代理
            self.view.show_info("检测系统代理", f"未检测到已启用的系统代理。\n来源:{source}")
            # 直接返回
            return
        # 检测到系统代理：记录日志
        _logger.info(f"🌐 \"检测系统代理\"完成:检测到系统代理 -> {proxies}(来源:{source})")
        # 从urllib.parse导入urlsplit函数，用于解析代理URL
        from urllib.parse import urlsplit
        # 保留完整代理URL回填，避免SOCKS协议被拆成纯主机后丢失scheme
        http_url = str(proxies.get("http", "") or "")
        https_url = str(proxies.get("https", "") or http_url)
        http_parts = urlsplit(http_url)                   # 仅用于提取统一端口
        http_host = http_url                              # 地址框允许保存http://或socks5h://完整URL
        https_host = https_url                            # HTTPS地址同样保留完整协议信息
        port = str(http_parts.port or "")                 # 端口仍单独回填供现有配置结构使用
        # 将检测到的代理信息回填到View层的手动代理表单中
        self.view.fill_detected_proxy(http_host, https_host, port)
        # 构造详细的代理信息文本，用于弹窗显示
        detail = f"HTTP:  {proxies.get('http', '')}\nHTTPS: {proxies.get('https', '')}"
        # 在View层弹出成功提示，包含检测到的代理详情和操作提示
        self.view.show_info(
            "检测系统代理",
            f"检测到系统代理({source}):\n{detail}\n\n已自动填入手动代理表单,"
            f"确认后请点击\"保存配置\"。"
        )

    # ------------------------------------------------------------------
    # 账号管理
    # ------------------------------------------------------------------
    @Slot(str, str)
    def _on_add_account(self, email: str, password: str):
        """处理添加账号请求。

        执行流程：
        1. 校验邮箱和密码不能为空
        2. 调用Model层添加账号（新账号则添加，已存在则更新密码）
        3. 刷新账号列表和统计
        4. 在日志面板追加操作结果

        Args:
            email (str): 账号邮箱地址
            password (str): 登录密码

        Returns:
            None

        Raises:
            无。
        """
        # 去除邮箱首尾空格，处理空值
        email = (email or "").strip()
        # 校验邮箱不能为空
        if not email:
            # 在View层弹出警告提示
            self.view.show_warning("提示", "账号邮箱不能为空！")
            # 直接返回
            return
        # 校验密码不能为空
        if not password:
            # 在View层弹出警告提示，说明保存密码的必要性
            self.view.show_warning("提示", "登录密码不能为空！\n需保存密码后才能自动登录与批量签到。")
            # 直接返回
            return
        # 调用Model层添加账号
        # 返回值 is_new: True=新账号；False=已存在，本次仅更新其登录密码
        is_new = self.model.add_account(email, password)
        # 刷新账号列表和签到统计看板
        self._refresh_account_board()
        # 根据是否为新账号，在日志面板追加不同的提示
        if is_new:
            # 新账号：提示已添加并保存密码
            self.view.append_log(f"✅ 已添加账号并保存登录密码:{email}")
        else:
            # 已存在账号：提示密码已更新
            self.view.append_log(f"🔑 账号已存在,登录密码已更新:{email}")

    @Slot(str)
    def _on_del_account(self, email: str):
        """处理删除账号请求。

        删除为危险且不可逆操作，必须弹出二次确认框（红色确认按钮），
        用户确认后才执行删除。删除时会一并删除该账号的本地详情数据。

        Args:
            email (str): 要删除的账号邮箱地址

        Returns:
            None

        Raises:
            无。
        """
        # 校验邮箱不能为空（未选中账号时可能为空）
        if not email:
            # 在View层弹出警告提示：请先选中账号
            self.view.show_warning("提示", "请先在左侧列表选中账号！")
            # 直接返回
            return
        # 弹出二次确认对话框（危险操作，使用红色确认按钮）
        # confirmed: True=用户确认删除；False=用户取消
        confirmed = self.view.show_question(
            "确认删除账号",  # 对话框标题
            f"确定要删除账号 {email} 吗?\n该账号的本地详情数据将一并删除,此操作不可恢复。",  # 提示内容
            ok_text="确认删除",  # 确认按钮文字
            cancel_text="取消",  # 取消按钮文字
            danger=True,  # 危险操作标记（确认按钮显示为红色）
        )
        # 检查用户是否确认删除
        if not confirmed:
            # 用户取消删除：记录日志
            _logger.info(f"ℹ️ 用户取消删除账号:{email}")
            # 直接返回，不执行删除操作
            return
        # 用户确认删除：调用Model层删除账号及其本地数据
        self.model.remove_account(email)
        # 刷新账号列表和签到统计看板
        self._refresh_account_board()
        # 清空右侧账号详情面板（账号已删除，详情不再有效）
        self.view.clear_account_detail()
        # 在日志面板追加删除成功提示
        self.view.append_log(f"❌ 已删除账号:{email}")
        # 记录删除完成日志
        _logger.info(f"🗑️ 确认删除账号完成:{email}")

    @Slot(str)
    def _on_account_selected(self, email: str):
        """处理账号选中事件。

        当用户在左侧账号列表中点击选中某个账号时调用，
        加载该账号的本地详情、今日签到收益和已保存的密码，
        并回填到右侧详情面板。

        Args:
            email (str): 选中的账号邮箱地址

        Returns:
            None

        Raises:
            无。
        """
        # 记录调试日志：选中账号，开始加载详情
        _logger.debug(f"👤 选中账号,加载本地详情:{email}")
        # 从Model层获取账号详情，使用统一的formatter渲染详情文本
        # 界面面板与签到邮件/结果文件共用同一formatter渲染同一份详情文本
        self._show_account_detail(email, self.model.get_account_detail(email))
        # 更新第三栏（今日签到获取）：显示选中账号今日签到获得的流量
        # 未签到时返回空字符串，View显示"暂无"
        self.view.update_selected_gain(self.model.get_selected_today_gain(email))
        # 回填已持久化的登录密码到签到密码输入框
        # 单账号签到无需重复手输（密码不打日志，保护用户隐私）
        self.view.fill_sign_password(self.model.get_account_password(email))

    def _show_account_detail(self, email: str, detail):
        """用统一的formatter渲染账号详情并推给View层纯显示。

        界面面板、签到邮件正文、结果文件三个出口共用 format_user_info_text
        函数渲染同一份文本，确保信息展示的一致性。
        当detail为None时（账号删除/失败已清旧），渲染占位块。

        Args:
            email (str): 账号邮箱地址
            detail (dict | None): 账号详情数据字典，为None时显示占位内容

        Returns:
            None

        Raises:
            无。
        """
        # 调用统一的格式化函数生成详情文本，然后传给View层显示
        self.view.show_account_detail_text(format_user_info_text(email, detail))

    # ------------------------------------------------------------------
    # 单账号签到
    # ------------------------------------------------------------------
    @Slot(str, str)
    def _on_sign_started(self, account: str, password: str):
        """处理单账号签到请求。

        执行流程：
        1. 校验账号和密码不能为空
        2. 检查是否已有签到任务在运行（防止重复发起）
        3. 清空日志、重置进度条、设置状态
        4. 创建SignMailWorker并设置签到参数
        5. 连接Worker的各种信号到对应的槽函数
        6. 启动Worker线程执行签到

        Args:
            account (str): 签到账号邮箱
            password (str): 签到密码

        Returns:
            None

        Raises:
            无。
        """
        # 去除账号首尾空格，处理空值
        account = (account or "").strip()
        # 校验账号和密码不能为空
        if not account or not password:
            # 记录警告日志
            _logger.warning("⚠ 签到发起失败:账号或密码为空")
            # 在View层弹出警告提示
            self.view.show_warning("输入校验", "签到账号与密码不能为空！")
            # 直接返回
            return
        # 检查是否已有单账号签到任务在运行
        if self.sign_worker and self.sign_worker.isRunning():
            # 记录警告日志：重复发起被拒绝
            _logger.warning(f"⚠ 签到任务正在运行,拒绝重复发起:{account}")
            # 在View层弹出警告提示
            self.view.show_warning("提示", "签到任务正在运行,请等待完成！")
            # 直接返回
            return

        # 记录日志：发起单账号签到任务，包含账号和邮件开关状态（密码不落日志）
        _logger.info(
            f"🚀 发起单账号签到任务:账号={account}(密码不落日志),"
            f"邮件通知={'开' if self.model.is_mail_enabled() else '关'}"
        )
        # 清空日志面板
        self.view.clear_log()
        # 重置进度条为0%
        self.view.set_progress(0)
        # 设置状态栏为"正在执行签到"
        self.view.set_status("正在执行签到")
        # 禁用签到按钮，防止重复点击
        self.view.set_sign_enabled(False)

        # 创建单账号签到工作线程，传入全局配置
        self.sign_worker = SignMailWorker(self.model.get_global_config())
        # 设置签到参数：账号、密码、是否启用邮件通知
        self.sign_worker.set_sign_param({
            "account": account,
            "password": password,
            "enable_mail": self.model.is_mail_enabled(),
        })
        # 连接Worker的日志信号到View的追加日志方法
        self.sign_worker.signal_log.connect(self.view.append_log)
        # 连接Worker的进度信号到View的设置进度方法
        self.sign_worker.signal_progress.connect(self.view.set_progress)
        # 连接Worker的结果信号到本类的结果处理槽函数
        self.sign_worker.signal_result.connect(self._on_sign_result)
        # 启动Worker线程，开始执行签到任务
        self.sign_worker.start()

    @Slot(dict)
    def _on_sign_result(self, res: dict):
        """处理单账号签到结果回调。

        执行流程：
        1. 恢复签到按钮可用状态，重置状态栏
        2. 签到成功：
           - 弹窗提示成功
           - 刷新账号列表和统计
           - 重新选中刚签到的账号，同步详情和今日收益
           - 如果是新账号，自动添加到账号列表
        3. 签到失败：
           - 弹窗提示失败
           - 刷新看板，清除该账号的昨日残留状态

        Args:
            res (dict): 签到结果字典，包含以下键：
                - ok (bool): 是否签到成功
                - msg (str): 结果消息
                - email (str): 签到账号邮箱
                - detail (dict | None): 签到后的账号详情（Worker带回的副本）

        Returns:
            None

        Raises:
            无。
        """
        # 恢复签到按钮为可用状态
        self.view.set_sign_enabled(True)
        # 设置状态栏为"空闲"
        self.view.set_status("空闲")
        # 从结果中获取账号邮箱，如果没有则从View层获取当前选中账号
        account = res.get("email", "") or self.view.current_selected_account()
        # 检查签到是否成功
        if res.get("ok"):
            # 签到成功：弹出成功提示框
            self.view.show_info("签到结果", res.get("msg", "签到流程完成"))
            # 详情已由Worker按"清旧→签到→存新"流水线落盘
            # 此处只取回详情用于UI回填
            # Worker随结果带detail副本时优先使用，避免再读一次盘
            detail = res.get("detail")
            # 检查detail是否为字典类型（确保数据有效性）
            if not isinstance(detail, dict):
                # 如果detail不是字典或不存在，则从Model层重新读取
                detail = self.model.get_account_detail(account)
            # 记录日志：签到成功，详情已由Worker持久化
            _logger.info(f"💾 签到成功,账号详情已由Worker持久化:{account}")
            # 检查账号是否在账号列表中，如果不在则补入（新账号签到场景）
            # 并顺手记住本次登录密码（后续可直接批量签到）
            if account and account not in self.model.list_accounts():
                # 新账号：添加到账号列表，保存密码
                self.model.add_account(account, self.view.sign_password())
            # 刷新全账号统计看板
            # 注意：列表重建会丢失选中状态
            self._refresh_account_board()
            # 重新选中刚签到的账号，使其详情面板与"今日签到获取"栏
            # 立即反映本次签到结果
            if account and self.view.select_account(account):
                # 刷新详情面板显示
                self._show_account_detail(account, detail)
                # 刷新今日签到获取栏
                self.view.update_selected_gain(
                    self.model.get_selected_today_gain(account)
                )
        else:
            # 签到失败：记录警告日志
            _logger.warning(f"⚠ 签到任务返回失败:{account} | {res.get('msg', '签到失败')}")
            # 弹出失败警告框
            self.view.show_warning("签到结果", res.get("msg", "签到失败"))
            # 任务开头已清旧且失败不写新快照
            # 刷新看板清掉该账号的昨日残留状态（如旧的签到数据）
            self._refresh_account_board()
            # 重新选中账号，同步详情面板和今日收益栏
            if account and self.view.select_account(account):
                # 刷新详情面板（可能已清空）
                self._show_account_detail(account, self.model.get_account_detail(account))
                # 刷新今日签到获取栏（可能变为"暂无"）
                self.view.update_selected_gain(
                    self.model.get_selected_today_gain(account)
                )

    @Slot()
    def _on_sign_batch(self):
        """处理批量签到请求。

        执行流程：
        1. 校验账号列表不为空
        2. 获取已保存密码的账号列表（批量只跑保存了密码的账号）
        3. 检查是否有签到任务正在运行（单账号或批量），防止并发
        4. 清空日志、重置进度、设置状态
        5. 创建BatchSignWorker并连接信号
        6. 启动批量签到线程

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 从Model层获取所有已保存密码的账号凭证列表
        credentials = self.model.list_account_credentials()
        # 从Model层获取所有账号列表（用于计算缺密码的账号数）
        all_accounts = self.model.list_accounts()
        # 校验账号列表不为空
        if not all_accounts:
            # 弹出警告：账号列表为空
            self.view.show_warning("提示", "账号列表为空,请先添加账号！")
            # 直接返回
            return
        # 校验是否有保存了密码的账号
        if not credentials:
            # 没有任何账号保存了密码：弹出详细提示
            self.view.show_warning(
                "无法批量签到",
                "没有任何账号保存了登录密码。\n请在左侧填好账号+密码后点击\"添加账号\"保存。",
            )
            # 直接返回
            return
        # 检查批量签到任务是否正在运行
        if self.batch_worker and self.batch_worker.isRunning():
            # 记录警告日志：重复点击被忽略
            _logger.warning("⚠ 批量签到正在运行,忽略重复点击")
            # 弹出警告提示
            self.view.show_warning("提示", "批量签到正在运行,请等待完成！")
            # 直接返回
            return
        # 检查单账号签到任务是否正在运行
        # 单账号任务也在跑时不并发，避免同账号双会话/Cookie写冲突
        if self.sign_worker and self.sign_worker.isRunning():
            # 弹出警告提示
            self.view.show_warning("提示", "单账号签到正在运行,请等待完成！")
            # 直接返回
            return

        # 计算缺少密码的账号列表（所有账号中没有保存密码的）
        missing = [e for e in all_accounts if e not in {c["email"] for c in credentials}]
        # 记录日志：发起批量签到，包含已保存密码和缺密码的数量
        _logger.info(
            f"🚀 发起批量签到:已保存密码{len(credentials)}个,缺密码{len(missing)}个"
        )
        # 清空日志面板
        self.view.clear_log()
        # 重置进度条为0%
        self.view.set_progress(0)
        # 设置状态栏为批量签到进度（0/N）
        self.view.set_status(f"正在批量签到(0/{len(credentials)})")
        # 禁用签到按钮，防止重复点击
        self.view.set_sign_enabled(False)
        # 如果有缺少密码的账号，在日志面板追加提示
        if missing:
            self.view.append_log(f"⚠ 以下{len(missing)}个账号未保存密码,本次跳过:{', '.join(missing)}")

        # 创建批量签到工作线程，传入凭证列表和全局配置
        self.batch_worker = BatchSignWorker(credentials, self.model.get_global_config())
        # 连接Worker的日志信号到View的追加日志方法
        self.batch_worker.signal_log.connect(self.view.append_log)
        # 连接Worker的进度信号到本类的进度处理槽函数
        self.batch_worker.signal_progress.connect(self._on_batch_progress)
        # 连接Worker的单账号完成信号到本类的处理槽函数
        self.batch_worker.signal_item_done.connect(self._on_batch_item_done)
        # 连接Worker的最终结果信号到本类的结果处理槽函数
        self.batch_worker.signal_result.connect(self._on_batch_result)
        # 启动Worker线程，开始执行批量签到
        self.batch_worker.start()

    @Slot(int)
    def _on_batch_progress(self, pct: int):
        """处理批量签到进度更新回调。

        根据总进度百分比计算已完成的账号数，更新状态栏显示。

        Args:
            pct (int): 总进度百分比（0-100）

        Returns:
            None

        Raises:
            无。
        """
        # 更新进度条显示
        self.view.set_progress(pct)
        # 获取总账号数（已保存密码的账号数）
        total = len(self.model.list_account_credentials())
        # 根据百分比计算已完成的账号数（四舍五入）
        done = round(pct * total / 100) if total else 0
        # 更新状态栏显示：正在批量签到(done/total)
        self.view.set_status(f"正在批量签到({done}/{total})")

    @Slot(dict)
    def _on_batch_item_done(self, item: dict):
        """处理批量签到中单账号完成的回调。

        每个账号签到完成后都会调用此方法，用于实时刷新：
        1. "今日已签到"统计（通过刷新整个看板实现）
        2. 如果刚完成的账号正是当前选中项，同步刷新其详情面板与今日收益栏

        Args:
            item (dict): 单个账号的签到结果，包含：
                - email (str): 账号邮箱
                - checkin_ok (bool): 是否签到成功
                - 其他结果字段

        Returns:
            None

        Raises:
            无。
        """
        # 刷新账号列表和签到统计看板（实时更新今日已签到数量）
        self._refresh_account_board()
        # 获取刚完成的账号邮箱
        email = str(item.get("email", "")).strip()
        # 获取当前选中的账号
        selected = self.view.current_selected_account()
        # 如果刚完成的账号正是当前选中项
        if email and selected == email:
            # 同步刷新其详情面板
            self._show_account_detail(email, self.model.get_account_detail(email))
            # 同步刷新今日签到收益栏
            self.view.update_selected_gain(self.model.get_selected_today_gain(email))

    @Slot(list)
    def _on_batch_result(self, results: list):
        """处理批量签到最终结果回调。

        执行流程：
        1. 恢复签到按钮可用状态，重置状态栏
        2. 统计结果：新签成功数、已签跳过数、失败数
        3. 刷新账号列表和统计，恢复之前的选中项
        4. 在日志面板追加汇总信息
        5. 弹窗显示结果（有失败时显示失败明细）

        Args:
            results (list): 所有账号的签到结果列表，每个元素为dict，包含：
                - email (str): 账号邮箱
                - checkin_ok (bool): 是否签到成功
                - already_signed (bool): 是否今日已签（跳过）
                - error (str): 失败原因（失败时存在）

        Returns:
            None

        Raises:
            无。
        """
        # 恢复签到按钮为可用状态
        self.view.set_sign_enabled(True)
        # 设置状态栏为"空闲"
        self.view.set_status("空闲")
        # 详情已由BatchSignWorker按账号"清旧→签到→存新"逐条落盘
        # 此处只统计新签/跳过/失败并刷新UI，不再二次映射/存盘
        # 初始化成功计数器
        ok_cnt = 0
        # 初始化跳过计数器（今日已签的）
        skip_cnt = 0
        # 初始化失败列表（存储失败的账号和原因）
        fail_items = []
        # 遍历所有结果，统计各类情况
        for item in results or []:
            # 获取账号邮箱
            email = str(item.get("email", "")).strip()
            # 邮箱为空则跳过（无效数据）
            if not email:
                continue
            # 检查是否签到成功
            if item.get("checkin_ok"):
                # 成功计数+1
                ok_cnt += 1
            # 检查是否今日已签（跳过）
            elif item.get("already_signed"):
                # 跳过计数+1
                skip_cnt += 1
            else:
                # 失败：将账号和失败原因加入失败列表
                fail_items.append((email, item.get("error") or "未知原因"))
        # 列表重建前记住当前选中项，刷新后恢复选中，并同步其详情与今日收益栏
        prev_selected = self.view.current_selected_account()
        # 刷新账号列表和签到统计看板
        self._refresh_account_board()
        # 如果之前有选中的账号，尝试恢复选中
        if prev_selected and self.view.select_account(prev_selected):
            # 刷新详情面板
            self._show_account_detail(
                prev_selected, self.model.get_account_detail(prev_selected)
            )
            # 刷新今日签到收益栏
            self.view.update_selected_gain(
                self.model.get_selected_today_gain(prev_selected)
            )
        # 计算总账号数
        total = len(results or [])
        # 计算失败数量
        fail_cnt = len(fail_items)
        # 构造汇总文本
        summary = f"新签成功{ok_cnt}/{total},今日已签跳过{skip_cnt}个,失败{fail_cnt}个"
        # 在日志面板追加批量签到完成的汇总信息
        self.view.append_log(f"🏁 批量签到完成:{summary}")
        # 记录批量签到结果日志
        _logger.info(f"🎉 批量签到结果已回填并持久化:{summary}")
        # 检查是否有失败的账号
        if fail_items:
            # 有失败：构造失败明细文本（最多显示10个）
            fail_text = "\n".join(f"· {e}:{r}" for e, r in fail_items[:10])
            # 如果失败超过10个，追加"另有N个"提示
            more = f"\n……另有{len(fail_items) - 10}个" if len(fail_items) > 10 else ""
            # 弹出警告提示框，包含汇总和失败明细
            self.view.show_warning(
                "批量签到完成(有失败)",
                f"{summary}\n\n失败明细:\n{fail_text}{more}",
            )
        else:
            # 全部成功或跳过：弹出成功提示框
            self.view.show_info("批量签到", f"批量签到完成！\n{summary}")

    # ------------------------------------------------------------------
    # 版本检测 & 移交给外部更新助手
    # ------------------------------------------------------------------
    def startup_auto_check_update(self):
        """主窗口就绪后的静默版本检测入口。

        启动后静默检测版本：
        - 失败或无新版均不打扰用户（不弹窗）
        - 仅发现新版本时才弹出确认框询问用户是否更新

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 调用内部通用方法，传入silent=True表示静默模式
        self._trigger_version_check(silent=True)

    @Slot()
    def _on_open_update(self):
        """处理"检查更新"按钮点击事件。

        用户手动点击检查更新按钮时调用，后台检测版本，
        有更新则弹出确认框，用户确认后移交给更新助手。
        非静默模式：无论成功/失败/无新版都会给用户反馈。

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 调用内部通用方法，传入silent=False表示非静默模式（手动点击）
        self._trigger_version_check(silent=False)

    def _trigger_version_check(self, silent: bool):
        """启动版本检测Worker的通用方法。

        执行流程：
        1. 检查是否已有版本检测任务在运行（防止重复发起）
        2. 记录检测模式（静默/非静默）
        3. 创建VersionCheckWorker并连接结果信号
        4. 启动Worker线程
        5. 非静默模式下更新状态栏

        Args:
            silent (bool): 检测模式标记：
                - True: 启动后静默检测（无新版/失败不弹窗，仅记录日志）
                - False: 用户手动点击检测（必反馈，弹窗显示结果）

        Returns:
            None

        Raises:
            无。
        """
        # 检查是否已有版本检测任务在运行
        if self.version_check_worker is not None and self.version_check_worker.isRunning():
            # 如果不是静默模式（用户手动点击），弹出提示
            if not silent:
                # 记录警告日志
                _logger.warning("⚠ 版本检测正在进行,忽略重复点击")
                # 弹出警告提示
                self.view.show_warning("提示", "正在检查版本,请稍候...")
            # 静默模式下直接返回，不做任何提示
            return
        # 保存当前检测模式标记，供结果回调使用
        self._check_silent = silent
        # 记录日志：触发版本检测，包含模式信息
        _logger.info(f"🔍 触发版本检测(silent={silent})")
        # 创建版本检测工作线程，传入全局配置
        self.version_check_worker = VersionCheckWorker(self.model.get_global_config())
        # 连接Worker的结果信号到本类的结果处理槽函数
        self.version_check_worker.signal_result.connect(self._on_version_check_result)
        # 启动Worker线程，开始检测版本
        self.version_check_worker.start()
        # 如果是非静默模式（用户手动点击），更新状态栏提示
        if not silent:
            self.view.set_status("正在检查最新版本...")

    @Slot(dict)
    def _on_version_check_result(self, res: dict):
        """处理版本检测结果回调。

        根据检测模式（静默/非静默）决定是否弹窗：
        - 静默模式：只有发现新版本才打扰用户（弹出确认框）
        - 非静默模式：无论成功/失败/无新版都会给用户反馈

        Args:
            res (dict): 版本检测结果字典，包含以下键：
                - ok (bool): 检测是否成功
                - msg (str): 结果消息（失败时为错误原因）
                - has_new_version (bool): 是否有新版本
                - remote_version (str): 远程最新版本号

        Returns:
            None

        Raises:
            无。
        """
        # 获取当前检测是否为静默模式，默认False（非静默）
        silent = getattr(self, "_check_silent", False)
        # 如果是非静默模式，恢复状态栏为"空闲"
        if not silent:
            self.view.set_status("空闲")
        # 检查检测是否成功
        if not res.get("ok"):
            # 检测失败：记录错误日志
            _logger.error(f"❌ 版本检测失败:{res.get('msg', '未知错误')}")
            # 非静默模式下弹出错误提示框
            if not silent:
                self.view.show_error("检查更新失败", res.get("msg", "未知错误"))
            # 静默模式下不弹窗，直接返回
            return

        # 检测成功，检查是否有新版本
        if not res.get("has_new_version", False):
            # 无新版本：记录日志
            _logger.info("✅ 当前已是最新版本")
            # 非静默模式下弹出提示：当前已是最新版本
            if not silent:
                self.view.show_info("检查更新", "当前已是最新版本！")
            # 静默模式下不弹窗，直接返回
            return

        # 发现新版本：获取远程版本号
        remote_version = res.get("remote_version", "")
        # 获取本地版本号
        local_version = self.model.get_local_version()
        # 弹出确认对话框，询问用户是否立即更新
        # confirmed: True=用户确认更新；False=用户选择暂不更新
        confirmed = common_dialog.show_confirm(
            self.view,  # 父窗口
            "发现新版本",  # 对话框标题
            f"检测到新版本 {remote_version}\n当前版本 {local_version}\n\n"
            f"确认后软件将关闭,由更新助手完成下载与安装,完成后自动重启。",  # 提示内容
            ok_text="立即更新",  # 确认按钮文字
            cancel_text="暂不更新",  # 取消按钮文字
        )
        # 检查用户是否确认更新
        if not confirmed:
            # 用户选择暂不更新：记录日志
            _logger.info(f"ℹ️ 用户选择暂不更新,留在当前版本 {local_version}")
            # 直接返回
            return
        # 用户确认更新：移交给外部更新助手处理
        self._handoff_to_external_updater(remote_version)

    def _handoff_to_external_updater(self, remote_version: str):
        """写入更新任务JSON并启动同目录更新助手，随后主程序退出释放文件锁。

        任务JSON透传配置/版本/PID/路径等信息，更新助手以任务模式启动：
        - 无需自举扫描目录，启动更快
        - old_pid 精准等待主程序退出，避免按进程名误等

        执行流程：
        1. 检查更新助手是否可用
        2. 写入更新任务JSON文件
        3. 启动更新助手
        4. 标记自更新进行中，强制关闭主窗口，退出应用

        Args:
            remote_version (str): 远程新版本号

        Returns:
            None

        Raises:
            无。所有异常在方法内捕获并处理。
        """
        # 懒导入更新相关模块，避免顶层循环导入
        from runtime.model.update_launcher import (
            is_update_assistant_available,  # 检查更新助手是否可用
            launch_update_assistant,        # 启动更新助手
            write_update_task,              # 写入更新任务JSON
        )

        # 检查更新助手是否存在（用户可能只拷贝了主程序，没有更新助手）
        # 文件传输Tab是通用工具，与更新业务隔离，不再承担更新包自动下载
        # 如果更新助手缺失，直接提示无法自动更新
        if not is_update_assistant_available():
            # 记录错误日志：未找到更新助手
            _logger.error("⚠ 未找到同目录更新助手,无法自动更新")
            # 弹出错误提示框，说明无法自动更新的原因和解决方法
            self.view.show_error(
                "无法自动更新",
                "软件目录中未找到更新助手(update/update.exe),无法完成自动下载与安装。\n"
                "请重新下载完整软件包,或将更新助手放回软件目录后重试。",
            )
            # 直接返回
            return

        # 写入更新任务JSON文件：只传配置定位契约/版本/PID,更新助手按需读取真实配置
        ok, task_path, msg = write_update_task(
            local_version=self.model.get_local_version(),  # 本地版本号
            remote_version=remote_version,  # 目标远程版本号
        )
        # 检查任务文件是否写入成功
        if not ok:
            # 写入失败：记录错误日志
            _logger.error(f"⚠ 写入更新任务失败:{msg}")
            # 弹出错误提示框
            self.view.show_error("更新失败", f"无法启动更新助手:{msg}")
            # 直接返回
            return

        # 启动更新助手，传入任务文件路径
        ok, msg = launch_update_assistant(task_path)
        # 检查启动是否成功
        if not ok:
            # 启动失败：弹出错误提示框
            self.view.show_error("更新失败", msg)
            # 直接返回
            return

        # 更新助手已成功启动
        # 标记自更新流程进行中（关窗时跳过确认）
        # 主程序退出，文件锁完全释放
        # 更新助手安装阶段会按任务中的 old_pid 精准等待本进程退出
        _logger.info(f"🔄 已启动更新助手(目标版本{remote_version}),主程序退出")
        # 设置自更新进行中标记
        self._self_update_in_progress = True
        # 强制关闭主窗口（不弹确认框）
        self.view.force_close()
        # 退出Qt应用，结束主程序
        QApplication.quit()

    def _ensure_transfer_controller(self) -> "FileTransferController":
        """懒创建文件传输Tab控制器（仅创建一次，随主窗口存活）。

        如果文件传输控制器尚未创建，则创建一个新实例并保存。
        懒加载的目的：
        - 文件传输功能与自更新业务完全隔离
        - 用户不使用文件传输功能时不创建相关对象，节省资源
        - 避免顶层循环导入问题

        Args:
            无参数。

        Returns:
            FileTransferController: 文件传输Tab控制器实例（已创建或新创建）。

        Raises:
            无。
        """
        # 获取当前文件传输控制器实例
        controller = self.transfer_controller
        # 检查是否尚未创建
        if controller is None:
            # 在函数内懒导入，避免顶层循环导入
            from runtime.controller.file_transfer_controller import (
                FileTransferController,
            )
            # 创建文件传输控制器实例
            # 传入全局配置和主窗口视图引用
            controller = FileTransferController(
                config=self.model.get_global_config(),
                main_view=self.view,
            )
            # 保存控制器实例到实例属性
            self.transfer_controller = controller
        # 返回控制器实例
        return controller

    @Slot()
    def _on_fetch_assets(self):
        """处理"获取资产"按钮点击事件。

        用户点击"获取资产"按钮时调用：
        - 首次点击：用当前全局配置懒创建控制器（构造时只回填只读配置，不拉列表）
        - 再次点击：先用最新系统配置刷新只读展示，再拉取资产

        拉取/下载/上传任务运行期间按钮由View层置灰，此处不重复忙碌判断。

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 确保文件传输控制器已创建（懒加载）
        controller = self._ensure_transfer_controller()
        # 先用最新的系统配置刷新文件传输控制器的配置和只读展示
        controller.refresh_config(self.model.get_global_config())
        # 触发拉取Release资产列表
        controller.fetch_assets()

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def _on_close_requested(self):
        """处理主窗口关闭请求。

        执行流程：
        1. 如果正在自更新流程中，直接关闭（不弹确认）
        2. 检查是否有后台任务在运行（单账号签到、批量签到、文件传输）
        3. 有任务运行时弹出二次确认，用户确认后协作式中断各线程
        4. 无任务或用户确认后，强制关闭窗口

        Args:
            无参数。

        Returns:
            None

        Raises:
            无。
        """
        # 检查是否正在自更新流程中
        # 已移交给更新助手：直接关闭，不弹确认（更新助手会等待本进程退出）
        if self._self_update_in_progress:
            # 记录日志：更新助手接管中，跳过关闭确认
            _logger.info("🔄 更新助手接管中,跳过关闭确认")
            # 强制关闭主窗口
            self.view.force_close()
            # 直接返回
            return
        # 检查单账号签到任务是否在运行
        running_single = bool(self.sign_worker and self.sign_worker.isRunning())
        # 检查批量签到任务是否在运行
        running_batch = bool(self.batch_worker and self.batch_worker.isRunning())
        # 检查文件传输任务是否在运行
        running_transfer = bool(
            self.transfer_controller is not None and self.transfer_controller.is_busy()
        )
        # 检查是否有任何任务在运行
        if running_single or running_batch or running_transfer:
            # 有任务在运行：构造正在运行的任务名称列表
            task_parts = []
            # 批量签到优先显示（批量包含单账号的概念）
            if running_batch:
                task_parts.append("批量签到")
            elif running_single:
                task_parts.append("签到")
            # 文件传输任务
            if running_transfer:
                task_parts.append("文件传输")
            # 拼接任务名称，用"/"分隔
            task_name = "/".join(task_parts)
            # 弹出二次确认对话框（危险操作，红色确认按钮）
            confirmed = self.view.show_question(
                "确认退出",  # 对话框标题
                f"{task_name}任务正在后台运行,关闭窗口将中断任务。\n确认退出吗?",  # 提示内容
                ok_text="确认退出",  # 确认按钮文字
                cancel_text="继续等待",  # 取消按钮文字
                danger=True,  # 危险操作标记
            )
            # 检查用户是否确认退出
            if not confirmed:
                # 用户选择继续等待：记录日志
                _logger.info(f"ℹ️ {task_name}任务运行中,用户选择继续等待,不退出")
                # 直接返回，不关闭窗口
                return
            # 用户确认退出：记录警告日志
            _logger.warning(f"🛑 用户在{task_name}任务运行中强制退出,中断后台任务")
            # 先协作式中断各线程：
            # 防风控延时的分段睡眠会立即响应，关闭会话后正常退出
            # 若线程正阻塞在HTTP请求中（有自身超时），给5秒优雅期，仍不退出才强杀

            # 中断单账号签到线程（如果在运行）
            if running_single and self.sign_worker is not None:
                # 请求协作式中断（线程会在合适的检查点响应）
                self.sign_worker.requestInterruption()
                # 等待线程退出，最多等5秒
                if not self.sign_worker.wait(5000):
                    # 5秒内未退出：记录警告日志
                    _logger.warning("🛑 单账号签到线程5秒内未退出,terminate强制结束")
                    # 强制终止线程（最后手段，可能导致资源泄漏）
                    self.sign_worker.terminate()
                    # 等待线程完全结束
                    self.sign_worker.wait()
            # 中断批量签到线程（如果在运行）
            if running_batch and self.batch_worker is not None:
                # 请求协作式中断
                self.batch_worker.requestInterruption()
                # 等待线程退出，最多等5秒
                if not self.batch_worker.wait(5000):
                    # 5秒内未退出：记录警告日志
                    _logger.warning("🛑 批量签到线程5秒内未退出,terminate强制结束")
                    # 强制终止线程
                    self.batch_worker.terminate()
                    # 等待线程完全结束
                    self.batch_worker.wait()
            # 中断文件传输器的所有线程（如果在运行）
            # 文件传输器：协作取消Release列表/下载/上传线程（有界等待，超时强杀）
            if running_transfer and self.transfer_controller is not None:
                self.transfer_controller.stop_workers()
        # 记录日志：用户请求关闭主窗口
        _logger.info("🪟 用户请求关闭主窗口")
        # 强制关闭主窗口
        self.view.force_close()
