# ==============================================================================
# 【主窗口 View 层】main_window.py
# ==============================================================================
# 文件用途:
#   签到工具平台的主窗口视图类 MainWindow,是整个应用程序的核心UI界面。
#   采用严格的 MVC 架构设计,本文件作为纯 View 层,只负责窗口渲染与界面交互,
#   不包含任何业务逻辑。所有用户操作通过 Qt Signal 信号向外发射,交由
#   Controller 层处理;所有界面数据回填由 Controller 调用公开接口完成。
# ------------------------------------------------------------------------------
# 架构定位 (MVC 模式):
#   - 所属层级: View 层(视图层) - 主窗口视图
#   - 对应 Controller: MainController(主控制器,负责连接 View 与 Model,处理业务逻辑)
#   - 对应 Model:      MainModel/AccountModel/ConfigModel 等(数据模型层)
#   - 上游调用方:      Controller 层(通过调用 View 的公开方法更新界面)
#   - 下游依赖:        仅依赖 PySide6 与同包内的 common_dialog 模块
#   - 职责边界:        仅负责UI渲染、信号发射、数据回填,不处理任何业务逻辑
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 签到工具 Tab: 账号管理(添加/删除/列表)、单账号签到、批量签到、签到日志
#   2. 文件传输 Tab: GitHub Release 资产下载、TUS 协议文件上传、传输进度与日志
#   3. 配置管理 Tab: 路径配置、邮件配置、GitHub配置、文件IO配置、网络代理配置
#   4. 状态栏:       显示当前状态信息与版本号
#   5. 关闭控制:     拦截关闭事件,交由 Controller 判断是否有后台任务
# ------------------------------------------------------------------------------
# MVC 定位:
#   - Model(模型层):      数据模型,负责账号、配置、签到记录等数据的管理与持久化
#   - View(视图层):       本文件,负责所有UI界面的渲染、用户交互信号发射、数据回填显示
#   - Controller(控制层): 业务控制器,负责连接 View 与 Model,处理业务逻辑与线程调度
# ------------------------------------------------------------------------------
# UI 设计规范:
#   - 整体风格: 深色主题(暗色背景 #1e1e1e,浅色文字 #e0e0e0)
#   - 卡片分组: QGroupBox 圆角 6px 描边,标题置顶,内边距 10px
#   - 按钮样式:
#     - 主操作按钮(蓝): 保存配置/下载选中资产 (#2f6fbd)
#     - 正向操作按钮(绿): 检查更新/上传 (#28a745)
#     - 提醒操作按钮(橙): 获取资产 (#d38e0a)
#   - 布局比例: 签到工具 Tab 左3右7,文件传输 Tab 左4右6
#   - 统一规范: 所有 QGroupBox 使用 CARD_GROUP_QSS 样式常量
# ------------------------------------------------------------------------------
# 禁止事项 (严格MVC职责边界):
#   - 禁止 import 业务模块(service/comm_tools/infrastructure)
#   - 禁止 import QThread/Worker,不管理任何后台线程
#   - 禁止直接读写 json 配置文件
#   - 禁止直接发起网络请求
#   - 禁止在 View 层处理业务逻辑
# ------------------------------------------------------------------------------
# 依赖:
#   - 标准库: os、re
#   - 第三方: PySide6.QtCore、PySide6.QtWidgets
#   - 项目内: runtime.view.common_dialog
# ==============================================================================

# =====** 标准库导入 =====**
import os  # 操作系统接口模块,用于路径拼接、文件分隔符获取等文件系统操作
import re  # 正则表达式模块,用于从签到文案中提取流量数值

# =====** PySide6 核心模块导入 =====**
from PySide6.QtCore import Qt, Signal  # Qt: Qt 命名空间枚举常量; Signal: Qt 信号类,用于自定义信号

# =====** PySide6 控件类导入 =====**
from PySide6.QtWidgets import QAbstractItemView  # 抽象项视图基类,用于设置表格/列表的编辑触发模式等
from PySide6.QtWidgets import QApplication  # 应用程序类,用于退出应用程序
from PySide6.QtWidgets import QCheckBox  # 复选框控件,用于开关选项
from PySide6.QtWidgets import QFileDialog  # 文件对话框,用于选择文件/目录
from PySide6.QtWidgets import QFormLayout  # 表单布局,用于标签+输入框的两列布局
from PySide6.QtWidgets import QFrame  # 框架控件,用于设置滚动区域边框样式
from PySide6.QtWidgets import QGroupBox  # 分组框控件,用于将相关控件组织在一起
from PySide6.QtWidgets import QHBoxLayout  # 水平布局管理器,用于横向排列控件
from PySide6.QtWidgets import QLabel  # 标签控件,用于显示静态文本
from PySide6.QtWidgets import QLineEdit  # 单行输入框控件,用于文本输入
from PySide6.QtWidgets import QListWidget  # 列表控件,用于显示账号列表等
from PySide6.QtWidgets import QMainWindow  # 主窗口基类,提供菜单栏、工具栏、状态栏等主窗口框架
from PySide6.QtWidgets import QProgressBar  # 进度条控件,用于显示任务进度
from PySide6.QtWidgets import QPushButton  # 按钮控件,用于用户点击操作
from PySide6.QtWidgets import QScrollArea  # 滚动区域控件,用于内容超出时提供滚动
from PySide6.QtWidgets import QTableWidget  # 表格控件,用于显示资产列表等表格数据
from PySide6.QtWidgets import QTableWidgetItem  # 表格项控件,用于表格单元格数据
from PySide6.QtWidgets import QTabWidget  # 选项卡控件,用于多Tab页面切换
from PySide6.QtWidgets import QTextEdit  # 多行文本编辑控件,用于日志显示、详情展示等
from PySide6.QtWidgets import QVBoxLayout  # 垂直布局管理器,用于纵向排列控件
from PySide6.QtWidgets import QWidget  # 通用窗口控件基类,用于容器控件

# =====** 同包内模块导入 =====**
from runtime.view import common_dialog  # 导入同包内的通用对话框模块,用于弹出消息提示框

# =========** MainWindow View =========
# 对应 Controller: MainController(主控制器)
# 对应 Model:      AccountModel(账号模型)、ConfigModel(配置模型)、TransferModel(传输模型)
# 说明: 本类为应用程序主窗口视图,包含三大功能Tab(签到工具、文件传输、配置管理),
#       是用户与系统交互的主要界面。所有用户操作通过信号发射给 MainController 处理,
#       所有界面数据由 MainController 调用本类公开方法进行回填。
# ==============================================================================

# =====** 全局样式常量组件 =====**
# 卡片式页面视觉规范(以"文件传输"Tab为样板,各配置页统一复用,勿各自散写)
# 卡片分组框样式:圆角6px描边,标题置顶,内边距10px,深灰色文字加粗
CARD_GROUP_QSS = """
QGroupBox {
    color:#e0e0e0; font-weight:bold;        /* 文字颜色:浅灰色,字体加粗 */
    border:1px solid #333; border-radius:6px; margin-top:8px; padding:10px;  /* 边框:深灰色1px,圆角6px,顶部外边距8px,内边距10px */
}
"""
# 主操作按钮样式(蓝色):用于保存配置、下载选中资产等主要操作
PRIMARY_BTN_QSS = (
    "QPushButton{background-color:#2f6fbd;color:white;border-radius:4px;"  # 背景蓝色,白色文字,圆角4px
    "padding:6px 14px;font-weight:bold;}"                                  # 内边距上下6px左右14px,字体加粗
    "QPushButton:disabled{background-color:#6b84a8;color:#dbe2ec;}"        # 禁用状态:浅蓝灰色背景,浅蓝灰色文字
)
# 正向操作按钮样式(绿色):用于检查更新、上传等成功/正向操作
SUCCESS_BTN_QSS = (
    "QPushButton{background-color:#28a745;color:white;border-radius:4px;"  # 背景绿色,白色文字,圆角4px
    "padding:6px 14px;font-weight:bold;}"                                  # 内边距上下6px左右14px,字体加粗
    "QPushButton:disabled{background-color:#74b385;color:#e0f0e5;}"        # 禁用状态:浅绿灰色背景,浅绿灰色文字
)
# 提醒操作按钮样式(橙色):用于获取资产等提醒/注意操作
WARN_BTN_QSS = (
    "QPushButton{background-color:#d38e0a;color:white;border-radius:4px;"  # 背景橙色,白色文字,圆角4px
    "padding:6px 14px;font-weight:bold;}"                                  # 内边距上下6px左右14px,字体加粗
    "QPushButton:disabled{background-color:#c9a35e;color:#f0e7d2;}"        # 禁用状态:浅橙灰色背景,浅橙灰色文字
)


# =====** 工具函数组件 =====**
def _short_flow(text) -> str:
    """
    界面显示用工具函数:将签到文案精简为纯流量值字符串

    作用:将"获得了 493MB 流量."这类完整文案简化为"493MB",
    避免"今日签到获取:获得了…流量"的语义重复问题。
    若提取不到数值+单位,则去掉句末句点后原样返回;空串返回空串。

    :param text: 原始签到文案字符串,可能包含流量信息
    :type text: str 或其他可转为字符串的类型
    :return: 精简后的流量字符串,如"493MB";提取失败则返回去除末尾标点的原文
    :rtype: str
    """
    raw = str(text or "").strip()  # 将输入转为字符串并去除首尾空白字符,处理 None 等空值情况
    if not raw:                    # 如果处理后的字符串为空
        return ""                  # 直接返回空字符串
    # 使用正则表达式匹配流量数值和单位
    # 说明:不使用\b单词边界是因为Unicode模式下中文字符也算单词字符,
    # "MB流量"的B与"流"之间没有单词边界,会导致匹配失败
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)(?![A-Za-z])", raw, re.IGNORECASE)
    if not m:                      # 如果没有匹配到流量数值和单位
        return raw.rstrip("。.")   # 去掉句末的中文句号和英文句号后返回原文
    # 处理数值部分:如果以".0"结尾(整数浮点数),则去掉".0"使其更简洁
    num = m.group(1)[:-2] if m.group(1).endswith(".0") else m.group(1)
    # 拼接数值和大写单位后返回
    return f"{num}{m.group(2).upper()}"


