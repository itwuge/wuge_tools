# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: install_worker.py
# 归属: updater_app/workers —— 安装工作线程（Install Worker）
# ------------------------------------------------------------------------------
# 文件用途:
#   更新助手 Worker 层——文件替换安装线程（QThread）。
#   把 installer.install_update 放到子线程执行，避免"等待旧主程序退出"
#   期间冻结 UI。纯转发实现，安装算法全部在 updater_app/installer.py。
# ------------------------------------------------------------------------------
# 架构定位:
#   Worker 层的安装工作线程，在 QThread 子线程中执行安装操作。
#   作为 Controller 和 installer 之间的桥梁，将同步的安装函数
#   包装为异步的线程执行，避免阻塞 UI 主线程。
#
#   关联组件:
#     - 上游: UpdateAssistantController（Controller，创建并启动本线程）
#     - 下游: updater_app.installer.install_update（安装器核心函数）
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 在子线程中调用 installer.install_update 执行安装
#   2. 通过 signal_log 信号转发安装日志到 UI
#   3. 通过 signal_finish 信号返回安装结果
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只负责将安装操作放到子线程执行，不实现安装算法
#   - 不直接操作 UI 控件，通过信号回主线程
#   - 不 import 任何 view 模块
# ------------------------------------------------------------------------------
# 线程模型:
#   本类继承自 QThread，run() 方法在子线程中执行。
#   通过信号（signal_log、signal_finish）与主线程通信，
#   Qt 会自动将信号排队到接收者所在线程的事件队列。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging
#   - 第三方: PySide6.QtCore
#   - 项目内: updater_app.installer（安装器模块）
# ==============================================================================

# 导入日志记录库，用于记录工作线程的日志
import logging

# 导入 Qt 核心模块的 QThread（线程基类）和 Signal（信号类）
from PySide6.QtCore import QThread, Signal


# ==============================================================================
# 类: InstallWorker
# ==============================================================================
# =========** [InstallWorker][Worker] =========
# 关联组件:
#   - [UpdateAssistantController][Controller]: Controller 创建并管理本线程实例
#   - [installer.install_update][Service]: 本线程在 run() 中调用安装核心函数
# ==============================================================================
class InstallWorker(QThread):
    """
    安装工作线程类，在子线程中执行软件更新安装。

    详细说明:
        继承自 QThread，将 installer.install_update 包装为异步执行。
        通过 signal_log 信号实时转发安装日志，
        通过 signal_finish 信号返回最终安装结果。
        安装过程不可取消（防止文件损坏）。

    Signals:
        signal_log (str): 安装日志信号，携带日志文本
        signal_finish (dict): 安装完成信号，携带结果字典

    Attributes:
        _logger (logging.Logger): 日志器实例
        _package_path (str): 更新包文件的绝对路径
        _app_dir (str): 软件安装目录的绝对路径
        _app_exe (str): 旧主程序可执行文件的绝对路径
        _old_pid (int): 旧主程序的进程 ID
        _self_exe_name (str): 更新助手自身的可执行文件名
        _wait_exe_name (str): 独立模式下按此进程名等待主程序关闭
    """
    # 安装日志信号，参数为日志文本字符串
    signal_log = Signal(str)
    # 安装完成信号，参数为结果字典
    signal_finish = Signal(dict)

    def __init__(self, package_path: str, app_dir: str, app_exe: str, old_pid: int,
                 self_exe_name: str, wait_exe_name: str = "", parent=None):
        """
        初始化安装工作线程。

        参数:
            package_path (str): 更新包文件的绝对路径
            app_dir (str): 软件安装目录的绝对路径
            app_exe (str): 旧主程序可执行文件的绝对路径
            old_pid (int): 旧主程序的进程 ID；任务模式>0，独立模式=0
            self_exe_name (str): 更新助手自身的可执行文件名
            wait_exe_name (str): 独立模式下按此进程名等待主程序关闭；默认为空串
            parent (QObject): 父对象；默认为 None

        返回值:
            None

        异常:
            无
        """
        # 调用父类 QThread 的构造函数
        super().__init__(parent)
        # 创建日志器实例，命名空间为 Updater.InstallWorker
        self._logger = logging.getLogger("Updater.InstallWorker")
        # 保存更新包路径
        self._package_path = package_path
        # 保存软件目录路径
        self._app_dir = app_dir
        # 保存旧主程序路径
        self._app_exe = app_exe
        # 保存旧主程序 PID
        self._old_pid = old_pid
        # 保存更新助手自身文件名
        self._self_exe_name = self_exe_name
        # 保存等待的主程序名（独立模式使用）
        self._wait_exe_name = wait_exe_name

    def run(self):
        """
        线程主函数，在子线程中执行安装操作。

        详细说明:
            延迟导入 installer 模块（避免循环依赖或过早导入），
            调用 install_update 执行完整安装流程，
            将日志回调设为 signal_log.emit（通过信号回主线程），
            安装完成后发射 signal_finish 信号返回结果。

        参数:
            无

        返回值:
            None（通过 signal_finish 信号返回结果）

        异常:
            不向外抛出异常；所有异常都在 installer.install_update 内部捕获
        """
        # 延迟导入安装器模块（避免模块级循环依赖）
        from updater_app.installer import install_update
        # 调用安装器核心函数执行完整安装流程
        # log_cb 设置为 signal_log.emit，日志通过信号回主线程
        result = install_update(
            # 更新包路径
            package_path=self._package_path,
            # 软件安装目录
            app_dir=self._app_dir,
            # 旧主程序路径
            app_exe=self._app_exe,
            # 旧主程序 PID
            old_pid=self._old_pid,
            # 更新助手自身文件名
            self_exe_name=self._self_exe_name,
            # 日志回调函数（信号的 emit 方法）
            log_cb=self.signal_log.emit,
            # 独立模式下按此进程名等待主程序关闭
            wait_exe_name=self._wait_exe_name,
        )
        # 安装完成，发射完成信号，携带结果字典
        self.signal_finish.emit(result)
