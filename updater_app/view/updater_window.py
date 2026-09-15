# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: updater_window.py
# 归属: updater_app/view —— 更新助手视图层（View Layer）
# ------------------------------------------------------------------------------
# 文件用途:
#   更新助手 View 层（纯视图）——更新服务设置表单 + 下载/安装进度。
#   负责更新进度、版本信息、下载状态等 UI 渲染与用户交互。
# ------------------------------------------------------------------------------
# 架构定位:
#   更新助手 MVC 架构中的 View 层（视图层）。
#   纯界面渲染，通过信号向 Controller 发射用户操作事件，
#   不直接操作数据，不 import 任何 service/infrastructure 业务模块。
#
#   关联组件:
#     - 上游: UpdateAssistantController（Controller，持有 View 引用并调用接口）
#     - 下游: 无（纯 UI 展示）
# ------------------------------------------------------------------------------
# 布局（自上而下）:
#   1. 更新服务设置分组（聚合主程序四份 JSON，保存后与主程序共用同一配置）:
#        仓库所有者/仓库项目名              -> github.json
#        API 地址/请求超时/访问令牌          -> github.json
#        更新软件包模板/启动自动检查开关     -> version.json
#        更新下载位置（带目录选择）          -> user_info.json
#        自动检测系统代理                    -> proxy.json
#      + [保存配置] [重新检测] 按钮
#   2. 版本信息行（本地 -> 线上 + 状态）
#   3. 更新说明
#   4. 进度条
#   5. 运行日志
#   6. [取消更新] 按钮（安装阶段禁用，窗口关闭被拦截）
# ------------------------------------------------------------------------------
# 严格 MVC 边界:
#   - 只渲染控件并向外发信号
#   - 不读写 json、不创建线程、不 import 业务模块
#   - 目录选择用原生 QFileDialog（与主窗口"路径配置"一致），选完仅回填文本框
# ------------------------------------------------------------------------------
# 线程模型:
#   UI 操作必须在 Qt 主线程（GUI 线程）中执行，
#   所有 UI 控件的创建和修改都必须在主线程完成。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: 无
#   - 第三方: PySide6.QtCore（Signal）、PySide6.QtWidgets（多个控件）
#   - 项目内: 无
# ==============================================================================

# 导入 Qt 核心模块的 Signal（信号类）
from PySide6.QtCore import Signal
# 导入 PySide6 各类 UI 控件
from PySide6.QtWidgets import (
    QCheckBox,      # 复选框控件，用于开关选项
    QFileDialog,    # 文件/目录选择对话框
    QFormLayout,    # 表单布局，用于标签+输入框的两列布局
    QGroupBox,      # 分组框控件，用于将相关控件组织在一起
    QHBoxLayout,    # 水平布局管理器，用于横向排列控件
    QLabel,         # 标签控件，用于显示静态文本
    QLineEdit,      # 单行输入框控件，用于文本输入
    QMainWindow,    # 主窗口基类，提供主窗口框架
    QMessageBox,    # 消息框控件，用于弹出提示
    QProgressBar,   # 进度条控件，用于显示任务进度
    QPushButton,    # 按钮控件，用于用户点击操作
    QTextEdit,      # 多行文本编辑控件，用于日志显示
    QVBoxLayout,    # 垂直布局管理器，用于纵向排列控件
    QWidget,        # 通用窗口控件基类，用于容器控件
)


# ------------------------------------------------------------------------------
# 暗色主题样式表：统一窗口背景/控件配色
# ------------------------------------------------------------------------------
_DARK_STYLE = """
QWidget { background-color:#252526; color:#e0e0e0; font-size:13px; }
QGroupBox { border:1px solid #333; border-radius:6px; margin-top:12px; padding:10px; font-weight:bold; }
QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; }
QLineEdit { background-color:#1e1e1e; border:1px solid #3a3a3a; border-radius:3px; padding:5px 6px; color:#e0e0e0; }
QLineEdit:disabled { background-color:#2a2a2a; color:#888; }
QTextEdit { background-color:#1e1e1e; border:1px solid #333; border-radius:4px; padding:6px; }
QPushButton { background-color:#3a3a3a; color:white; border-radius:4px; padding:7px 16px; }
QPushButton:hover { background-color:#4a4a4a; }
QPushButton:disabled { background-color:#2d2d2d; color:#888; }
QPushButton#primary { background-color:#2f6fbd; font-weight:bold; }
QPushButton#primary:hover { background-color:#3f7fcd; }
QProgressBar { border:1px solid #444; border-radius:4px; text-align:center; background-color:#2b2b2b; color:#fff; height:18px; }
QProgressBar::chunk { background-color:#28a745; border-radius:3px; }
"""