# =====** 主窗口视图类组件 =====**
class MainWindow(QMainWindow):
    """
    主窗口纯视图类 - 签到工具平台的主界面

    本类继承自 QMainWindow,是应用程序的主窗口。采用严格MVC架构,
    只负责UI渲染与用户交互信号的发射,不包含任何业务逻辑。

    包含三大功能Tab:
    1. 签到工具: 账号管理、单账号/批量签到、签到日志
    2. 文件传输: GitHub Release 资产下载、TUS 协议文件上传
    3. 配置管理: 路径、邮件、GitHub、文件IO、网络代理等配置

    Signals (自定义信号,用户操作触发后向 Controller 发射):
        sig_save_path(dict):       配置Tab 保存路径配置(数据目录全路径+子路径相对路径)
        sig_save_mail(dict):       配置Tab 保存邮件配置
        sig_save_github(dict):     配置Tab 保存GitHub配置
        sig_save_fileio(dict):     配置Tab 保存下载配置(文件IO)
        sig_save_proxy(dict):      配置Tab 保存代理配置
        sig_detect_proxy():           配置Tab 请求检测系统代理设置
        sig_add_account(str, str):    添加账号(邮箱,密码),持久化供自动登录/批量签到
        sig_del_account(str):         删除账号(邮箱)
        sig_account_selected(str):    列表选中账号(邮箱)
        sig_sign_started(str, str):   单账号签到(账号,密码)
        sig_sign_batch():             批量签到(使用全部已保存密码的账号)
        sig_open_update():            打开软件更新窗口
        sig_close_requested():        用户请求关闭窗口(可能有后台任务运行中)
        sig_download_requested(list, str):  文件传输Tab:请求下载(勾选资产名列表, 保存目录)
        sig_upload_requested(str, str):     文件传输Tab:请求上传(TUS服务地址, 本地文件路径)
        sig_fetch_assets_requested():       文件传输Tab:用户点击"获取资产"按钮,手动拉取最新Release资产列表

    Attributes (类属性/实例属性说明):
        local_version (str):          本地软件版本号,显示在状态栏
        _allow_close (bool):          是否允许直接关闭窗口的标志位,有后台任务时为False
        tabWidget (QTabWidget):       顶部选项卡控件,包含三大功能Tab
        statusbar (QStatusBar):       状态栏控件,显示状态信息
        version_label (QLabel):       状态栏右侧的版本号标签
        以及各Tab内的大量UI控件属性...
    """

    # =====** 信号定义组件 =====**
    # 配置管理Tab - 保存路径配置信号,参数为包含数据目录全路径+子路径相对路径的字典
    sig_save_path = Signal(dict)
    # 配置管理Tab - 保存邮件配置信号,参数为包含SMTP各项邮件配置的字典
    sig_save_mail = Signal(dict)
    # 配置管理Tab - 保存GitHub配置信号,参数为包含GitHub仓库与更新包配置的字典
    sig_save_github = Signal(dict)
    # 配置管理Tab - 保存下载配置信号,参数为包含下载/上传分片与并发配置的字典
    sig_save_fileio = Signal(dict)
    # 配置管理Tab - 保存代理配置信号,参数为包含代理开关与代理地址端口的字典
    sig_save_proxy = Signal(dict)
    # 配置管理Tab - 检测系统代理信号,无参数,触发后由Controller读取系统代理设置
    sig_detect_proxy = Signal()
    # 签到工具Tab - 添加账号信号,参数为(邮箱, 密码)
    sig_add_account = Signal(str, str)
    # 签到工具Tab - 删除账号信号,参数为(邮箱)
    sig_del_account = Signal(str)
    # 签到工具Tab - 选中账号信号,参数为(邮箱)
    sig_account_selected = Signal(str)
    # 签到工具Tab - 开始单账号签到信号,参数为(账号, 密码)
    sig_sign_started = Signal(str, str)
    # 签到工具Tab - 开始批量签到信号,无参数(使用全部已保存账号)
    sig_sign_batch = Signal()
    # 配置管理Tab - 打开软件更新窗口信号,无参数
    sig_open_update = Signal()
    # 文件传输Tab - 请求下载信号,参数为(勾选的资产名列表, 保存目录路径)
    sig_download_requested = Signal(list, str)
    # 文件传输Tab - 请求上传信号,参数为(TUS服务地址, 本地文件路径)
    sig_upload_requested = Signal(str, str)
    # 文件传输Tab - 请求获取资产列表信号,无参数,手动拉取最新Release资产列表
    sig_fetch_assets_requested = Signal()
    # 窗口关闭请求信号,无参数,用户点击关闭按钮时发射,由Controller判断是否可以关闭
    sig_close_requested = Signal()

    def __init__(self, local_version: str = ""):
        """
        主窗口构造函数 - 初始化主窗口及所有UI控件

        :param local_version: 本地软件版本号字符串,用于显示在状态栏
        :type local_version: str
        """
        super().__init__()                  # 调用父类 QMainWindow 的构造函数
        self.local_version = local_version  # 保存本地版本号到实例属性,供状态栏显示使用
        self._allow_close = False           # 初始化允许关闭标志为False,关闭窗口需经Controller确认

        # =====** 主窗口基本设置 =====**
        self.setWindowTitle("个人小工具")  # 设置窗口标题
        self.resize(780, 740)               # 设置窗口初始大小:宽780px,高740px
        self.setMinimumSize(650, 560)       # 设置窗口最小尺寸:宽650px,高560px,防止窗口过小导致布局错乱

        # =====** 顶部Tab栏 =====**
        # 创建中心部件作为主窗口的核心容器(QMainWindow必须设置centralWidget)
        central_widget = QWidget()          # 创建中心窗口部件
        self.setCentralWidget(central_widget)  # 将中心部件设置为主窗口的中心控件
        main_layout = QVBoxLayout(central_widget)  # 创建垂直布局并应用到中心部件
        main_layout.setContentsMargins(8, 8, 8, 8)  # 设置布局四周内边距为8px

        # 创建选项卡控件,用于组织三大功能页面
        self.tabWidget = QTabWidget()       # 创建选项卡控件实例
        self.tabWidget.setTabPosition(QTabWidget.TabPosition.North)  # 设置Tab栏位置在顶部(北方)
        self.tabWidget.setStyleSheet("""    # 设置Tab栏样式
            QTabBar::tab {                  # Tab按钮样式
                padding: 8px 18px;          # Tab内边距:上下8px,左右18px
                min-width: 100px;           # Tab最小宽度100px
                font-size:13px;             # Tab文字字号13px
            }
        """)
        main_layout.addWidget(self.tabWidget)  # 将选项卡控件添加到主布局中

        # =====** 三大Tab页面构建 =====**
        # 按顺序构建三个Tab页面,注意:addTab的顺序决定了显示顺序
        self._build_tab3()           # ①签到工具Tab(第1位显示)
        self._build_tab_transfer()   # ②文件传输Tab(第2位显示,通用上传下载,与自更新业务隔离)
        self._build_tab_config()     # ③配置管理Tab(第3位显示,左系统配置:右路径配置)

        # =====** 状态栏 =====**
        self.statusbar = self.statusBar()       # 获取主窗口状态栏控件
        self.statusbar.showMessage("空闲")      # 设置状态栏初始消息为"空闲"
        self.version_label = QLabel(f"当前版本:{self.local_version}")  # 创建版本号标签
        self.version_label.setStyleSheet("padding-right:10px; color:#cccccc;")  # 设置版本标签样式:右内边距10px,浅灰色文字
        self.statusbar.addPermanentWidget(self.version_label)  # 将版本标签添加为状态栏永久控件(显示在右侧)

        # =====** 信号绑定 =====**
        self._bind_signals()  # 调用内部方法绑定所有UI控件的信号与槽函数

    def set_local_version(self, version: str):
        """
        更新底部状态栏版本号显示

        由 Controller 在检测到版本变化时调用,更新状态栏中的版本号显示。

        :param version: 新的版本号字符串
        :type version: str
        :return: 无返回值
        :rtype: None
        """
        self.local_version = version                           # 更新实例属性中的版本号
        self.version_label.setText(f"当前版本:{version}")      # 更新版本标签的显示文字

    # ======================================================================
    # 文件传输Tab 构建函数
    # 布局说明:左侧4份(配置只读展示) : 右侧6份(资产列表+操作)
    # 左侧包含:GitHub配置只读、文件IO配置只读、上传配置(TUS地址)
    # 右侧包含:资产列表表格、操作按钮、进度条、传输日志
    # ======================================================================
    def _build_tab_transfer(self):
        """
        构建文件传输Tab页面 - 通用上传下载工具

        本Tab为纯粹的通用文件传输工具,与软件自更新业务完全隔离。
        左侧只读展示系统配置中的GitHub和文件IO配置,右侧为Release资产表格、
        操作按钮、进度条,底部为传输运行日志。

        :return: 无返回值,直接将Tab添加到 self.tabWidget 中
        :rtype: None
        """
        # =====** 文件传输Tab主容器 =====**
        self.tab_transfer = QWidget()                    # 创建文件传输Tab的主容器窗口
        self.tabWidget.addTab(self.tab_transfer, "文件传输")  # 将该页面添加到选项卡,标签文字为"文件传输"
        lay = QVBoxLayout(self.tab_transfer)             # 创建垂直布局并应用到主容器
        lay.setContentsMargins(12, 12, 12, 12)           # 设置四周内边距12px
        lay.setSpacing(10)                               # 设置子控件间距10px

        # 中间区域水平布局:左侧配置区 + 右侧资产列表区
        middle_layout = QHBoxLayout()                    # 创建水平布局
        middle_layout.setSpacing(10)                     # 设置左右两栏间距10px

        # 左侧垂直布局:GitHub配置 + 文件IO配置 + 上传配置
        left_layout = QVBoxLayout()                      # 创建左侧垂直布局
        left_layout.setSpacing(10)                       # 设置左侧各组间距10px

        # =====** 左上:GitHub配置只读展示组件 =====**
        # 读取系统配置 github.json,只读展示,不可在此Tab修改
        group_github_ro = QGroupBox("GitHub 配置")   # 创建GitHub配置分组框
        group_github_ro.setMinimumWidth(260)             # 设置最小宽度260px,保证左侧栏宽度一致
        group_github_ro.setStyleSheet(CARD_GROUP_QSS)    # 应用卡片式分组框统一样式
        form_gh = QFormLayout(group_github_ro)           # 创建表单布局并应用到分组框
        form_gh.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        form_gh.setHorizontalSpacing(12)                 # 水平间距12px
        form_gh.setVerticalSpacing(10)                   # 垂直间距10px

        # 创建四个只读输入框:API基础地址、仓库所有者、仓库项目名、访问令牌
        self.le_dl_gh_api_base_url = QLineEdit()         # GitHub API基础地址输入框
        self.le_dl_gh_owner = QLineEdit()                # 仓库所有者输入框
        self.le_dl_gh_name = QLineEdit()                 # 仓库项目名输入框
        self.le_dl_gh_token = QLineEdit()                # 访问令牌输入框
        # 批量设置四个输入框为只读模式
        for le in (self.le_dl_gh_api_base_url, self.le_dl_gh_owner,
                   self.le_dl_gh_name, self.le_dl_gh_token):
            le.setReadOnly(True)                         # 设置为只读,用户不可编辑
        self.le_dl_gh_token.setEchoMode(QLineEdit.EchoMode.Password)  # 令牌输入框设置为密码模式(隐藏内容)
        self.le_dl_gh_api_base_url.setPlaceholderText("https://api.github.com")  # API地址缺省展示提示
        self.le_dl_gh_owner.setPlaceholderText("系统配置中设置仓库所有者")  # 设置占位提示文字
        self.le_dl_gh_name.setPlaceholderText("系统配置中设置仓库项目名")   # 设置占位提示文字
        self.le_dl_gh_token.setPlaceholderText("公开仓库留空")             # 设置占位提示文字

        form_gh.addRow(QLabel("API基础地址:"), self.le_dl_gh_api_base_url)  # 第一行展示github.json的API地址
        form_gh.addRow(QLabel("仓库所有者:"), self.le_dl_gh_owner)   # 添加仓库所有者行
        form_gh.addRow(QLabel("仓库项目名:"), self.le_dl_gh_name)    # 添加仓库项目名行
        form_gh.addRow(QLabel("访问令牌:"), self.le_dl_gh_token)     # 添加访问令牌行
        # 添加提示文字标签,说明配置来源
        tip_gh = QLabel("ℹ️ 以上配置如需修改请前往系统配置。")
        tip_gh.setWordWrap(True)                         # 允许自动换行
        tip_gh.setStyleSheet("color:#9a9a9a; font-size:12px;")  # 浅灰色文字,字号12px
        form_gh.addRow(tip_gh)                           # 添加提示文字行(占两列)
        left_layout.addWidget(group_github_ro)           # 将GitHub配置分组添加到左侧布局

        # =====** 左中:文件IO配置只读展示组件 =====**
        # 读取系统配置 file_io.json,只读展示,不可在此Tab修改
        group_fileio_ro = QGroupBox("下载器 配置")  # 创建文件IO配置分组框
        group_fileio_ro.setMinimumWidth(260)             # 设置最小宽度260px
        group_fileio_ro.setStyleSheet(CARD_GROUP_QSS)    # 应用卡片式分组框统一样式
        form_fio = QFormLayout(group_fileio_ro)          # 创建表单布局
        form_fio.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        form_fio.setHorizontalSpacing(12)                # 水平间距12px
        form_fio.setVerticalSpacing(10)                  # 垂直间距10px

        # 创建四个只读输入框:下载分片、下载并发、上传分片、上传并发
        self.le_dl_dl_chunk = QLineEdit()                # 下载分片大小输入框
        self.le_dl_dl_workers = QLineEdit()              # 下载并发数输入框
        self.le_dl_ul_chunk = QLineEdit()                # 上传分片大小输入框
        self.le_dl_ul_workers = QLineEdit()              # 上传并发数输入框
        # 批量设置四个输入框为只读模式
        for le in (self.le_dl_dl_chunk, self.le_dl_dl_workers,
                   self.le_dl_ul_chunk, self.le_dl_ul_workers):
            le.setReadOnly(True)                         # 设置为只读
        self.le_dl_dl_chunk.setPlaceholderText("默认1048576(1MB)")     # 下载分片占位提示
        self.le_dl_dl_workers.setPlaceholderText("默认4")              # 下载并发占位提示
        self.le_dl_ul_chunk.setPlaceholderText("默认5242880(5MB)")     # 上传分片占位提示
        self.le_dl_ul_workers.setPlaceholderText("默认3")              # 上传并发占位提示

        form_fio.addRow(QLabel("下载分片大小:"), self.le_dl_dl_chunk)    # 添加下载分片行
        form_fio.addRow(QLabel("下载并发数:"), self.le_dl_dl_workers)    # 添加下载并发行
        form_fio.addRow(QLabel("上传分片大小:"), self.le_dl_ul_chunk)    # 添加上传分片行
        form_fio.addRow(QLabel("上传并发数:"), self.le_dl_ul_workers)    # 添加上传并行发
        # 添加提示文字标签
        tip_fio = QLabel("ℹ️ 以上配置如需修改请前往系统配置。")
        tip_fio.setWordWrap(True)                        # 允许自动换行
        tip_fio.setStyleSheet("color:#9a9a9a; font-size:12px;")  # 浅灰色文字,字号12px
        form_fio.addRow(tip_fio)                         # 添加提示文字行
        left_layout.addWidget(group_fileio_ro)           # 将文件IO配置分组添加到左侧布局

        # =====** 左下:上传配置组件(TUS服务地址) =====**
        group_upload_cfg = QGroupBox("上传地址 配置")  # 创建上传配置分组框
        group_upload_cfg.setMinimumWidth(260)            # 设置最小宽度260px
        group_upload_cfg.setStyleSheet(CARD_GROUP_QSS)   # 应用卡片式分组框统一样式
        form_ul = QFormLayout(group_upload_cfg)          # 创建表单布局
        form_ul.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        form_ul.setHorizontalSpacing(12)                 # 水平间距12px
        form_ul.setVerticalSpacing(10)                   # 垂直间距10px

        self.le_dl_tus_url = QLineEdit()                 # TUS服务地址输入框
        self.le_dl_tus_url.setPlaceholderText("TUS服务创建资源地址,例如 https://example.com/files/")  # 设置占位提示
        form_ul.addRow(QLabel("TUS上传地址:"), self.le_dl_tus_url)  # 添加TUS地址行
        # 添加提示文字标签
        tip_ul = QLabel("ℹ️ 服务端必须实现TUS v1.0分片上传协议。")
        tip_ul.setWordWrap(True)                         # 允许自动换行
        tip_ul.setStyleSheet("color:#9a9a9a; font-size:12px;")  # 浅灰色文字,字号12px
        form_ul.addRow(tip_ul)                           # 添加提示文字行
        left_layout.addWidget(group_upload_cfg)          # 将上传配置分组添加到左侧布局
        left_layout.addStretch(1)                        # 添加弹性空间,将上方组件顶到顶部
        middle_layout.addLayout(left_layout, stretch=4)  # 将左侧布局添加到中间布局,占4份宽度

        # =====** 右侧:资产列表+操作按钮+进度条组件 =====**
        group_asset_box = QGroupBox("Release 资产表")   # 创建资产列表分组框
        group_asset_box.setStyleSheet(CARD_GROUP_QSS)    # 应用卡片式分组框统一样式
        right_layout = QVBoxLayout(group_asset_box)      # 创建垂直布局
        right_layout.setSpacing(8)                       # 设置子控件间距8px

        # =====** 操作按钮栏组件 =====**
        # 操作栏放在表格上方:全选/清空 + 获取资产/上传/下载选中
        btn_layout = QHBoxLayout()                       # 创建按钮水平布局
        btn_layout.setSpacing(8)                         # 按钮间距8px
        self.btn_dl_select_all = QPushButton("全选")     # 全选按钮
        self.btn_dl_select_clear = QPushButton("清空选择")  # 清空选择按钮
        self.btn_dl_fetch = QPushButton("获取资产")      # 获取资产按钮(橙色-提醒操作)
        self.btn_dl_upload = QPushButton("上传")         # 上传按钮(绿色-正向操作)
        self.btn_dl_download = QPushButton("下载资产")  # 下载按钮(蓝色-主操作)
        # 批量设置按钮高度和最小宽度
        for btn in (self.btn_dl_select_all, self.btn_dl_select_clear,
                    self.btn_dl_fetch, self.btn_dl_upload, self.btn_dl_download):
            btn.setFixedHeight(25)                       # 固定高度25px
            btn.setMinimumWidth(60)                      # 最小宽度60px
        self.btn_dl_fetch.setStyleSheet(WARN_BTN_QSS)    # 获取资产按钮应用橙色提醒样式
        self.btn_dl_upload.setStyleSheet(SUCCESS_BTN_QSS)  # 上传按钮应用绿色正向样式
        self.btn_dl_download.setStyleSheet(PRIMARY_BTN_QSS)  # 下载按钮应用蓝色主操作样式
        btn_layout.addWidget(self.btn_dl_select_all)     # 添加全选按钮
        btn_layout.addWidget(self.btn_dl_select_clear)   # 添加清空选择按钮
        btn_layout.addStretch(1)                         # 添加弹性空间,将右侧按钮顶到右边
        btn_layout.addWidget(self.btn_dl_fetch)          # 添加获取资产按钮
        btn_layout.addWidget(self.btn_dl_upload)         # 添加上传按钮
        btn_layout.addWidget(self.btn_dl_download)       # 添加下载按钮
        right_layout.addLayout(btn_layout)               # 将按钮布局添加到右侧布局

        # =====** 资产表格组件 =====**
        self.table_dl_asset = QTableWidget()             # 创建资产表格控件
        self.table_dl_asset.setColumnCount(4)            # 设置4列:勾选框、展示名、实际文件名、SHA256
        self.table_dl_asset.setHorizontalHeaderLabels(["", "展示名", "实际文件名", "SHA256"])  # 设置表头文字
        self.table_dl_asset.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)  # 设置不可编辑(只读)
        self.table_dl_asset.setColumnWidth(0, 15)        # 第0列(勾选框)宽度15px
        self.table_dl_asset.setColumnWidth(1, 200)       # 第1列(展示名)宽度200px
        self.table_dl_asset.setColumnWidth(2, 200)       # 第2列(实际文件名)宽度200px
        self.table_dl_asset.horizontalHeader().setStretchLastSection(True)  # 最后一列(SHA256)自动拉伸填充
        self.table_dl_asset.setStyleSheet("""            # 设置表格深色主题样式
        QTableWidget {
            background-color:#1e1e1e; gridline-color:#3a3a3a; color:#e0e0e0;  /* 深灰背景,网格线深灰,文字浅灰 */
            font-size:13px; selection-background-color:#2f6fbd; selection-color:white;  /* 字号13px,选中行蓝底白字 */
            border:1px solid #333; border-radius:4px;    /* 深灰边框,圆角4px */
        }
        QHeaderView::section {
            background-color:#2a2a2a; color:#e0e0e0; padding:6px;  /* 表头深灰背景,浅灰文字,内边距6px */
            border:1px solid #333; font-weight:bold;     /* 深灰边框,加粗字体 */
        }
        QTableWidget QTableCornerButton::section { background-color:#2a2a2a; border:1px solid #333; }  /* 左上角角落按钮样式 */
        """)
        right_layout.addWidget(self.table_dl_asset, stretch=1)  # 将表格添加到右侧布局,占1份弹性空间

        # =====** 传输进度条组件 =====**
        self.progress_transfer = QProgressBar()          # 创建传输进度条
        self.progress_transfer.setValue(0)               # 初始值为0
        self.progress_transfer.setFixedHeight(20)        # 固定高度20px
        self.progress_transfer.setStyleSheet("""        # 设置进度条深色主题样式
        QProgressBar {
            border:1px solid #444; border-radius:4px; text-align:center;  /* 深灰边框,圆角4px,文字居中 */
            background-color:#2b2b2b; color:#ffffff;     /* 深灰背景,白色文字 */
        }
        QProgressBar::chunk { background-color:#28a745; border-radius:3px; }  /* 进度块绿色,圆角3px */
        """)
        right_layout.addWidget(self.progress_transfer)   # 将进度条添加到右侧布局
        middle_layout.addWidget(group_asset_box, stretch=6)  # 将右侧资产列表分组添加到中间布局,占6份宽度

        lay.addLayout(middle_layout, stretch=1)          # 将中间布局添加到主布局,占1份弹性空间

        # =====** 底部:传输运行日志组件 =====**
        group_dl_log = QGroupBox("传输运行日志")         # 创建传输日志分组框
        group_dl_log.setFixedHeight(160)                 # 固定高度180px
        group_dl_log.setStyleSheet(CARD_GROUP_QSS)       # 应用卡片式分组框统一样式
        lay_log = QVBoxLayout(group_dl_log)              # 创建垂直布局
        self.te_transfer_log = QTextEdit()               # 创建传输日志文本框
        self.te_transfer_log.setReadOnly(True)           # 设置为只读
        self.te_transfer_log.setPlaceholderText("等待获取Release资产列表……")  # 设置占位提示文字
        self.te_transfer_log.setStyleSheet("""          # 设置日志文本框深色主题样式
        QTextEdit {
            background-color:#1e1e1e; color:#c0c0c0;     /* 深灰背景,浅灰文字 */
            font-family:Consolas,monospace; font-size:12px;  /* 等宽字体,字号12px */
            border:1px solid #333; border-radius:4px; padding:6px;  /* 深灰边框,圆角4px,内边距6px */
        }
        """)
        lay_log.addWidget(self.te_transfer_log)          # 将日志文本框添加到日志布局
        lay.addWidget(group_dl_log)                      # 将日志分组添加到主布局

    # =====** 文件传输Tab:Controller回填接口组件 =====**
    def fill_github_config_view(self, config: dict):
        """
        用系统配置(github.json)回填左侧只读GitHub配置

        由 Controller 在初始化或配置变更时调用,将GitHub配置数据
        回填到文件传输Tab左侧的只读输入框中。

        :param config: 包含 github 配置的字典,结构为 {"github": {...}}
        :type config: dict
        :return: 无返回值
        :rtype: None
        """
        github = (config or {}).get("github", {})        # 从配置字典中获取github子字典,空安全处理
        self.le_dl_gh_api_base_url.setText(str(            # 回填GitHub API基础地址
            github.get("GITHUB_API_BASE_URL", "https://api.github.com")  # 配置缺失时回退官方API地址
        ))
        self.le_dl_gh_owner.setText(str(github.get("GITHUB_REPO_OWNER", "")))  # 回填仓库所有者
        self.le_dl_gh_name.setText(str(github.get("GITHUB_REPO_NAME", "")))    # 回填仓库项目名
        self.le_dl_gh_token.setText(str(github.get("GITHUB_TOKEN", "") or ""))  # 回填访问令牌

    def fill_fileio_config_view(self, config: dict):
        """
        用系统配置(file_io.json)回填左侧只读文件IO配置

        由 Controller 在初始化或配置变更时调用,将文件IO配置数据
        回填到文件传输Tab左侧的只读输入框中。

        :param config: 包含 file_io 配置的字典,结构为 {"file_io": {...}}
        :type config: dict
        :return: 无返回值
        :rtype: None
        """
        fileio = (config or {}).get("file_io", {})       # 从配置字典中获取file_io子字典,空安全处理
        self.le_dl_dl_chunk.setText(str(fileio.get("DOWNLOAD_CHUNK_SIZE", "")))     # 回填下载分片大小
        self.le_dl_dl_workers.setText(str(fileio.get("DOWNLOAD_MAX_WORKERS", "")))  # 回填下载并发数
        self.le_dl_ul_chunk.setText(str(fileio.get("UPLOAD_CHUNK_SIZE", "")))       # 回填上传分片大小
        self.le_dl_ul_workers.setText(str(fileio.get("UPLOAD_MAX_WORKERS", "")))    # 回填上传并发数

    def fill_download_assets(self, asset_list: list):
        """
        回填Release资产表格数据

        由 Controller 在获取到资产列表后调用,将资产数据填充到表格中。
        每行包含:勾选框、展示名(label中文名)、实际文件名、SHA256哈希值。

        :param asset_list: 资产列表,每个元素为字典,包含 display_name、label、sha256 等字段
        :type asset_list: list
        :return: 无返回值
        :rtype: None
        """
        self.table_dl_asset.setRowCount(0)               # 先清空表格所有行(设置行数为0)
        # 遍历资产列表,逐行添加到表格
        for row_idx, asset in enumerate(asset_list):
            self.table_dl_asset.insertRow(row_idx)       # 在指定索引位置插入新行
            # 第0列:勾选框(存储资产display_name到UserRole供后续下载定位)
            check_item = QTableWidgetItem()              # 创建表格项
            check_item.setCheckState(Qt.CheckState.Unchecked)  # 默认未勾选
            check_item.setData(Qt.ItemDataRole.UserRole, asset.get("display_name", ""))  # 将实际文件名存入UserRole(下载键)
            self.table_dl_asset.setItem(row_idx, 0, check_item)  # 设置第0列的表格项

            # 第1列:展示名(优先显示label中文名,无label时显示display_name文件名)
            item_disp = QTableWidgetItem(str(asset.get("label", "") or asset.get("display_name", "")))  # 创建展示名表格项
            item_disp.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)  # 垂直居中对齐
            self.table_dl_asset.setItem(row_idx, 1, item_disp)  # 设置第1列的表格项

            # 第2列:实际文件名(display_name)
            item_ori = QTableWidgetItem(str(asset.get("display_name", "")))  # 创建实际文件名表格项
            item_ori.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)  # 垂直居中对齐
            self.table_dl_asset.setItem(row_idx, 2, item_ori)  # 设置第2列的表格项

            # 第3列:SHA256哈希值(超长时截断显示,完整值放tooltip)
            sha_raw = asset.get("sha256") or ""          # 获取SHA256原始值
            show_sha = sha_raw[:16] + "……" if len(sha_raw) > 16 else sha_raw  # 超过16字符则截断显示
            item_sha = QTableWidgetItem(show_sha)        # 创建SHA256表格项
            item_sha.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)  # 垂直居中对齐
            item_sha.setToolTip(sha_raw)                 # 将完整SHA256值设为鼠标悬停提示
            self.table_dl_asset.setItem(row_idx, 3, item_sha)  # 设置第3列的表格项

    def select_download_asset(self, display_name: str) -> bool:
        """
        按展示文件名勾选匹配的资产行,其余行取消勾选

        用于 Controller 指定选中某个特定资产时调用,只勾选匹配的行,
        其他所有行取消勾选。返回是否找到匹配的资产。

        :param display_name: 资产展示文件名
        :type display_name: str
        :return: 是否找到并选中了匹配的资产,True=找到,False=未找到
        :rtype: bool
        """
        found = False                                    # 初始化找到标志为False
        # 遍历表格所有行
        for r in range(self.table_dl_asset.rowCount()):
            item = self.table_dl_asset.item(r, 0)        # 获取第0列的勾选框项
            if not item:                                 # 如果项不存在则跳过
                continue
            # 比较UserRole中存储的display_name是否匹配
            if (item.data(Qt.ItemDataRole.UserRole) or "") == display_name:
                item.setCheckState(Qt.CheckState.Checked)  # 匹配则设为勾选状态
                found = True                             # 标记已找到
            else:
                item.setCheckState(Qt.CheckState.Unchecked)  # 不匹配则设为未勾选状态
        return found                                     # 返回是否找到匹配项

    def get_checked_download_assets(self) -> list:
        """
        获取所有已勾选的资产展示文件名列表

        用于下载前获取用户选择的资产列表,遍历表格所有行,
        收集勾选状态为Checked的资产display_name。

        :return: 已勾选资产的展示文件名列表
        :rtype: list
        """
        res = []                                         # 初始化结果列表
        # 遍历表格所有行
        for r in range(self.table_dl_asset.rowCount()):
            item = self.table_dl_asset.item(r, 0)        # 获取第0列的勾选框项
            # 如果项存在且为勾选状态
            if item and item.checkState() == Qt.CheckState.Checked:
                res.append(item.data(Qt.ItemDataRole.UserRole))  # 将资产display_name添加到结果列表
        return res                                       # 返回已勾选的资产名列表

    def append_transfer_log(self, txt: str):
        """
        向传输日志文本框追加一行日志内容

        由 Controller 在传输过程中调用,实时更新传输日志显示。

        :param txt: 要追加的日志文本内容
        :type txt: str
        :return: 无返回值
        :rtype: None
        """
        self.te_transfer_log.append(txt)                 # 向日志文本框追加一行文字

    def set_transfer_progress(self, val: int):
        """
        设置传输进度条的进度值

        由 Controller 在传输过程中调用,实时更新传输进度。
        自动将值限制在 0-100 范围内。

        :param val: 进度值(0-100)
        :type val: int
        :return: 无返回值
        :rtype: None
        """
        # 设置进度条值,使用max/min限制在0-100范围内,确保不会越界
        self.progress_transfer.setValue(max(0, min(100, int(val))))

    def set_transfer_enabled(self, enabled: bool):
        """
        设置文件传输Tab相关按钮的启用/禁用状态

        传输任务运行时由Controller调用,禁用相关按钮防止重复操作;
        任务完成后恢复启用状态。

        :param enabled: 是否启用,True=启用,False=禁用
        :type enabled: bool
        :return: 无返回值
        :rtype: None
        """
        self.btn_dl_fetch.setEnabled(enabled)            # 设置获取资产按钮状态
        self.btn_dl_download.setEnabled(enabled)         # 设置下载按钮状态
        self.btn_dl_upload.setEnabled(enabled)           # 设置上传按钮状态
        self.btn_dl_select_all.setEnabled(enabled)       # 设置全选按钮状态
        self.btn_dl_select_clear.setEnabled(enabled)     # 设置清空选择按钮状态

    # =====** 文件传输Tab:View内部纯界面行为/发信号组件 =====**
    def _dl_select_all(self):
        """
        全选资产表格中的所有行(内部槽函数)

        响应用户点击"全选"按钮,将表格中所有行的勾选框设为选中状态。
        纯界面操作,不涉及业务逻辑。

        :return: 无返回值
        :rtype: None
        """
        # 遍历表格所有行
        for r in range(self.table_dl_asset.rowCount()):
            item = self.table_dl_asset.item(r, 0)        # 获取第0列的勾选框项
            if item:                                     # 如果项存在
                item.setCheckState(Qt.CheckState.Checked)  # 设为勾选状态

    def _dl_clear_all(self):
        """
        清空资产表格中的所有勾选(内部槽函数)

        响应用户点击"清空选择"按钮,将表格中所有行的勾选框设为未选中状态。
        纯界面操作,不涉及业务逻辑。

        :return: 无返回值
        :rtype: None
        """
        # 遍历表格所有行
        for r in range(self.table_dl_asset.rowCount()):
            item = self.table_dl_asset.item(r, 0)        # 获取第0列的勾选框项
            if item:                                     # 如果项存在
                item.setCheckState(Qt.CheckState.Unchecked)  # 设为未勾选状态

    def _dl_click_download(self):
        """
        点击下载按钮的处理函数(内部槽函数)

        响应用户点击"下载选中资产"按钮:
        1. 检查是否有勾选的资产,没有则弹出警告
        2. 弹出目录选择对话框让用户选择保存目录
        3. 发射下载请求信号,将勾选资产列表和保存目录交给Controller处理

        :return: 无返回值
        :rtype: None
        """
        selected_names = self.get_checked_download_assets()  # 获取所有已勾选的资产名列表
        if not selected_names:                               # 如果没有勾选任何资产
            self.show_warning("操作提示", "请至少勾选一项资产包！")  # 弹出警告提示
            return                                            # 直接返回,不继续执行
        # 弹出目录选择对话框,让用户选择文件保存目录
        save_dir = QFileDialog.getExistingDirectory(self, "选择文件保存目录")
        if not save_dir:                                     # 如果用户取消了选择
            return                                            # 直接返回
        # 发射下载请求信号,参数为(勾选的资产名列表, 保存目录路径)
        self.sig_download_requested.emit(selected_names, save_dir)

    def _dl_click_upload(self):
        """
        点击上传按钮的处理函数(内部槽函数)

        响应用户点击"上传"按钮:
        1. 检查TUS服务地址是否填写,未填写则弹出警告
        2. 弹出文件选择对话框让用户选择要上传的本地文件
        3. 发射上传请求信号,将TUS服务地址和本地文件路径交给Controller处理

        :return: 无返回值
        :rtype: None
        """
        # TUS服务地址为本Tab临时输入;文件选择后把(服务地址,本地路径)交给Controller
        tus_endpoint = self.le_dl_tus_url.text().strip()  # 获取TUS服务地址并去除首尾空白
        if not tus_endpoint:                              # 如果地址为空
            self.show_warning("操作提示", "请先填写TUS服务上传地址！")  # 弹出警告提示
            return                                         # 直接返回
        # 弹出文件选择对话框,让用户选择要上传的本地文件
        local_path, _ = QFileDialog.getOpenFileName(self, "选择要上传的本地文件")
        if not local_path:                                # 如果用户取消了选择
            return                                         # 直接返回
        # 发射上传请求信号,参数为(TUS服务地址, 本地文件路径)
        self.sig_upload_requested.emit(tus_endpoint, local_path)

    # ======================================================================
    # 配置管理Tab 构建函数
    # 布局说明:紧凑两列布局,无外层滚动区/底部操作栏
    # 左列:路径配置 + 邮件配置
    # 右列:GitHub配置 + 下载配置 + 网络代理配置
    # 每个QGroupBox就是一类配置,自带保存按钮收进组内;
    # "检查更新"按钮已移到签到工具Tab底部
    # ======================================================================
    def _build_tab_config(self):
        """构建配置管理Tab页面。

        页面采用无外层滚动区的左右两列紧凑布局:左列放路径与邮件配置,
        右列放GitHub、下载及代理配置;左右宽度按6:4分配,为路径输入框保留更多空间。

        :return: 无返回值,直接将配置管理Tab添加到 self.tabWidget
        :rtype: None
        """
        self.tab_config = QWidget()                       # 创建配置管理Tab主容器
        self.tabWidget.addTab(self.tab_config, "配置管理")  # 将配置管理页加入选项卡
        layout = QVBoxLayout(self.tab_config)             # 创建页面最外层垂直布局
        layout.setContentsMargins(12, 12, 12, 12)         # 设置页面四周内边距12px
        layout.setSpacing(10)                             # 设置页面内组件间距10px
        body = QHBoxLayout()                              # 创建左右双列主体布局
        body.setSpacing(10)                               # 设置左右列间距10px
        left = QVBoxLayout()                              # 创建左列布局:路径+邮件
        left.setSpacing(10)                               # 设置左列分组间距10px
        group_path, group_mail = self._build_path_mail_groups()  # 构建左列两个配置分组
        left.addWidget(group_path)                        # 添加路径配置分组
        left.addWidget(group_mail)                        # 添加邮件配置分组
        left.addStretch(1)                                # 添加弹性空间,将分组顶端对齐
        body.addLayout(left, stretch=6)                   # 左列占6份宽度,给路径输入框更多空间
        right = QVBoxLayout()                             # 创建右列布局:GitHub+下载+代理
        right.setSpacing(10)                              # 设置右列分组间距10px
        group_github, group_fileio, group_proxy = self._build_sys_groups()  # 构建右列三个配置分组
        right.addWidget(group_github)                     # 添加GitHub配置分组
        right.addWidget(group_fileio)                     # 添加下载配置分组
        right.addWidget(group_proxy)                      # 添加网络代理配置分组
        right.addStretch(1)                               # 添加弹性空间,将分组顶端对齐
        body.addLayout(right, stretch=4)                  # 右列占4份宽度,形成左宽右窄布局
        layout.addLayout(body)                            # 将双列主体加入页面最外层布局

    # ======================================================================
    # 配置Tab内容块:路径配置 & 邮件通知配置(纯控件创建,返回两个分组框)
    # ======================================================================
    def _build_path_mail_groups(self):
        """
        构建路径配置和邮件配置两个分组框

        路径配置包含8个目录路径设置项,每个都有对应的"选择"按钮。
        邮件配置包含SMTP服务器、发件人、授权码、收件人等设置。

        :return: (路径配置分组框, 邮件配置分组框) 元组
        :rtype: tuple(QGroupBox, QGroupBox)
        """
        # =====** 分组1:路径配置组件 =====**
        group_path = QGroupBox("路径配置")               # 创建路径配置分组框
        group_path.setStyleSheet(CARD_GROUP_QSS)         # 应用卡片式分组框统一样式
        lay_group_path = QFormLayout(group_path)         # 创建表单布局
        lay_group_path.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        lay_group_path.setHorizontalSpacing(12)          # 水平间距12px
        lay_group_path.setVerticalSpacing(12)            # 垂直间距12px

        # =====** 数据目录行 =====**
        # 数据目录(所有数据的根,更改并重启后自动整体迁移)
        self.le_data_root = QLineEdit()                  # 数据目录输入框(全路径)
        self.le_data_root.setPlaceholderText("所有数据的总目录(全路径,更改后重启自动迁移)")  # 占位提示
        self.btn_browse_data_root = QPushButton("选择")  # 浏览选择按钮
        self.btn_browse_data_root.setFixedWidth(72)      # 固定宽度72px
        row_data_root = QHBoxLayout()                    # 创建数据目录行水平布局
        row_data_root.addWidget(self.le_data_root, stretch=1)  # 输入框占弹性空间
        row_data_root.addWidget(self.btn_browse_data_root)  # 添加选择按钮
        lay_group_path.addRow(QLabel("数据目录:"), row_data_root)  # 添加到表单布局

        # =====** cookie目录行 =====**
        self.le_cookie_dir = QLineEdit()                 # cookie目录输入框(相对路径)
        self.le_cookie_dir.setPlaceholderText("相对数据目录的路径,如 cookies")  # 占位提示
        self.btn_browse_cookie = QPushButton("选择")     # 浏览选择按钮
        self.btn_browse_cookie.setFixedWidth(72)         # 固定宽度72px
        row_cookie = QHBoxLayout()                       # 创建水平布局
        row_cookie.addWidget(self.le_cookie_dir, stretch=1)  # 输入框占弹性空间
        row_cookie.addWidget(self.btn_browse_cookie)     # 添加选择按钮
        lay_group_path.addRow(QLabel("cookie目录:"), row_cookie)  # 添加到表单布局

        # =====** 账号目录行 =====**
        self.le_account_dir = QLineEdit()                # 账号目录输入框
        self.le_account_dir.setPlaceholderText("账号详细信息持久化目录")  # 占位提示
        self.btn_browse_account = QPushButton("选择")    # 浏览选择按钮
        self.btn_browse_account.setFixedWidth(72)        # 固定宽度72px
        row_account = QHBoxLayout()                      # 创建水平布局
        row_account.addWidget(self.le_account_dir, stretch=1)  # 输入框占弹性空间
        row_account.addWidget(self.btn_browse_account)   # 添加选择按钮
        lay_group_path.addRow(QLabel("账号目录:"), row_account)  # 添加到表单布局

        # =====** 签到目录行 =====**
        self.le_checkin_record_dir = QLineEdit()         # 签到记录目录输入框
        self.le_checkin_record_dir.setPlaceholderText("签到历史记录保存目录")  # 占位提示
        self.btn_browse_checkin_record = QPushButton("选择")  # 浏览选择按钮
        self.btn_browse_checkin_record.setFixedWidth(72)  # 固定宽度72px
        row_checkin_record = QHBoxLayout()               # 创建水平布局
        row_checkin_record.addWidget(self.le_checkin_record_dir, stretch=1)  # 输入框占弹性空间
        row_checkin_record.addWidget(self.btn_browse_checkin_record)  # 添加选择按钮
        lay_group_path.addRow(QLabel("签到目录:"), row_checkin_record)  # 添加到表单布局

        # =====** 更新目录行 =====**
        self.le_download_dir = QLineEdit()               # 更新下载目录输入框
        self.le_download_dir.setPlaceholderText("程序更新包、附件下载目录")  # 占位提示
        self.btn_browse_download = QPushButton("选择")   # 浏览选择按钮
        self.btn_browse_download.setFixedWidth(72)       # 固定宽度72px
        row_download = QHBoxLayout()                     # 创建水平布局
        row_download.addWidget(self.le_download_dir, stretch=1)  # 输入框占弹性空间
        row_download.addWidget(self.btn_browse_download)  # 添加选择按钮
        lay_group_path.addRow(QLabel("更新目录:"), row_download)  # 添加到表单布局

        # =====** 配置目录行 =====**
        self.le_config_root_dir = QLineEdit()            # 配置根目录输入框
        self.le_config_root_dir.setPlaceholderText("所有底层配置json存放根目录")  # 占位提示
        self.btn_browse_config_root = QPushButton("选择")  # 浏览选择按钮
        self.btn_browse_config_root.setFixedWidth(72)    # 固定宽度72px
        row_config_root = QHBoxLayout()                  # 创建水平布局
        row_config_root.addWidget(self.le_config_root_dir, stretch=1)  # 输入框占弹性空间
        row_config_root.addWidget(self.btn_browse_config_root)  # 添加选择按钮
        lay_group_path.addRow(QLabel("配置目录:"), row_config_root)  # 添加到表单布局

        # =====** 邮件目录行 =====**
        # 邮件目录(签到结果/邮件正文固定文件名mail_body.txt)
        self.le_mail_dir = QLineEdit()                   # 邮件目录输入框
        self.le_mail_dir.setPlaceholderText("签到结果与邮件正文保存目录(文件名固定:mail_body.txt)")  # 占位提示
        self.btn_browse_mail = QPushButton("选择")       # 浏览选择按钮
        self.btn_browse_mail.setFixedWidth(72)           # 固定宽度72px
        row_mail = QHBoxLayout()                         # 创建水平布局
        row_mail.addWidget(self.le_mail_dir, stretch=1)  # 输入框占弹性空间
        row_mail.addWidget(self.btn_browse_mail)         # 添加选择按钮
        lay_group_path.addRow(QLabel("邮件目录:"), row_mail)  # 添加到表单布局

        # =====** 日志目录行 =====**
        # 日志目录(软件运行日志固定文件名app.log)
        self.le_log_dir = QLineEdit()                    # 日志目录输入框
        self.le_log_dir.setPlaceholderText("程序运行日志存放目录(文件名固定:app.log,更改后重启生效)")  # 占位提示
        self.btn_browse_log = QPushButton("选择")        # 浏览选择按钮
        self.btn_browse_log.setFixedWidth(72)            # 固定宽度72px
        row_log = QHBoxLayout()                          # 创建水平布局
        row_log.addWidget(self.le_log_dir, stretch=1)    # 输入框占弹性空间
        row_log.addWidget(self.btn_browse_log)           # 添加选择按钮
        lay_group_path.addRow(QLabel("日志目录:"), row_log)  # 添加到表单布局

        # =====** 路径配置独立保存按钮行 =====**
        path_btn_wrap = QWidget()                         # 创建按钮包装容器,用于占满表单内容列
        path_btn_row = QHBoxLayout(path_btn_wrap)         # 创建保存按钮水平布局
        path_btn_row.setContentsMargins(0, 0, 0, 0)       # 清除包装容器内边距
        path_btn_row.addStretch(1)                        # 左侧添加弹性空间,使按钮右对齐
        self.btn_save_path = QPushButton("保存")  # 创建路径配置独立保存按钮
        self.btn_save_path.setStyleSheet(PRIMARY_BTN_QSS) # 应用蓝色主操作按钮样式
        path_btn_row.addWidget(self.btn_save_path)        # 将保存按钮添加到行尾
        lay_group_path.addRow(path_btn_wrap)              # 将按钮行添加到路径表单底部

        # =====** 邮件配置分组 =====**
        group_mail = QGroupBox("邮件配置")               # 创建邮件配置分组框
        group_mail.setStyleSheet(CARD_GROUP_QSS)         # 应用卡片式分组框统一样式
        lay_group_mail = QFormLayout(group_mail)         # 创建表单布局
        lay_group_mail.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        lay_group_mail.setHorizontalSpacing(12)          # 水平间距12px
        lay_group_mail.setVerticalSpacing(12)            # 垂直间距12px

        self.cb_enable_mail = QCheckBox("启用邮件发送报告")  # 启用邮件复选框
        self.cb_enable_mail.setToolTip("签到任务结束后,自动发送签到结果邮件到接收邮箱")  # 设置提示信息
        lay_group_mail.addRow(self.cb_enable_mail)       # 添加复选框行(占两列)

        self.le_smtp_host = QLineEdit()                  # SMTP服务器地址输入框
        self.le_smtp_host.setPlaceholderText("SMTP服务器地址,例如 smtp.qq.com")  # 占位提示
        lay_group_mail.addRow(QLabel("SMTP服务器:"), self.le_smtp_host)  # 添加到表单布局

        self.le_smtp_port = QLineEdit()                  # SMTP端口输入框
        self.le_smtp_port.setPlaceholderText("SMTP端口,例如 465")  # 占位提示
        lay_group_mail.addRow(QLabel("SMTP端 口:"), self.le_smtp_port)  # 添加到表单布局(空格用于对齐)

        self.le_sender_email = QLineEdit()               # 发件邮箱输入框
        self.le_sender_email.setPlaceholderText("发件邮箱地址")  # 占位提示
        lay_group_mail.addRow(QLabel("发件邮箱:"), self.le_sender_email)  # 添加到表单布局

        self.le_smtp_code = QLineEdit()                  # SMTP授权码输入框
        self.le_smtp_code.setPlaceholderText("SMTP授权码")  # 占位提示
        self.le_smtp_code.setEchoMode(QLineEdit.EchoMode.Password)  # 设置为密码模式(隐藏内容)
        lay_group_mail.addRow(QLabel("SMTP授权码:"), self.le_smtp_code)  # 添加到表单布局

        self.le_receive_email = QLineEdit()              # 接收邮箱输入框
        self.le_receive_email.setPlaceholderText("接收报告的邮箱")  # 占位提示
        lay_group_mail.addRow(QLabel("接收邮箱:"), self.le_receive_email)  # 添加到表单布局

        # =====** 邮件配置独立保存/重置按钮行 =====**
        mail_btn_wrap = QWidget()                         # 创建邮件操作按钮包装容器
        mail_btn_row = QHBoxLayout(mail_btn_wrap)         # 创建邮件操作按钮水平布局
        mail_btn_row.setContentsMargins(0, 0, 0, 0)       # 清除包装容器内边距
        mail_btn_row.addStretch(1)                        # 左侧留弹性空间,使按钮组右对齐
        self.btn_save_mail = QPushButton("配置邮件")      # 创建邮件配置保存按钮
        self.btn_save_mail.setStyleSheet(PRIMARY_BTN_QSS) # 应用蓝色主操作按钮样式
        self.btn_reset_mail = QPushButton("重置")         # 创建仅清空邮件表单的重置按钮
        mail_btn_row.addWidget(self.btn_save_mail)        # 添加配置邮件按钮
        mail_btn_row.addWidget(self.btn_reset_mail)       # 添加重置按钮
        lay_group_mail.addRow(mail_btn_wrap)              # 将按钮组添加到邮件表单底部

        return group_path, group_mail                    # 返回两个分组框

    # ======================================================================
    # 配置Tab内容块:系统配置 GitHub & 文件IO & 网络代理(纯控件创建)
    # ======================================================================
    def _build_sys_groups(self):
        """
        构建系统配置的三个分组框:GitHub配置、文件IO配置、网络代理配置

        每个分组框内包含各自的配置项,其中GitHub组内含"检查更新"按钮,
        代理组内含"检测代理"按钮。

        :return: (GitHub配置分组框, 文件IO配置分组框, 网络代理配置分组框) 元组
        :rtype: tuple(QGroupBox, QGroupBox, QGroupBox)
        """
        # =====** GitHub配置分组组件 =====**
        group_github = QGroupBox("GitHub配置")           # 创建GitHub配置分组框
        group_github.setStyleSheet(CARD_GROUP_QSS)       # 应用卡片式分组框统一样式
        lay_github = QFormLayout(group_github)           # 创建表单布局
        lay_github.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        lay_github.setHorizontalSpacing(12)              # 水平间距12px
        lay_github.setVerticalSpacing(12)                # 垂直间距12px

        self.le_github_owner = QLineEdit()               # 仓库所有者输入框
        self.le_github_owner.setPlaceholderText("仓库所有者名称,例如:xxx")  # 占位提示
        lay_github.addRow(QLabel("仓库所有者:"), self.le_github_owner)  # 添加到表单布局

        self.le_github_name = QLineEdit()                # 仓库项目名输入框
        self.le_github_name.setPlaceholderText("仓库项目名,例如:签到Tool")  # 占位提示
        lay_github.addRow(QLabel("仓库项目名:"), self.le_github_name)  # 添加到表单布局

        self.le_update_pkg = QLineEdit()                 # 更新包文件名模板输入框
        self.le_update_pkg.setPlaceholderText("自动下载更新包文件名模板,{tag}会替换为版本号")  # 占位提示
        lay_github.addRow(QLabel("更新软件包:"), self.le_update_pkg)  # 添加到表单布局

        self.le_github_token = QLineEdit()               # GitHub访问令牌输入框
        self.le_github_token.setPlaceholderText("Token,公开仓库留空")  # 占位提示
        self.le_github_token.setEchoMode(QLineEdit.EchoMode.Password)  # 设置为密码模式
        self.le_github_token.setMaximumWidth(250)        # 缩短令牌输入框,为同行保存按钮预留空间
        self.btn_save_github = QPushButton("保存")  # 创建GitHub配置独立保存按钮
        self.btn_save_github.setStyleSheet(PRIMARY_BTN_QSS)  # 应用蓝色主操作按钮样式
        github_token_row = QHBoxLayout()                  # 创建令牌输入框+保存按钮同行布局
        github_token_row.setContentsMargins(0, 0, 0, 0)  # 清除同行布局内边距
        github_token_row.setSpacing(8)                    # 设置输入框与按钮间距8px
        github_token_row.addWidget(self.le_github_token, stretch=1)  # 令牌输入框占可用弹性空间
        github_token_row.addWidget(self.btn_save_github)  # 保存按钮固定在令牌输入框右侧
        lay_github.addRow(QLabel("访问令牌:"), github_token_row)  # 将组合行添加到GitHub表单

        self.cb_auto_check_update = QCheckBox("启动程序时自动检查新版本")  # 自动检查更新复选框
        lay_github.addRow(self.cb_auto_check_update)     # 添加复选框行

        # =====** 下载配置分组组件 =====**
        group_fileio = QGroupBox("下载配置")             # 创建下载/上传分片与并发配置分组框
        group_fileio.setStyleSheet(CARD_GROUP_QSS)       # 应用卡片式分组框统一样式
        lay_fileio = QFormLayout(group_fileio)           # 创建表单布局
        lay_fileio.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        lay_fileio.setHorizontalSpacing(12)              # 水平间距12px
        lay_fileio.setVerticalSpacing(12)                # 垂直间距12px

        self.le_download_chunk = QLineEdit()             # 下载分片大小输入框
        self.le_download_chunk.setPlaceholderText("下载分片大小(字节),默认1048576(1MB)")  # 占位提示
        lay_fileio.addRow(QLabel("下载分片大小:"), self.le_download_chunk)  # 添加到表单布局

        self.le_download_workers = QLineEdit()           # 下载并发数输入框
        self.le_download_workers.setPlaceholderText("下载最大并发线程数,默认4")  # 占位提示
        lay_fileio.addRow(QLabel("下载并发数:"), self.le_download_workers)  # 添加到表单布局

        self.le_upload_chunk = QLineEdit()               # 上传分片大小输入框
        self.le_upload_chunk.setPlaceholderText("上传分片大小(字节),默认5242880(5MB)")  # 占位提示
        lay_fileio.addRow(QLabel("上传分片大小:"), self.le_upload_chunk)  # 添加到表单布局

        self.le_upload_workers = QLineEdit()             # 上传并发数输入框
        self.le_upload_workers.setPlaceholderText("上传最大并发线程数,默认3")  # 占位提示
        self.le_upload_workers.setMaximumWidth(250)      # 缩短上传并发输入框,为同行保存按钮预留空间
        self.btn_save_fileio = QPushButton("保存")  # 创建下载配置独立保存按钮
        self.btn_save_fileio.setStyleSheet(PRIMARY_BTN_QSS)  # 应用蓝色主操作按钮样式
        upload_workers_row = QHBoxLayout()                # 创建上传并发输入框+保存按钮同行布局
        upload_workers_row.setContentsMargins(0, 0, 0, 0)  # 清除同行布局内边距
        upload_workers_row.setSpacing(8)                  # 设置输入框与按钮间距8px
        upload_workers_row.addWidget(self.le_upload_workers, stretch=1)  # 输入框占可用弹性空间
        upload_workers_row.addWidget(self.btn_save_fileio)  # 保存按钮固定在上传并发输入框右侧
        lay_fileio.addRow(QLabel("上传并发数:"), upload_workers_row)  # 将组合行加入下载配置表单

        # =====** 网络代理分组组件 =====**
        group_proxy = QGroupBox("网络代理配置")          # 创建网络代理配置分组框
        group_proxy.setStyleSheet(CARD_GROUP_QSS)        # 应用卡片式分组框统一样式
        lay_proxy = QFormLayout(group_proxy)             # 创建表单布局
        lay_proxy.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        lay_proxy.setHorizontalSpacing(12)               # 水平间距12px
        lay_proxy.setVerticalSpacing(12)                 # 垂直间距11px

        self.cb_auto_proxy = QCheckBox("自动检测系统代理")  # 自动检测代理复选框
        lay_proxy.addRow(self.cb_auto_proxy)             # 添加复选框行

        self.cb_use_proxy = QCheckBox("启用代理(手动填写)")  # 启用手动代理复选框
        lay_proxy.addRow(self.cb_use_proxy)              # 添加复选框行

        self.le_http_proxy = QLineEdit()                 # HTTP代理地址输入框
        self.le_http_proxy.setPlaceholderText("HTTP代理地址,示例:127.0.0.1")  # 占位提示
        lay_proxy.addRow(QLabel("HTTP代理地址:"), self.le_http_proxy)  # 添加到表单布局

        self.le_https_proxy = QLineEdit()                # HTTPS代理地址输入框
        self.le_https_proxy.setPlaceholderText("HTTPS代理地址,示例:127.0.0.1")  # 占位提示
        lay_proxy.addRow(QLabel("HTTPS代理地址:"), self.le_https_proxy)  # 添加到表单布局

        self.le_proxy_port = QLineEdit()                 # 代理端口输入框
        self.le_proxy_port.setPlaceholderText("代理端口,示例:7890")  # 占位提示
        lay_proxy.addRow(QLabel("代理端口:"), self.le_proxy_port)  # 添加到表单布局

        # =====** "检测代理"按钮组件(网络代理的专属操作,收进本分组,右对齐) =====**
        px_btn_wrap = QWidget()                          # 创建按钮包装容器
        px_btn_row = QHBoxLayout(px_btn_wrap)            # 创建按钮水平布局
        px_btn_row.setContentsMargins(0, 0, 0, 0)        # 无内边距
        px_btn_row.addStretch(1)                         # 添加弹性空间,将按钮顶到右侧
        self.btn_detect_proxy = QPushButton("检测代理")  # 检测代理按钮
        self.btn_detect_proxy.setToolTip("读取操作系统代理设置并回填")  # 设置提示信息
        self.btn_save_proxy = QPushButton("保存")  # 创建代理配置独立保存按钮
        self.btn_save_proxy.setStyleSheet(PRIMARY_BTN_QSS)  # 应用蓝色主操作按钮样式
        px_btn_row.addWidget(self.btn_detect_proxy)      # 添加检测代理按钮到布局
        px_btn_row.addWidget(self.btn_save_proxy)        # 添加保存代理按钮到布局
        lay_proxy.addRow(px_btn_wrap)                    # 将按钮包装器添加到表单布局

        # 勾选自动检测时手动代理项整体灰显(运行时实时跟随系统代理)
        self.cb_auto_proxy.toggled.connect(self._on_auto_proxy_toggled)  # 连接自动检测复选框的状态变化信号到槽函数

        return group_github, group_fileio, group_proxy   # 返回三个分组框

    # ======================================================================
    # 签到工具Tab 构建函数
    # 布局比例:左侧3 : 右侧7
    # 左侧:账号信息统计 + 账号列表
    # 右侧:账号管理(登录) + 账号详情 + 签到日志
    # 底部:操作按钮栏 + 进度条
    # ======================================================================
    def _build_tab3(self):
        """
        构建签到工具Tab页面

        包含账号管理(添加/删除)、账号列表、账号详情、单账号签到、
        批量签到、签到日志等功能。布局比例为左3右7。

        :return: 无返回值,直接将Tab添加到 self.tabWidget 中
        :rtype: None
        """
        # =====** 签到工具Tab主容器 =====**
        self.tab_sign = QWidget()                        # 创建签到工具Tab的主容器窗口
        self.tabWidget.addTab(self.tab_sign, "签到工具")  # 将该页面添加到选项卡
        lay3 = QVBoxLayout(self.tab_sign)                # 创建垂直布局
        lay3.setContentsMargins(12, 12, 12, 12)          # 设置四周内边距12px
        lay3.setSpacing(10)                              # 设置子控件间距10px

        # 主体区域水平布局:左侧账号区 + 右侧签到区
        body_layout = QHBoxLayout()                      # 创建主体水平布局
        body_layout.setSpacing(12)                       # 左右两栏间距12px

        # =====** 左侧:账号汇总信息 + 账号列表 =====**
        left_layout = QVBoxLayout()                      # 创建左侧垂直布局
        left_layout.setSpacing(10)                       # 子控件间距10px

        # =====** 账号信息统计组件 =====**
        group_summary = QGroupBox("账号信息统计")        # 创建账号统计分组框
        group_summary.setStyleSheet("QGroupBox { font-weight: bold; font-size:14px; padding-top:14px; }")  # 分组框样式:加粗,14px字号
        lay_summary = QVBoxLayout(group_summary)         # 创建垂直布局
        self.lab_total_accounts = QLabel("账号总数:0")   # 账号总数标签
        self.lab_signed_today = QLabel("今日签到:0")     # 今日已签到数标签
        self.lab_total_flow = QLabel("签到流量:暂无")    # 今日签到获取流量标签
        lay_summary.addWidget(self.lab_total_accounts)   # 添加账号总数标签
        lay_summary.addWidget(self.lab_signed_today)     # 添加今日签到标签
        lay_summary.addWidget(self.lab_total_flow)       # 添加签到流量标签
        left_layout.addWidget(group_summary)             # 将统计分组添加到左侧布局

        # =====** 账号列表组件 =====**
        group_acct = QGroupBox("账号列表")               # 创建账号列表分组框
        group_acct.setStyleSheet("QGroupBox { font-weight: bold; font-size:14px; padding-top:14px; }")  # 分组框样式
        lay_acct = QVBoxLayout(group_acct)               # 创建垂直布局
        self.list_accounts = QListWidget()               # 创建账号列表控件
        self.list_accounts.setToolTip("账号列表,点击查看账号详情")  # 设置鼠标悬停提示
        lay_acct.addWidget(self.list_accounts)           # 将列表添加到布局
        left_layout.addWidget(group_acct, stretch=1)     # 将账号列表分组添加到左侧布局,占1份弹性空间
        body_layout.addLayout(left_layout, stretch=3)    # 将左侧布局添加到主体布局,占3份宽度

        # =====** 右侧:登录签到 + 账号详情 + 日志 =====**
        right_layout = QVBoxLayout()                     # 创建右侧垂直布局
        right_layout.setSpacing(10)                      # 子控件间距10px

        # =====** 账号管理组件(登录/添加/删除) =====**
        group_login = QGroupBox("账号管理")              # 创建账号管理分组框
        group_login.setStyleSheet("QGroupBox { font-weight: bold; font-size:14px; padding-top:14px; }")  # 分组框样式
        lay_group_login = QFormLayout(group_login)       # 创建表单布局
        lay_group_login.setLabelAlignment(Qt.AlignmentFlag.AlignRight)  # 标签右对齐
        lay_group_login.setHorizontalSpacing(12)         # 水平间距12px
        lay_group_login.setVerticalSpacing(9)            # 垂直间距8px
        self.le_sign_account = QLineEdit()               # 签到账号输入框
        self.le_sign_account.setPlaceholderText("签到账号邮箱")  # 占位提示
        lay_group_login.addRow(QLabel("签到账号:"), self.le_sign_account)  # 添加到表单布局
        self.le_sign_pwd = QLineEdit()                   # 签到密码输入框
        self.le_sign_pwd.setPlaceholderText("签到密码")  # 占位提示
        self.le_sign_pwd.setEchoMode(QLineEdit.EchoMode.Password)  # 设置为密码模式
        lay_group_login.addRow(QLabel("签到密码:"), self.le_sign_pwd)  # 添加到表单布局

        # =====** 添加/删除账号按钮组 =====**
        btn_acct_layout = QHBoxLayout()                  # 创建按钮水平布局
        self.btn_add_account = QPushButton("添加账号")   # 添加账号按钮
        self.btn_del_account = QPushButton("删除账号")   # 删除账号按钮
        self.btn_add_account.setFixedWidth(110)          # 添加按钮固定宽度110px
        self.btn_del_account.setFixedWidth(110)          # 删除按钮固定宽度110px
        btn_acct_layout.addWidget(self.btn_add_account)  # 添加添加账号按钮
        btn_acct_layout.addWidget(self.btn_del_account)  # 添加删除账号按钮
        btn_acct_layout.addStretch(1)                    # 添加弹性空间,将按钮顶到左侧
        lay_group_login.addRow("", btn_acct_layout)      # 添加按钮行到表单布局(标签为空)
        right_layout.addWidget(group_login)              # 将账号管理分组添加到右侧布局

        # =====** 账号详情面板组件 =====**
        group_detail = QGroupBox("详细信息")             # 创建账号详情分组框
        group_detail.setStyleSheet("QGroupBox { font-weight: bold; font-size:14px; padding-top:14px; }")  # 分组框样式
        lay_detail = QVBoxLayout(group_detail)           # 创建垂直布局
        self.te_account_detail = QTextEdit()             # 创建账号详情文本框
        self.te_account_detail.setReadOnly(True)         # 设置为只读
        self.te_account_detail.setPlaceholderText("单击左侧列表账号,查看账号全部详情")  # 占位提示
        self.te_account_detail.setMaximumHeight(240)     # 最大高度240px,避免占用过多空间
        lay_detail.addWidget(self.te_account_detail)     # 将详情文本框添加到布局
        right_layout.addWidget(group_detail)             # 将详情分组添加到右侧布局

        # =====** 签到日志组件 =====**
        group_log = QGroupBox("签到日志")                # 创建签到日志分组框
        group_log.setStyleSheet("QGroupBox { font-weight: bold; font-size:14px; padding-top:14px; }")  # 分组框样式
        lay_log = QVBoxLayout(group_log)                 # 创建垂直布局
        self.te_sign_log = QTextEdit()                   # 创建签到日志文本框
        self.te_sign_log.setReadOnly(True)               # 设置为只读
        self.te_sign_log.setPlaceholderText("签到过程日志输出区域……")  # 占位提示
        lay_log.addWidget(self.te_sign_log)              # 将日志文本框添加到布局
        right_layout.addWidget(group_log, stretch=1)     # 将日志分组添加到右侧布局,占1份弹性空间

        body_layout.addLayout(right_layout, stretch=7)   # 将右侧布局添加到主体布局,占7份宽度
        lay3.addLayout(body_layout, stretch=1)           # 将主体布局添加到主布局,占1份弹性空间

        # =====** 底部操作栏组件 =====**
        bottom_layout = QVBoxLayout()                    # 创建底部垂直布局
        btn_row = QHBoxLayout()                          # 创建按钮水平布局
        self.btn_sign_single = QPushButton("单账号签到")  # 单账号签到按钮
        self.btn_sign_batch = QPushButton("批量签到")    # 批量签到按钮
        self.btn_clear_log = QPushButton("清空日志")     # 清空日志按钮
        self.btn_check_update = QPushButton("检查更新")   # 创建签到工具Tab底部检查更新按钮
        self.btn_check_update.setStyleSheet(SUCCESS_BTN_QSS)  # 应用绿色正向操作样式
        self.btn_check_update.setToolTip("检查软件是否有新版本")  # 设置按钮悬停提示
        self.btn_sign_single.setFixedWidth(120)          # 单账号签到按钮固定宽度120px
        self.btn_sign_batch.setFixedWidth(120)           # 批量签到按钮固定宽度120px
        self.btn_clear_log.setFixedWidth(100)            # 清空日志按钮固定宽度100px
        btn_row.addWidget(self.btn_sign_single)          # 添加单账号签到按钮
        btn_row.addWidget(self.btn_sign_batch)           # 添加批量签到按钮
        btn_row.addWidget(self.btn_clear_log)            # 添加清空日志按钮
        btn_row.addStretch(1)                            # 将检查更新按钮推到操作栏最右侧
        btn_row.addWidget(self.btn_check_update)          # 添加检查更新按钮
        bottom_layout.addLayout(btn_row)                 # 将按钮行添加到底部布局

        # =====** 签到进度条组件 =====**
        self.pb_sign_progress = QProgressBar()           # 创建签到进度条
        self.pb_sign_progress.setRange(0, 100)           # 设置进度范围0-100
        self.pb_sign_progress.setValue(0)                # 初始值为0
        self.pb_sign_progress.setStyleSheet("""         # 设置进度条深色主题样式
            QProgressBar {
                border: 1px solid #444444;               /* 深灰边框 */
                border-radius: 4px;                      /* 圆角4px */
                text-align: center;                      /* 文字居中 */
                background-color: #2b2b2b;               /* 深灰背景 */
                color: #ffffff;                          /* 白色文字 */
                height:18px;                             /* 高度18px */
            }
            QProgressBar::chunk {
                background-color: #28a745;               /* 进度块绿色 */
                border-radius:3px;                       /* 圆角3px */
            }
        """)
        bottom_layout.addWidget(self.pb_sign_progress)   # 将进度条添加到底部布局
        lay3.addLayout(bottom_layout)                    # 将底部布局添加到主布局

    # ======================================================================
    # View内部事件处理函数
    # 职责:只收集界面数据/发射信号,不做任何业务逻辑处理
    # ======================================================================
    def _on_save_path(self):
        """收集路径表单并发射独立保存路径配置信号。

        数据目录保留全路径,其余目录按界面当前显示值提交(正常为相对数据目录路径)。

        :return: 无返回值
        :rtype: None
        """
        self.sig_save_path.emit({                         # 将路径字段统一组装后交给Controller保存
            "data_root": self.le_data_root.text().strip(),
            "cookie_dir": self.le_cookie_dir.text().strip(),
            "account_dir": self.le_account_dir.text().strip(),
            "checkin_record_dir": self.le_checkin_record_dir.text().strip(),
            "download_dir": self.le_download_dir.text().strip(),
            "config_root_dir": self.le_config_root_dir.text().strip(),
            "mail_dir": self.le_mail_dir.text().strip(),
            "log_dir": self.le_log_dir.text().strip(),
        })

    def _on_save_mail(self):
        """收集邮件表单并发射独立保存邮件配置信号。

        :return: 无返回值
        :rtype: None
        """
        self.sig_save_mail.emit({                         # 组装SMTP配置及邮件开关后交给Controller
            "enable_mail": self.cb_enable_mail.isChecked(),
            "smtp_host": self.le_smtp_host.text().strip(),
            "smtp_port": self.le_smtp_port.text().strip(),
            "sender_email": self.le_sender_email.text().strip(),
            "smtp_code": self.le_smtp_code.text(),
            "receive_email": self.le_receive_email.text().strip(),
        })

    def _on_reset_mail(self):
        """重置邮件配置输入区,不影响路径配置及已落盘数据。

        :return: 无返回值
        :rtype: None
        """
        self.cb_enable_mail.setChecked(False)             # 取消启用邮件发送开关
        self.le_smtp_host.clear()
        self.le_smtp_port.clear()
        self.le_sender_email.clear()
        self.le_smtp_code.clear()
        self.le_receive_email.clear()

    def _on_save_github(self):
        """收集GitHub与更新检查表单并发射独立保存信号。

        :return: 无返回值
        :rtype: None
        """
        self.sig_save_github.emit({                       # 组装仓库、令牌及更新选项后交给Controller
            "github_owner": self.le_github_owner.text().strip(),
            "github_name": self.le_github_name.text().strip(),
            "update_pkg": self.le_update_pkg.text().strip(),
            "github_token": self.le_github_token.text(),
            "auto_check_update": self.cb_auto_check_update.isChecked(),
        })

    def _on_save_fileio(self):
        """收集下载/上传分片与并发参数并发射独立保存信号。

        :return: 无返回值
        :rtype: None
        """
        self.sig_save_fileio.emit({                       # 原始文本交给Controller统一校验数字格式
            "download_chunk": self.le_download_chunk.text().strip(),
            "download_workers": self.le_download_workers.text().strip(),
            "upload_chunk": self.le_upload_chunk.text().strip(),
            "upload_workers": self.le_upload_workers.text().strip(),
        })

    def _on_save_proxy(self):
        """收集系统代理模式、地址和端口并发射独立保存信号。

        :return: 无返回值
        :rtype: None
        """
        self.sig_save_proxy.emit({                        # 组装代理字段后交给Controller校验与保存
            "auto_detect": self.cb_auto_proxy.isChecked(),
            "use_proxy": self.cb_use_proxy.isChecked(),
            "http_proxy": self.le_http_proxy.text().strip(),
            "https_proxy": self.le_https_proxy.text().strip(),
            "proxy_port": self.le_proxy_port.text().strip(),
        })

    def _on_auto_proxy_toggled(self, checked: bool):
        """
        自动检测代理复选框状态变化处理(内部槽函数)

        当勾选"自动检测系统代理"时,手动代理的四项输入控件灰显(不可编辑);
        取消勾选时恢复可编辑状态。

        :param checked: 复选框是否被勾选,True=勾选,False=取消勾选
        :type checked: bool
        :return: 无返回值
        :rtype: None
        """
        manual_enabled = not checked                      # 手动代理是否启用 = 自动检测未勾选
        self.cb_use_proxy.setEnabled(manual_enabled)      # 设置启用代理复选框状态
        self.le_http_proxy.setEnabled(manual_enabled)     # 设置HTTP代理输入框状态
        self.le_https_proxy.setEnabled(manual_enabled)    # 设置HTTPS代理输入框状态
        self.le_proxy_port.setEnabled(manual_enabled)     # 设置代理端口输入框状态

    def fill_detected_proxy(self, http_host: str, https_host: str, port: str):
        """
        将检测到的系统代理回填到手动代理表单

        由 Controller 检测完系统代理后调用:
        1. 取消自动检测模式
        2. 启用手动代理
        3. 填入检测到的代理地址和端口

        :param http_host: HTTP代理地址
        :type http_host: str
        :param https_host: HTTPS代理地址,为空时使用HTTP代理地址
        :type https_host: str
        :param port: 代理端口号
        :type port: str
        :return: 无返回值
        :rtype: None
        """
        self.cb_auto_proxy.setChecked(False)              # 取消自动检测代理勾选
        self.cb_use_proxy.setChecked(True)                # 勾选启用手动代理
        self.le_http_proxy.setText(http_host)             # 回填HTTP代理地址
        self.le_https_proxy.setText(https_host or http_host)  # 回填HTTPS代理地址,为空则用HTTP地址
        self.le_proxy_port.setText(port)                  # 回填代理端口

    def _on_add_account(self):
        """
        添加账号按钮点击处理(内部槽函数)

        响应用户点击"添加账号"按钮,获取输入框中的邮箱和密码,
        发射 sig_add_account 信号,交给 Controller 处理持久化。

        :return: 无返回值
        :rtype: None
        """
        # 邮箱+密码一起提交持久化(密码用于Cookie失效后的自动登录与批量签到)
        self.sig_add_account.emit(
            self.le_sign_account.text().strip(),          # 邮箱账号(去除首尾空格)
            self.le_sign_pwd.text()                       # 密码(保留原始内容)
        )

    def _on_del_account(self):
        """
        删除账号按钮点击处理(内部槽函数)

        响应用户点击"删除账号"按钮,获取当前选中的账号邮箱,
        发射 sig_del_account 信号,交给 Controller 处理删除。

        :return: 无返回值
        :rtype: None
        """
        item = self.list_accounts.currentItem()           # 获取列表中当前选中的项
        self.sig_del_account.emit(item.text() if item else "")  # 发射删除账号信号,无选中项则传空字符串

    def _on_account_selected(self, item):
        """
        账号列表选中项变化处理(内部槽函数)

        响应用户点击账号列表中的某项:
        1. 将选中的邮箱回填到签到账号输入框
        2. 发射 sig_account_selected 信号,通知Controller加载账号详情

        :param item: 被点击的列表项对象
        :type item: QListWidgetItem
        :return: 无返回值
        :rtype: None
        """
        if item:                                          # 如果项存在
            self.le_sign_account.setText(item.text())     # 将账号邮箱回填到签到账号输入框
            self.sig_account_selected.emit(item.text())   # 发射账号选中信号,通知Controller加载详情

    def _on_start_sign_click(self):
        """
        单账号签到按钮点击处理(内部槽函数)

        响应用户点击"单账号签到"按钮,获取输入框中的账号和密码,
        发射 sig_sign_started 信号,交给 Controller 处理签到。

        :return: 无返回值
        :rtype: None
        """
        self.sig_sign_started.emit(
            self.le_sign_account.text().strip(),          # 签到账号邮箱
            self.le_sign_pwd.text()                       # 签到密码
        )

    def _on_batch_sign_click(self):
        """
        批量签到按钮点击处理(内部槽函数)

        响应用户点击"批量签到"按钮,发射 sig_sign_batch 信号,
        交给 Controller 处理所有已保存账号的批量签到。

        :return: 无返回值
        :rtype: None
        """
        self.sig_sign_batch.emit()                        # 发射批量签到信号

    def _on_open_update_click(self):
        """
        检查更新按钮点击处理(内部槽函数)

        响应用户点击"检查更新"按钮,发射 sig_open_update 信号,
        交给 Controller 处理软件更新检查。

        :return: 无返回值
        :rtype: None
        """
        self.sig_open_update.emit()                       # 发射打开更新窗口信号

    # ======================================================================
    # Controller 调用的公开接口(界面刷新/弹窗)
    # ======================================================================
    def fill_form_data(self, data: dict):
        """
        根据Controller传入的展示数据回填整个表单

        由 Controller 在初始化或配置变更时调用,一次性回填所有配置页面的数据。

        :param data: 包含所有配置数据的字典,结构如下:
            {
                "paths": {data_root, cookie_dir, account_dir, checkin_record_dir,
                         download_dir, config_root_dir, mail_dir, log_dir},
                "mail":  {enable_mail, smtp_host, smtp_port, sender_email,
                         smtp_code, receive_email},
                "github": {owner, name, token, update_pkg, auto_check_update},
                "file_io": {download_chunk, download_workers, upload_chunk, upload_workers},
                "proxy":  {auto_detect, use_proxy, http_proxy, https_proxy, proxy_port},
                "accounts": [email, ...],
                "summary": {total, signed_today}
            }
        :type data: dict
        :return: 无返回值
        :rtype: None
        """
        paths = data.get("paths", {})                     # 获取路径配置字典(Model已统一返回根目录+相对子目录)
        mail = data.get("mail", {})                       # 获取邮件配置字典
        github = data.get("github", {})                   # 获取GitHub配置字典
        proxy = data.get("proxy", {})                     # 获取代理配置字典

        # =====** 回填Tab1 路径配置 =====**
        self.le_data_root.setText(str(paths.get("data_root", "")))              # 回填数据根目录
        self.le_cookie_dir.setText(str(paths.get("cookie_dir", "")))            # 回填cookie目录
        self.le_account_dir.setText(str(paths.get("account_dir", "")))          # 回填账号目录
        self.le_checkin_record_dir.setText(str(paths.get("checkin_record_dir", "")))  # 回填签到记录目录
        self.le_download_dir.setText(str(paths.get("download_dir", "")))        # 回填更新目录
        self.le_config_root_dir.setText(str(paths.get("config_root_dir", "")))  # 回填配置目录
        self.le_mail_dir.setText(str(paths.get("mail_dir", "")))                # 回填邮件目录
        self.le_log_dir.setText(str(paths.get("log_dir", "")))                  # 回填日志目录

        # =====** 回填Tab1 邮件配置 =====**
        self.cb_enable_mail.setChecked(bool(mail.get("enable_mail", False)))    # 回填启用邮件复选框
        self.le_smtp_host.setText(str(mail.get("smtp_host", "")))               # 回填SMTP服务器
        self.le_smtp_port.setText(str(mail.get("smtp_port", "")))               # 回填SMTP端口
        self.le_sender_email.setText(str(mail.get("sender_email", "")))         # 回填发件邮箱
        self.le_smtp_code.setText(str(mail.get("smtp_code", "")))               # 回填SMTP授权码
        self.le_receive_email.setText(str(mail.get("receive_email", "")))       # 回填接收邮箱

        # =====** 回填Tab2 GitHub配置 =====**
        self.le_github_owner.setText(str(github.get("owner", "")))              # 回填仓库所有者
        self.le_github_name.setText(str(github.get("name", "")))                # 回填仓库项目名
        self.le_update_pkg.setText(str(github.get("update_pkg", "")))           # 回填更新包模板
        self.le_github_token.setText(str(github.get("token", "") or ""))        # 回填访问令牌
        self.cb_auto_check_update.setChecked(bool(github.get("auto_check_update", True)))  # 回填自动检查更新

        # =====** 回填Tab2 文件IO配置 =====**
        fileio = data.get("file_io", {})                   # 获取文件IO配置字典
        self.le_download_chunk.setText(str(fileio.get("download_chunk", "")))     # 回填下载分片大小
        self.le_download_workers.setText(str(fileio.get("download_workers", "")))  # 回填下载并发数
        self.le_upload_chunk.setText(str(fileio.get("upload_chunk", "")))         # 回填上传分片大小
        self.le_upload_workers.setText(str(fileio.get("upload_workers", "")))    # 回填上传并发数

        # =====** 回填Tab2 代理配置 =====**
        self.cb_auto_proxy.setChecked(bool(proxy.get("auto_detect", False)))     # 回填自动检测代理
        self.cb_use_proxy.setChecked(bool(proxy.get("use_proxy", False)))        # 回填启用手动代理
        self.le_http_proxy.setText(str(proxy.get("http_proxy", "")))             # 回填HTTP代理地址
        self.le_https_proxy.setText(str(proxy.get("https_proxy", "")))           # 回填HTTPS代理地址
        self.le_proxy_port.setText(str(proxy.get("proxy_port", "")))             # 回填代理端口

        # =====** 回填Tab3 账号列表+统计 =====**
        # (summary由Model按今日签到实况计算)
        self.refresh_accounts(data.get("accounts", []), data.get("summary"))     # 刷新账号列表和统计信息

    def refresh_accounts(self, accounts: list, summary: dict | None = None):
        """
        刷新账号列表并更新全账号汇总信息(纯界面)

        由 Controller 在账号列表变化时调用,重新渲染账号列表和统计信息。

        :param accounts: 账号邮箱列表
        :type accounts: list
        :param summary: 账号汇总统计字典,来自 Model.get_checkin_summary() 的结果,
                        包含 total(账号总数) 和 signed_today(今日已签数);
                        None时前两栏按0显示。第三栏(选中账号今日收益)不在此更新,
                        由Controller在选中切换/签到完成时调update_selected_gain
        :type summary: dict | None
        :return: 无返回值
        :rtype: None
        """
        self.list_accounts.clear()                          # 清空账号列表
        # 遍历账号列表,逐行添加到列表控件
        for email in accounts:
            self.list_accounts.addItem(str(email))          # 添加账号邮箱到列表
        summary = summary if isinstance(summary, dict) else {}  # 确保summary为字典类型
        # 更新账号统计信息(总数和今日已签数)
        self.update_summary(
            int(summary.get("total", len(accounts))),       # 账号总数,默认取列表长度
            int(summary.get("signed_today", 0)),            # 今日已签数,默认0
        )

    def update_summary(self, total: int, signed_today: int):
        """
        刷新左侧全账号统计(前两栏:账号总数、今日已签到数)

        由 Controller 在统计数据变化时调用。

        :param total: 账号总数
        :type total: int
        :param signed_today: 今日已签到账号数
        :type signed_today: int
        :return: 无返回值
        :rtype: None
        """
        self.lab_total_accounts.setText(f"账号总数:{total}")           # 更新账号总数标签
        self.lab_signed_today.setText(f"今日已签到:{signed_today}")    # 更新今日已签到标签

    def update_selected_gain(self, today_gain: str):
        """
        刷新第三栏:当前选中账号今日签到获得的流量

        由 Controller 在选中账号切换或签到完成时调用,
        更新第三栏显示的今日签到获取流量。未签到显示"暂无"。

        :param today_gain: 今日签到获得的流量文案(可能是完整句子或纯数值)
        :type today_gain: str
        :return: 无返回值
        :rtype: None
        """
        show = _short_flow(today_gain)                      # 调用工具函数精简流量文案
        self.lab_total_flow.setText(f"今日签到获取:{show or '暂无'}")  # 更新流量显示标签,空则显示"暂无"

    def show_account_detail_text(self, text: str | None):
        """
        显示账号详细信息文本

        文本由Controller调用comm_tools.tools.format_user_info_text统一渲染
        (与签到邮件正文/结果文件完全同一份),View层只做纯显示,不做业务格式化。

        :param text: 账号详细信息文本,None或空则显示空
        :type text: str | None
        :return: 无返回值
        :rtype: None
        """
        self.te_account_detail.setPlainText(text or "")     # 设置详情文本框内容,None则设为空字符串

    def clear_account_detail(self):
        """
        清空账号详情面板

        :return: 无返回值
        :rtype: None
        """
        self.te_account_detail.clear()                      # 清空账号详情文本框

    def fill_sign_password(self, password: str):
        """
        回填选中账号的登录密码

        选中左侧账号后,由Controller回填该账号已持久化的登录密码到密码输入框。

        :param password: 账号登录密码
        :type password: str
        :return: 无返回值
        :rtype: None
        """
        self.le_sign_pwd.setText(password or "")            # 回填密码到输入框,None则设为空字符串

    def sign_password(self) -> str:
        """
        读取签到密码框当前内容

        供 Controller 在单账号登录成功后用于自动记忆密码。

        :return: 密码输入框中的当前文本
        :rtype: str
        """
        return self.le_sign_pwd.text()                      # 返回密码输入框的文本内容

    def append_log(self, text: str):
        """
        向签到日志文本框追加一行日志

        由 Controller 在签到过程中调用,实时更新签到日志显示。

        :param text: 要追加的日志文本
        :type text: str
        :return: 无返回值
        :rtype: None
        """
        self.te_sign_log.append(text)                       # 向签到日志文本框追加一行文字

    def clear_log(self):
        """
        清空签到日志

        响应用户点击"清空日志"按钮,清除所有签到日志内容。

        :return: 无返回值
        :rtype: None
        """
        self.te_sign_log.clear()                            # 清空签到日志文本框

    def set_progress(self, value: int):
        """
        设置签到进度条的值

        由 Controller 在签到过程中调用,实时更新签到进度。

        :param value: 进度值(0-100)
        :type value: int
        :return: 无返回值
        :rtype: None
        """
        self.pb_sign_progress.setValue(value)               # 设置签到进度条的值

    def set_status(self, text: str):
        """
        设置状态栏显示文字

        由 Controller 在状态变化时调用,更新状态栏提示信息。

        :param text: 状态栏要显示的文字
        :type text: str
        :return: 无返回值
        :rtype: None
        """
        self.statusbar.showMessage(text)                    # 在状态栏显示消息

    def set_sign_enabled(self, enabled: bool):
        """
        设置签到相关按钮的启用/禁用状态

        签到任务运行时由Controller调用,禁用签到按钮防止重复操作;
        任务完成后恢复启用状态。

        :param enabled: 是否启用,True=启用,False=禁用
        :type enabled: bool
        :return: 无返回值
        :rtype: None
        """
        self.btn_sign_single.setEnabled(enabled)            # 设置单账号签到按钮状态
        self.btn_sign_batch.setEnabled(enabled)             # 设置批量签到按钮状态

    def current_selected_account(self) -> str:
        """
        获取当前选中的账号邮箱

        :return: 当前选中的账号邮箱字符串,无选中则返回空字符串
        :rtype: str
        """
        item = self.list_accounts.currentItem()             # 获取列表中当前选中的项
        return item.text() if item else ""                  # 返回选中项的文字,无选中则返回空字符串

    def select_account(self, email: str) -> bool:
        """
        按邮箱选中列表中的账号项

        用于列表重建后恢复选中状态。命中返回True,未找到返回False。
        注意:setCurrentItem不会触发itemClicked,调用方需自行刷新详情/统计栏。

        :param email: 要选中的账号邮箱
        :type email: str
        :return: 是否找到并选中了匹配的账号,True=找到,False=未找到
        :rtype: bool
        """
        target = (email or "").strip()                      # 去除首尾空格,处理空值
        if not target:                                      # 如果目标为空
            return False                                    # 直接返回False
        # 遍历列表所有项
        for i in range(self.list_accounts.count()):
            item = self.list_accounts.item(i)               # 获取第i项
            if item is not None and item.text() == target:  # 如果项存在且文字匹配
                self.list_accounts.setCurrentItem(item)     # 设置为当前选中项
                return True                                 # 返回True表示找到
        return False                                        # 遍历完未找到,返回False

    # =====** 标准弹窗接口组件 =====**
    def show_info(self, title: str, text: str):
        """
        显示信息提示框

        委托 common_dialog 模块弹出信息提示框,保证全项目弹窗风格统一。

        :param title: 对话框标题
        :type title: str
        :param text: 对话框正文内容
        :type text: str
        :return: 无返回值
        :rtype: None
        """
        common_dialog.show_info(self, title, text)          # 调用通用对话框模块显示信息提示

    def show_warning(self, title: str, text: str):
        """
        显示警告提示框

        委托 common_dialog 模块弹出警告提示框,保证全项目弹窗风格统一。

        :param title: 对话框标题
        :type title: str
        :param text: 对话框正文内容
        :type text: str
        :return: 无返回值
        :rtype: None
        """
        common_dialog.show_warning(self, title, text)       # 调用通用对话框模块显示警告提示

    def show_error(self, title: str, text: str):
        """
        显示错误提示框

        委托 common_dialog 模块弹出错误提示框,保证全项目弹窗风格统一。

        :param title: 对话框标题
        :type title: str
        :param text: 对话框正文内容
        :type text: str
        :return: 无返回值
        :rtype: None
        """
        common_dialog.show_error(self, title, text)         # 调用通用对话框模块显示错误提示

    def show_question(self, title: str, text: str, ok_text: str = "确认",
                      cancel_text: str = "取消", danger: bool = False) -> bool:
        """
        显示确认/取消对话框

        委托 common_dialog 模块弹出确认对话框,按钮文案由Controller按场景传入。

        :param title: 对话框标题
        :type title: str
        :param text: 对话框正文内容
        :type text: str
        :param ok_text: 确认按钮文案,默认"确认"
        :type ok_text: str
        :param cancel_text: 取消按钮文案,默认"取消"
        :type cancel_text: str
        :param danger: 是否为危险操作,True时确认按钮标红,默认False
        :type danger: bool
        :return: 用户点击结果,True=确认,False=取消
        :rtype: bool
        """
        return common_dialog.show_confirm(
            self, title, text,
            ok_text=ok_text, cancel_text=cancel_text, danger=danger,
        )                                                   # 调用通用对话框模块显示确认对话框

    def force_close(self):
        """
        强制关闭窗口并退出应用

        后台任务已停止后,由Controller调用真正关闭窗口并退出应用程序。
        设置 _allow_close 为 True 后,closeEvent 会直接接受关闭。

        :return: 无返回值
        :rtype: None
        """
        self._allow_close = True                            # 设置允许关闭标志为True
        self.close()                                        # 关闭主窗口
        QApplication.quit()                                 # 退出应用程序

    # ======================================================================
    # View内部纯UI行为:目录选择对话框(不涉及业务数据)
    # ======================================================================
    def _browse_dir(self, line_edit: QLineEdit, title: str):
        """
        弹出目录选择对话框,并将选择的路径填入指定的输入框

        纯界面工具方法,用于所有"选择"按钮的目录浏览功能。
        选择后自动在路径末尾添加系统路径分隔符。

        :param line_edit: 要回填路径的 QLineEdit 输入框对象
        :type line_edit: QLineEdit
        :param title: 目录选择对话框的标题文字
        :type title: str
        :return: 无返回值
        :rtype: None
        """
        # 目录选择器优先从当前数据目录打开;数据目录本身就是最终根目录,不会额外追加data_store
        data_root = self.le_data_root.text().strip()
        initial_dir = data_root if data_root and os.path.isdir(data_root) else "./data_store"
        selected_path = QFileDialog.getExistingDirectory(self, title, initial_dir)
        if selected_path:                                  # 用户确认选择后才更新编辑框
            if line_edit is not self.le_data_root and data_root:  # 子目录必须相对数据目录展示
                try:
                    relative = os.path.relpath(selected_path, data_root)  # 计算相对数据根目录路径
                    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                        self.show_warning("路径选择", "子目录必须选择在当前数据目录内部！")
                        return                             # 禁止选择数据目录之外的位置
                    selected_path = relative              # 界面只显示相对子路径
                except ValueError:
                    self.show_warning("路径选择", "子目录必须与数据目录位于同一磁盘！")
                    return                                 # Windows跨盘符无法作为数据目录子路径
            line_edit.setText(os.path.normpath(selected_path))  # 规范化后回填选择结果

    # ======================================================================
    # 信号绑定函数(只连View内部槽函数,不涉及Controller)
    # ======================================================================
    def _bind_signals(self):
        """
        绑定所有UI控件的信号与View内部槽函数

        本方法只绑定View内部的信号-槽连接,所有向外发射的自定义信号
        由 Controller 在外部连接。按照功能Tab分组组织代码。

        :return: 无返回值
        :rtype: None
        """
        # =====** Tab1 路径配置-浏览目录按钮 =====**
        self.btn_browse_data_root.clicked.connect(lambda: self._browse_dir(self.le_data_root, "选择数据目录"))
        self.btn_browse_cookie.clicked.connect(lambda: self._browse_dir(self.le_cookie_dir, "选择cookie目录"))
        self.btn_browse_account.clicked.connect(lambda: self._browse_dir(self.le_account_dir, "选择账号目录"))
        self.btn_browse_checkin_record.clicked.connect(lambda: self._browse_dir(self.le_checkin_record_dir, "选择签到目录"))
        self.btn_browse_download.clicked.connect(lambda: self._browse_dir(self.le_download_dir, "选择更新目录"))
        self.btn_browse_config_root.clicked.connect(lambda: self._browse_dir(self.le_config_root_dir, "选择配置目录"))
        self.btn_browse_mail.clicked.connect(lambda: self._browse_dir(self.le_mail_dir, "选择邮件目录"))
        self.btn_browse_log.clicked.connect(lambda: self._browse_dir(self.le_log_dir, "选择日志目录"))

        # =====** 配置管理Tab 独立保存按钮 =====**
        self.btn_save_path.clicked.connect(self._on_save_path)
        self.btn_save_mail.clicked.connect(self._on_save_mail)
        self.btn_reset_mail.clicked.connect(self._on_reset_mail)
        self.btn_save_github.clicked.connect(self._on_save_github)
        self.btn_save_fileio.clicked.connect(self._on_save_fileio)
        self.btn_save_proxy.clicked.connect(self._on_save_proxy)
        self.btn_detect_proxy.clicked.connect(self.sig_detect_proxy.emit)

        # =====** Tab3 签到工具-账号管理 =====**
        self.list_accounts.itemClicked.connect(self._on_account_selected)   # 账号列表点击
        self.btn_add_account.clicked.connect(self._on_add_account)          # 添加账号按钮
        self.btn_del_account.clicked.connect(self._on_del_account)          # 删除账号按钮

        # =====** Tab3 签到工具-签到操作 =====**
        self.btn_sign_single.clicked.connect(self._on_start_sign_click)     # 单账号签到按钮
        self.btn_sign_batch.clicked.connect(self._on_batch_sign_click)      # 批量签到按钮
        self.btn_clear_log.clicked.connect(self.clear_log)                  # 清空日志按钮
        self.btn_check_update.clicked.connect(self._on_open_update_click)

        # =====** 文件传输Tab =====**
        self.btn_dl_fetch.clicked.connect(self.sig_fetch_assets_requested.emit)  # 获取资产按钮(直接发射信号)
        self.btn_dl_select_all.clicked.connect(self._dl_select_all)         # 全选按钮
        self.btn_dl_select_clear.clicked.connect(self._dl_clear_all)        # 清空选择按钮
        self.btn_dl_download.clicked.connect(self._dl_click_download)       # 下载按钮
        self.btn_dl_upload.clicked.connect(self._dl_click_upload)           # 上传按钮

    # ======================================================================
    # 关闭事件处理
    # 默认不直接关闭,询问Controller(可能有后台任务运行中)
    # ======================================================================
    def closeEvent(self, event):
        """
        窗口关闭事件处理函数(重写自 QMainWindow)

        拦截用户点击窗口关闭按钮的事件:
        - 如果 _allow_close 为 True(Controller已确认可以关闭),则直接接受关闭
        - 如果 _allow_close 为 False(可能有后台任务运行),则忽略关闭事件,
          并发射 sig_close_requested 信号,由 Controller 判断是否可以关闭

        :param event: 关闭事件对象
        :type event: QCloseEvent
        :return: 无返回值
        :rtype: None
        """
        if self._allow_close:                               # 如果允许直接关闭
            event.accept()                                  # 接受关闭事件,窗口关闭
            return                                          # 直接返回
        event.ignore()                                      # 忽略关闭事件,窗口不关闭
        self.sig_close_requested.emit()                     # 发射关闭请求信号,由Controller处理
