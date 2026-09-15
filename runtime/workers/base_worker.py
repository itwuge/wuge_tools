# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: base_worker.py
# 归属: runtime/workers 后台任务线程层 —— Worker 公共基类
# ------------------------------------------------------------------------------
# 文件用途:
#   定义所有后台任务 Worker 的公共基类 BaseWorker,统一"日志落盘 app.log +
#   Qt 信号推送 UI"的双写能力。子类继承后直接调用 self._log() 即可,
#   避免在每个 Worker 的 run() 内部重复定义相同的日志闭包函数。
# ------------------------------------------------------------------------------
# 架构定位:
#   本模块是 QThread 工作线程的抽象基类,自身不执行业务逻辑,只提供:
#   1. 线程内日志记录能力(落盘 + 信号推送 UI 双写)
#   2. 按子类类名自动获取 logging 日志器,便于区分日志来源
#   子类在 run() 中调用 service 业务层,通过 Signal 向 controller/view 发事件,
#   严格遵守 Qt 线程模型,不直接操作 UI 控件。
#
#   关联组件:
#     - 上游: 各具体 Worker 子类(SignMailWorker、BatchSignWorker 等)
#     - 下游: logging、PySide6.QtCore
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 以具体子类类名自动获取 logging 日志器(self._logger),日志按类名区分来源
#   2. _log() 方法将同一条消息同时写入 app.log 文件并经 signal_log 推送到界面日志区
#   3. 继承自 QThread,提供标准的 Qt 线程生命周期管理
# ------------------------------------------------------------------------------
# 职责边界:
#   - 作为基类不依赖任何业务模块,只提供日志双写与 QThread 基础能力
#   - 子类必须在自己的类作用域内重新定义 signal_log = Signal(str)
#   - 子类 run() 内直接调用 self._log(msg, level) 即可实现日志双写
# ------------------------------------------------------------------------------
# 线程模型:
#   signal_log(str) 在子线程内 emit,Qt 自动以队列连接(QueuedConnection)
#   跨线程投递给 UI 线程的日志槽函数;
#   日志器为标准 logging.Logger,随全局日志配置落盘到 app.log;
#   子类必须在自己的类作用域内重新定义 signal_log = Signal(str),
#   因为 Qt 的 Signal 必须在类定义时声明才能正确绑定元对象。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging(负责把日志按级别落盘到 app.log)
#   - 第三方: PySide6.QtCore(QThread、Signal)
#   - 项目内: 无(作为基类不依赖任何业务模块)
# ==============================================================================

# ==============================================================================
# 【Worker 基类】提取所有 Worker 共用的日志双写能力（落盘 app.log + 推送 UI）
# 子类只需继承 BaseWorker，直接调用 self._log(msg) 即可
# 避免每个 Worker 的 run() 内部重复定义相同的 _log 闭包
# ==============================================================================
import logging  # 导入标准库日志模块:负责把日志按级别落盘到 app.log 文件

from PySide6.QtCore import QThread, Signal  # 导入 Qt 线程基类 QThread 与信号定义工具 Signal


# =========** [Base]Worker - 所有 Worker 的公共基类 **=========
class BaseWorker(QThread):
    """Worker 基类：统一日志双写（落盘文件 + UI 信号推送）。

    所有后台任务 Worker 的公共父类，封装了日志双写能力和 QThread 基础能力。
    子类只需继承并调用 self._log() 即可同时实现文件落盘和界面日志推送。
    logger 自动以子类类名获取（如 UpdateWorker -> logging.getLogger("UpdateWorker")），
    子类如需自定义可在 __init__ 中覆盖 self._logger 属性。

    子类约定：
        - 直接继承 BaseWorker，__init__ 中调用 super().__init__(parent)
        - 类内定义 signal_log = Signal(str)（若需推送 UI 日志）
        - run() 内直接调用 self._log(msg, level) 即可
    """

    # ------------------------------------------------------------------
    # 类属性（Qt 信号）
    # ------------------------------------------------------------------
    # 日志信号:载荷为一条已格式化好的日志文本(str)，UI 线程接收后追加到日志区
    # 注意：Qt 的 Signal 必须在类作用域定义，不能在 __init__ 中动态创建
    signal_log = Signal(str)  # 定义日志信号，参数类型为字符串，用于向 UI 线程推送日志文本

    # ------------------------------------------------------------------
    # 实例属性（在 __init__ 中初始化）
    # ------------------------------------------------------------------
    # self._logger: logging.Logger 日志器实例，以子类类名命名，用于文件落盘
    # self._parent: QObject | None Qt 父对象，用于线程/对象生命周期管理（继承自 QThread）

    def __init__(self, parent=None):
        """初始化 Worker 基类，并创建以子类类名命名的日志器。

        :param parent: Qt 父对象，用于线程/对象生命周期管理，
                       通常为发起任务的控制器或窗口对象
        :type parent: QObject | None
        :return: 无返回值
        :rtype: None
        :raises RuntimeError: 若 Qt 元对象系统初始化失败
        """
        super().__init__(parent)  # 调用 QThread 父类的构造函数，绑定 Qt 父对象以管理生命周期
        self._logger = logging.getLogger(self.__class__.__name__)  # 以具体子类的类名作为 logger 名称，便于在日志中区分不同 Worker 的来源

    def _log(self, msg: str, level: int = logging.INFO) -> None:
        """同一步骤同时落盘 app.log 文件并推送到 UI 日志区（图标不省略）。

        将同一条日志消息同时写入两个通道：
        1. 通过 logging.Logger 按指定级别写入 app.log 落盘文件
        2. 通过 Qt 信号 signal_log 推送到 UI 线程的日志显示区域

        :param msg: 待记录并推送的日志文本（可含 emoji 图标，用于增强可读性）
        :type msg: str
        :param level: logging 日志级别，默认为 logging.INFO（20），
                      可选值：logging.DEBUG(10)、logging.INFO(20)、
                      logging.WARNING(30)、logging.ERROR(40)、logging.CRITICAL(50)
        :type level: int
        :return: 无返回值
        :rtype: None
        :raises RuntimeError: 若信号发射失败（如 Qt 线程已销毁）
        """
        self._logger.log(level, msg)  # 按指定的日志级别将消息写入 app.log 落盘日志文件
        self.signal_log.emit(msg)  # 发射 signal_log 信号，把同一条文本推送到界面日志区显示