# ==============================================================================
# 类: UpdaterWindow
# ==============================================================================
# =========** [UpdaterWindow][View] =========
# 关联组件:
#   - [UpdateAssistantController][Controller]: Controller 持有本窗口引用，
#     连接信号并调用公开接口更新 UI
# ==============================================================================
class UpdaterWindow(QMainWindow):
    """
    更新助手主窗口类，负责更新服务设置表单与下载/安装进度的 UI 渲染。

    详细说明:
        纯视图层，只渲染控件并通过信号向 Controller 发射用户操作事件。
        不直接操作数据，不 import 任何业务模块。

    Signals:
        sig_save_settings (dict): 保存更新服务设置（仓库/连接/超时/目录/开关）
        sig_recheck (): 重新检测最新 Release
        sig_cancel (): 用户请求取消/关闭（下载阶段）
    """
    # 保存设置信号，参数为表单数据字典
    sig_save_settings = Signal(dict)
    # 重新检测信号，无参数
    sig_recheck = Signal()
    # 取消信号，无参数
    sig_cancel = Signal()

    def __init__(self, local_version: str = "", remote_version: str = ""):
        """
        初始化更新助手主窗口。

        参数:
            local_version (str): 本地版本号；默认为空串
            remote_version (str): 远程版本号（检测后回填）；默认为空串

        返回值:
            None

        异常:
            无
        """
        # 调用 QMainWindow 父类构造函数
        super().__init__()
        # 是否处于安装阶段标志（控制取消/关闭拦截）
        self._installing = False
        # 设置窗口标题
        self.setWindowTitle("更新助手")
        # 设置窗口初始大小
        self.resize(760, 800)
        # 设置窗口最小大小
        self.setMinimumSize(680, 700)
        # 应用暗色主题样式表
        self.setStyleSheet(_DARK_STYLE)

        # 创建中心部件
        central = QWidget()
        # 设置中心部件
        self.setCentralWidget(central)
        # 创建根垂直布局
        root = QVBoxLayout(central)
        # 设置外边距
        root.setContentsMargins(14, 12, 14, 12)
        # 设置子组件间距
        root.setSpacing(8)

        # =====** 更新服务设置分组组件 =====**
        # 构建设置分组并添加到根布局
        self._build_settings_group(root)

        # =====** 版本信息行组件 =====**
        # 创建版本信息水平布局
        ver_row = QHBoxLayout()
        # 创建版本号标签（本地版本 -> 远程版本）
        self.lab_versions = QLabel(
            f"当前版本:<b>{local_version or '--'}</b>"
            f"&nbsp;&nbsp;➜&nbsp;&nbsp;新版本:<b>{remote_version or '--'}</b>"
        )
        # 创建状态标签
        self.lab_status = QLabel("正在准备……")
        # 设置状态文字颜色
        self.lab_status.setStyleSheet("color:#9cdcfe;")
        # 添加版本标签到版本行
        ver_row.addWidget(self.lab_versions)
        # 添加弹性空间
        ver_row.addStretch(1)
        # 添加状态标签到版本行
        ver_row.addWidget(self.lab_status)
        # 添加版本行到根布局
        root.addLayout(ver_row)

        # =====** 更新说明组件 =====**
        # 创建更新说明文本框
        self.te_note = QTextEdit()
        # 设置为只读
        self.te_note.setReadOnly(True)
        # 设置占位提示文本
        self.te_note.setPlaceholderText("正在获取更新说明……")
        # 设置固定高度
        self.te_note.setFixedHeight(110)
        # 添加更新说明到根布局
        root.addWidget(self.te_note)

        # =====** 进度条组件 =====**
        # 创建进度条
        self.progress = QProgressBar()
        # 设置初始进度为 0
        self.progress.setValue(0)
        # 添加进度条到根布局
        root.addWidget(self.progress)

        # =====** 运行日志组件 =====**
        # 创建日志文本框
        self.te_log = QTextEdit()
        # 设置为只读
        self.te_log.setReadOnly(True)
        # 设置占位提示文本
        self.te_log.setPlaceholderText("等待开始……")
        # 设置日志框样式（等宽字体）
        self.te_log.setStyleSheet(
            "QTextEdit { font-family:Consolas,monospace; font-size:12px; }"
        )
        # 添加日志框到根布局（占满剩余空间）
        root.addWidget(self.te_log, stretch=1)

        # =====** 底部按钮组件 =====**
        # 创建底部水平布局
        bottom_row = QHBoxLayout()
        # 添加弹性空间（按钮靠右）
        bottom_row.addStretch(1)
        # 创建取消按钮
        self.btn_cancel = QPushButton("取消更新")
        # 设置按钮最小宽度
        self.btn_cancel.setMinimumWidth(120)
        # 点击取消按钮时发射取消信号
        self.btn_cancel.clicked.connect(self.sig_cancel.emit)
        # 添加取消按钮到底部布局
        bottom_row.addWidget(self.btn_cancel)
        # 添加底部布局到根布局
        root.addLayout(bottom_row)

    # ------------------------------------------------------------------
    # =====** 更新服务设置分组组件 =====**
    # ------------------------------------------------------------------
    def _build_settings_group(self, root):
        """
        构建设置分组：仓库/连接/超时/包名/令牌/下载目录/开关。

        参数:
            root (QVBoxLayout): 根布局

        返回值:
            None

        异常:
            无
        """
        # 创建分组框
        group = QGroupBox("更新服务设置(与主程序共用同一份配置)")
        # 创建表单布局
        form = QFormLayout(group)
        # 设置水平间距
        form.setHorizontalSpacing(12)
        # 设置垂直间距
        form.setVerticalSpacing(8)

        # 仓库所有者输入框
        self.le_github_owner = QLineEdit()
        # 设置占位提示
        self.le_github_owner.setPlaceholderText("仓库所有者名称,例如:itwuge")
        # 添加表单项
        form.addRow(QLabel("仓库所有者:"), self.le_github_owner)

        # 仓库项目名输入框
        self.le_github_name = QLineEdit()
        # 设置占位提示
        self.le_github_name.setPlaceholderText("仓库项目名,例如:wuge_tools")
        # 添加表单项
        form.addRow(QLabel("仓库项目名:"), self.le_github_name)

        # GitHub API 地址输入框
        self.le_github_api = QLineEdit()
        # 设置占位提示
        self.le_github_api.setPlaceholderText("GitHub API地址,默认https://api.github.com,可填兼容网关/镜像")
        # 添加表单项
        form.addRow(QLabel("更新连接(API):"), self.le_github_api)

        # 请求超时输入框
        self.le_github_timeout = QLineEdit()
        # 设置占位提示
        self.le_github_timeout.setPlaceholderText("请求超时秒数,默认15(1~600)")
        # 添加表单项
        form.addRow(QLabel("请求超时(秒):"), self.le_github_timeout)

        # 更新包文件名模板输入框
        self.le_update_pkg = QLineEdit()
        # 设置占位提示
        self.le_update_pkg.setPlaceholderText("更新包模板需含{platform}/{archive_ext},例如:wuge_tools-{platform}.{archive_ext}")
        # 添加表单项
        form.addRow(QLabel("更新软件包:"), self.le_update_pkg)

        # GitHub 访问令牌输入框（密码模式）
        self.le_github_token = QLineEdit()
        # 设置占位提示
        self.le_github_token.setPlaceholderText("GitHub Token,公开仓库留空")
        # 设置为密码模式（隐藏输入）
        self.le_github_token.setEchoMode(QLineEdit.EchoMode.Password)
        # 添加表单项
        form.addRow(QLabel("访问令牌:"), self.le_github_token)

        # 更新下载位置：输入框 + 选择按钮
        self.le_download_dir = QLineEdit()
        # 设置占位提示
        self.le_download_dir.setPlaceholderText("更新包下载保存目录")
        # 创建目录选择按钮
        self.btn_browse_dir = QPushButton("选择")
        # 设置按钮最小宽度
        self.btn_browse_dir.setMinimumWidth(64)
        # 点击按钮时打开目录选择对话框
        self.btn_browse_dir.clicked.connect(self._on_browse_dir)
        # 创建目录行水平布局
        dir_row = QHBoxLayout()
        # 无外边距
        dir_row.setContentsMargins(0, 0, 0, 0)
        # 添加输入框
        dir_row.addWidget(self.le_download_dir)
        # 添加选择按钮
        dir_row.addWidget(self.btn_browse_dir)
        # 添加表单项
        form.addRow(QLabel("更新下载位置:"), dir_row)

        # 启动自动检查更新开关
        self.cb_auto_check_update = QCheckBox("启动主程序时自动检查新版本(取消后仅手动点\"检查更新\")")
        # 添加表单项
        form.addRow(self.cb_auto_check_update)

        # 自动检测系统代理开关
        self.cb_auto_detect_proxy = QCheckBox("自动检测系统代理(关闭后按主程序代理配置生效)")
        # 添加表单项
        form.addRow(self.cb_auto_detect_proxy)

        # 操作按钮行：重新检测 + 保存配置
        btn_row = QHBoxLayout()
        # 添加弹性空间（按钮靠右）
        btn_row.addStretch(1)
        # 创建重新检测按钮
        self.btn_recheck = QPushButton("重新检测")
        # 创建保存配置按钮
        self.btn_save = QPushButton("保存配置")
        # 标记为主按钮（蓝色样式）
        self.btn_save.setObjectName("primary")
        # 点击保存按钮时发射保存信号
        self.btn_save.clicked.connect(self._emit_save_settings)
        # 点击重新检测按钮时发射重新检测信号
        self.btn_recheck.clicked.connect(self.sig_recheck.emit)
        # 添加重新检测按钮
        btn_row.addWidget(self.btn_recheck)
        # 添加保存按钮
        btn_row.addWidget(self.btn_save)
        # 添加按钮行到表单
        form.addRow(btn_row)
        # 添加分组到根布局
        root.addWidget(group)

    def _on_browse_dir(self):
        """
        原生目录选择对话框；选完仅回填文本框，实际保存由"保存配置"统一落盘。

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 获取当前目录文本
        cur = self.le_download_dir.text().strip()
        # 打开目录选择对话框
        chosen = QFileDialog.getExistingDirectory(
            self, "选择更新包下载目录", cur or ""
        )
        # 用户选择了目录
        if chosen:
            # 回填到输入框
            self.le_download_dir.setText(chosen)

    # ------------------------------------------------------------------
    # 对外信号发射
    # ------------------------------------------------------------------
    def _emit_save_settings(self):
        """
        收集表单数据并发射保存信号。

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 发射保存设置信号，携带表单数据字典
        self.sig_save_settings.emit({
            # 仓库所有者
            "github_owner": self.le_github_owner.text().strip(),
            # 仓库项目名
            "github_name": self.le_github_name.text().strip(),
            # API 地址
            "github_api_url": self.le_github_api.text().strip(),
            # 请求超时
            "github_timeout": self.le_github_timeout.text().strip(),
            # 更新包模板
            "update_pkg": self.le_update_pkg.text().strip(),
            # 访问令牌
            "github_token": self.le_github_token.text(),
            # 下载目录
            "download_dir": self.le_download_dir.text().strip(),
            # 启动自检开关
            "auto_check_update": self.cb_auto_check_update.isChecked(),
            # 自动检测代理开关
            "auto_detect_proxy": self.cb_auto_detect_proxy.isChecked(),
        })

    # ------------------------------------------------------------------
    # Controller 调用的回填/状态接口
    # ------------------------------------------------------------------
    def fill_settings_form(self, form: dict):
        """
        回填更新服务设置全部字段。

        参数:
            form (dict): 表单数据字典

        返回值:
            None

        异常:
            无
        """
        # 回填仓库所有者
        self.le_github_owner.setText(str(form.get("github_owner", "")))
        # 回填仓库项目名
        self.le_github_name.setText(str(form.get("github_name", "")))
        # 回填 API 地址
        self.le_github_api.setText(str(form.get("github_api_url", "")))
        # 回填请求超时
        self.le_github_timeout.setText(str(form.get("github_timeout", "")))
        # 回填更新包模板
        self.le_update_pkg.setText(str(form.get("update_pkg", "")))
        # 回填访问令牌
        self.le_github_token.setText(str(form.get("github_token", "")))
        # 回填下载目录
        self.le_download_dir.setText(str(form.get("download_dir", "")))
        # 回填启动自检开关
        self.cb_auto_check_update.setChecked(bool(form.get("auto_check_update", True)))
        # 回填自动代理开关
        self.cb_auto_detect_proxy.setChecked(bool(form.get("auto_detect_proxy", True)))

    def fill_release(self, meta: dict):
        """
        回填 Release 版本信息与更新说明。

        参数:
            meta (dict): Release 元数据字典，包含 tag（版本号）、
                publish_time（发布时间）、release_note（更新说明）

        返回值:
            None

        异常:
            无
        """
        # 获取版本标签
        tag = meta.get("tag", "")
        # 更新版本标签文本（新版本号 + 发布时间）
        self.lab_versions.setText(
            f"新版本:<b>{tag or '--'}</b>"
            f"&nbsp;&nbsp;发布时间:{self._fmt_time(meta.get('publish_time', ''))}"
        )
        # 回填更新说明
        self.te_note.setPlainText(meta.get("release_note", "") or "暂无更新说明")

    def append_log(self, text: str):
        """
        追加日志到日志框。

        参数:
            text (str): 日志文本

        返回值:
            None

        异常:
            无
        """
        # 追加一行日志
        self.te_log.append(text)

    def set_progress(self, val: int):
        """
        设置进度条百分比。

        参数:
            val (int): 进度值（0-100）

        返回值:
            None

        异常:
            无
        """
        # 进度条范围不是 0-100 则重置
        if self.progress.maximum() != 100:
            self.progress.setRange(0, 100)
        # 设置进度（钳制在 0-100 之间）
        self.progress.setValue(max(0, min(100, int(val))))

    def set_busy_indeterminate(self):
        """
        安装/等待阶段无法量化，进度条滚动展示。

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 范围 0-0 表示不确定进度（滚动条）
        self.progress.setRange(0, 0)

    def set_status(self, text: str):
        """
        设置状态栏文本。

        参数:
            text (str): 状态文本

        返回值:
            None

        异常:
            无
        """
        # 更新状态标签
        self.lab_status.setText(text)

    def set_controls_enabled(self, enabled: bool):
        """
        任务运行中禁用设置项与相关按钮，避免运行中改配置。

        参数:
            enabled (bool): 是否启用

        返回值:
            None

        异常:
            无
        """
        # 遍历所有设置控件
        for w in (
            self.le_github_owner, self.le_github_name, self.le_github_api,
            self.le_github_timeout, self.le_update_pkg, self.le_github_token,
            self.le_download_dir, self.btn_browse_dir,
            self.cb_auto_check_update, self.cb_auto_detect_proxy,
            self.btn_save, self.btn_recheck,
        ):
            # 设置启用状态
            w.setEnabled(enabled)

    def set_installing(self, installing: bool):
        """
        安装阶段：禁止取消/关闭（中断文件替换会留下半成品）。

        参数:
            installing (bool): 是否处于安装阶段

        返回值:
            None

        异常:
            无
        """
        # 更新安装状态标志
        self._installing = installing
        # 安装中禁用设置控件
        self.set_controls_enabled(not installing)
        # 安装中禁用取消按钮
        self.btn_cancel.setEnabled(not installing)
        # 进入安装阶段
        if installing:
            # 修改按钮文字
            self.btn_cancel.setText("正在安装,请勿关闭……")
            # 进度条变为不确定（滚动）
            self.progress.setRange(0, 0)
        # 退出安装阶段
        else:
            # 恢复按钮文字
            self.btn_cancel.setText("取消更新")
            # 恢复进度条范围
            self.progress.setRange(0, 100)

    def show_info(self, title: str, text: str):
        """
        信息提示框（中文按钮）。

        参数:
            title (str): 标题
            text (str): 正文

        返回值:
            None

        异常:
            无
        """
        # 创建信息消息框
        box = QMessageBox(self)
        # 设置图标为信息图标
        box.setIcon(QMessageBox.Icon.Information)
        # 设置标题
        box.setWindowTitle(title)
        # 设置正文
        box.setText(text)
        # 添加中文「确定」按钮
        ok_btn = box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
        # 设为默认按钮
        box.setDefaultButton(ok_btn)
        # 弹出模态对话框
        box.exec()

    def show_error(self, title: str, text: str):
        """
        错误提示框（中文按钮）。

        参数:
            title (str): 标题
            text (str): 正文

        返回值:
            None

        异常:
            无
        """
        # 创建错误消息框
        box = QMessageBox(self)
        # 设置图标为错误图标
        box.setIcon(QMessageBox.Icon.Critical)
        # 设置标题
        box.setWindowTitle(title)
        # 设置正文
        box.setText(text)
        # 添加中文「确定」按钮
        ok_btn = box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
        # 设为默认按钮
        box.setDefaultButton(ok_btn)
        # 弹出模态对话框
        box.exec()

    def confirm_cancel(self) -> bool:
        """
        下载阶段关闭/取消前二次确认（中文按钮）。

        返回值:
            bool: 用户是否确认取消；True 表示确认，False 表示取消

        异常:
            无
        """
        # 创建询问消息框
        box = QMessageBox(self)
        # 设置图标为询问图标
        box.setIcon(QMessageBox.Icon.Question)
        # 设置标题
        box.setWindowTitle("取消更新")
        # 设置正文
        box.setText("确定取消本次更新吗?\n将关闭更新助手。")
        # 添加中文「确认取消」按钮（接受角色）
        yes_btn = box.addButton("确认取消", QMessageBox.ButtonRole.AcceptRole)
        # 添加中文「再想想」按钮（拒绝角色）
        no_btn = box.addButton("再想想", QMessageBox.ButtonRole.RejectRole)
        # 默认聚焦「再想想」按钮（防止误点）
        box.setDefaultButton(no_btn)
        # 弹出模态对话框
        box.exec()
        # 用户点击的是「确认取消」按钮则返回 True
        return box.clickedButton() is yes_btn

    @staticmethod
    def _fmt_time(raw: str) -> str:
        """
        格式化 ISO 时间字符串为可读格式。

        参数:
            raw (str): ISO 时间字符串（如 2026-09-15T08:00:00Z）

        返回值:
            str: 格式化后的时间（如 2026-09-15 08:00:00）；空输入返回"未知"

        异常:
            无；格式化失败返回原值
        """
        # 空字符串返回"未知"
        if not raw:
            return "未知"
        try:
            # 去除 Z 和毫秒，把 T 换成空格
            return raw.rstrip("Z").split(".")[0].replace("T", " ")
        except Exception:
            # 格式化失败返回原值
            return raw

    # ------------------------------------------------------------------
    # 关闭拦截：安装阶段直接忽略；其余转为取消信号由 Controller 确认
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        """
        窗口关闭事件：安装阶段禁止关闭；其余转为取消信号。

        参数:
            event (QCloseEvent): 关闭事件

        返回值:
            None

        异常:
            无
        """
        # 安装阶段禁止关闭
        if self._installing:
            # 忽略关闭事件
            event.ignore()
            return
        # 忽略默认关闭行为
        event.ignore()
        # 发射取消信号，由 Controller 处理
        self.sig_cancel.emit()
