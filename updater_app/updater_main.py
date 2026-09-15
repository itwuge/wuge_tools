# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: updater_main.py
# 归属: updater_app 更新助手包 —— 主入口模块 Main Entry
# ------------------------------------------------------------------------------
# 文件用途:
#   独立更新助手 update.exe 的唯一入口 薄引导层,仅负责装配 MVC 三层。
#   作为程序的起点,负责初始化日志、创建 QApplication、组装 Model/View/Controller,
#   最后启动 Qt 事件循环。
# ------------------------------------------------------------------------------
# 架构定位:
#   整个更新助手的启动入口,相当于 MVC 架构的"装配器"。
#   不包含任何业务逻辑,只负责:
#     1. 调用 bootstrap 完成最早期路径解析
#     2. 初始化日志系统
#     3. 创建 Qt 应用实例
#     4. 实例化 Model / View / Controller 并建立关联
#     5. 启动更新流程并进入事件循环
#
#   两种启动方式共用同一条 MVC 链路:
#     A. 独立运行: 双击 update.exe / python -m updater_app.updater_main 无参
#        ——助手自行推断软件目录/数据目录/主程序路径/配置 见 updater_app.bootstrap
#     B. 主程序移交: update.exe "<任务JSON路径>"
#        ——任务字段优先 精准 old_pid/透传配置,JSON 读不到时自动降级为独立运行
# ------------------------------------------------------------------------------
# 核心功能:
#   1. _resolve_resource_path —— 资源路径解析 兼容开发模式与 PyInstaller
#   2. _init_logger           —— 初始化独立日志文件
#   3. main                   —— 主入口函数,装配 MVC 并启动
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅作为程序入口和装配器,不包含业务逻辑
#   - 不直接操作 UI 控件细节 交给 View
#   - 不直接处理数据 交给 Model
#   - 不编排更新流程 交给 Controller
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块运行在 Qt 主线程 GUI 线程中,main() 函数在主线程执行,
#   创建的 Controller/Model/View 都属于主线程。
#   后台耗时操作由 workers 层在 QThread 子线程中执行,通过信号槽回主线程更新 UI。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging, os, sys
#   - 第三方: PySide6.QtGui, PySide6.QtWidgets
#   - 项目内:
#       updater_app.bootstrap 启动引导
#       updater_app.controller.update_controller 控制器层
#       updater_app.model.updater_model 数据模型层
#       updater_app.view.updater_window 视图层
# ==============================================================================

# 导入日志记录库,用于初始化更新助手的独立日志
import logging

# 导入操作系统接口库,用于路径操作和目录创建
import os

# 导入系统相关库,用于获取命令行参数和判断 frozen 状态
import sys

# ------------------------------------------------------------------------------
# 模块级路径修复: 兼容直接运行脚本与模块运行
# ------------------------------------------------------------------------------
# 兼容直接运行脚本 python updater_app/updater_main.py与模块运行 python -m updater_app.updater_main:
# 直接运行时 sys.path[0] 是 updater_app 目录而非项目根,绝对导入 updater_app/runtime 会失败,
# 故在任何项目内导入之前,把项目根 本文件上两级加入模块搜索路径最前面。
# 仅在非 frozen 且 __package__ 为空 即直接运行脚本时执行
if not getattr(sys, "frozen", False) and __package__ in (None, ""):
    # 计算项目根目录: 本文件 updater_main.py的上两级目录 即项目根
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 如果项目根不在 sys.path 中,则插入到最前面 最高优先级
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

# ------------------------------------------------------------------------------
# 最早期引导: 必须在 config_manager 导入之前完成
# ------------------------------------------------------------------------------
# 必须在任何 config_manager 导入之前完成: 自举推断 + 注入数据目录环境变量
# bootstrap 模块使用纯标准库实现,可安全地在其他模块之前导入
from updater_app.bootstrap import detect_app_dir, early_bootstrap

# 执行最早期引导,获取任务字典、任务文件路径、启动模式
# 此调用会解析命令行参数,并将数据目录写入 SERVICE_DATA_DIR 环境变量
# 后续导入的 config_manager 会读取该环境变量来确定数据目录
_TASK, _TASK_PATH, _LAUNCH_MODE = early_bootstrap(tuple(sys.argv[1:]))

# 导入 PySide6 的 QtGui 模块,用于 QIcon 等图形相关类
from PySide6.QtGui import QIcon

# 导入 PySide6 的 QtWidgets 模块,用于 QApplication 等 UI 组件
from PySide6.QtWidgets import QApplication

# 导入更新助手主控制器 Controller 层
from updater_app.controller.update_controller import UpdateAssistantController

# 导入更新助手数据模型 Model 层
from updater_app.model.updater_model import UpdaterModel

# 导入更新助手主窗口 View 层
from updater_app.view.updater_window import UpdaterWindow


# ==============================================================================
# 函数: _resolve_resource_path
# ==============================================================================
def _resolve_resource_path(filename: str) -> str:
    """
    解析资源文件的绝对路径,兼容开发模式与 PyInstaller onefile 解包目录。

    详细说明:
        PyInstaller onefile 模式下,资源文件被打包到 exe 中,运行时解压到
        _MEIPASS 临时目录;开发模式下,资源文件在项目根目录下。
        本函数根据运行环境自动选择正确的基准目录,拼接资源文件路径。

    参数:
        filename (str): 资源文件的相对文件名 如 "app_icon.png"

    返回值:
        str: 资源文件的绝对路径字符串

    异常:
        无显式抛出异常
    """
    # 判断是否为 PyInstaller 打包的 frozen 可执行文件
    if getattr(sys, "frozen", False):
        # frozen 模式: 优先使用 _MEIPASS onefile 模式的解压目录,
        # 没有 _MEIPASS 则使用 exe 所在目录 onedir 模式
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        # 开发模式: 使用项目根目录 本文件的上两级
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 拼接基准目录与文件名,返回完整路径
    return os.path.join(base, filename)


# ==============================================================================
# 函数: _init_logger
# ==============================================================================
def _init_logger(app_dir: str) -> None:
    """
    初始化更新助手的独立日志系统。

    详细说明:
        日志文件名为 _updater.log,默认保存在软件目录下。
        如果软件目录不可写 权限不足等,则回退到系统临时目录。
        日志采用覆盖模式 mode="w",每次启动清空旧日志,
        主程序下次启动时会归档该日志文件。

    参数:
        app_dir (str): 软件安装目录的绝对路径,日志文件默认存放位置

    返回值:
        None

    异常:
        不向外抛出异常;目录创建失败时回退到临时目录
    """
    # 构造日志文件的完整路径: 软件目录 + _updater.log
    log_path = os.path.join(app_dir, "_updater.log")
    try:
        # 确保软件目录存在 不存在则创建
        os.makedirs(app_dir, exist_ok=True)
    except OSError:
        # 目录创建失败 权限不足等,回退到系统临时目录
        import tempfile

        # 使用系统临时目录存放日志
        log_path = os.path.join(tempfile.gettempdir(), "_updater.log")
    # 创建文件日志处理器,采用覆盖模式 每次启动重写
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    # 设置日志格式: 时间 | 日志器名 | 日志级别 | 消息内容
    handler.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s"))
    # 获取根日志器
    root = logging.getLogger()
    # 清空根日志器已有的处理器 避免重复输出
    root.handlers = [handler]
    # 设置根日志器的默认日志级别为 INFO
    root.setLevel(logging.INFO)


# ==============================================================================
# 函数: main
# ==============================================================================
def main() -> int:
    """
    更新助手主入口函数,负责装配 MVC 三层并启动程序。

    详细说明:
        执行顺序:
        1. 获取任务信息和软件目录
        2. 初始化独立日志系统
        3. 创建 QApplication 实例并配置
        4. 实例化 Model 数据模型
        5. 实例化 View 主窗口
        6. 实例化 Controller 控制器,关联 Model 和 View
        7. 调用 controller.start() 启动更新流程
        8. 进入 Qt 事件循环,返回退出码

        窗口关闭由 Controller 统一接管 取消需确认;独立模式直接退出,任务模式重启旧版,
        因此设置 setQuitOnLastWindowClosed(False),禁止"关窗即退进程"。

    参数:
        无

    返回值:
        int: 程序退出码,由 app.exec() 返回
             0 表示正常退出,非 0 表示异常退出

    异常:
        无显式抛出异常;Qt 内部异常由事件循环处理
    """
    # 获取全局任务字典 模块级变量,由 early_bootstrap 赋值
    task = _TASK
    # 获取软件目录绝对路径: 任务中的 app_dir 优先,否则自动检测
    app_dir = os.path.abspath(task.get("app_dir", "") or detect_app_dir())
    # 初始化独立日志系统
    _init_logger(app_dir)
    # 创建名为 "Updater" 的日志器实例
    logger = logging.getLogger("Updater")
    # 根据启动模式生成中文描述
    mode_cn = "主程序移交" if _LAUNCH_MODE == "task" else "独立运行"
    # 记录启动日志,包含关键信息: 模式、主程序路径、旧 PID、数据目录、版本信息
    logger.info(
        f"🚀 更新助手启动(模式={mode_cn}) | 主程序={task.get('app_exe') or '未找到'} | "
        f"old_pid={task.get('old_pid')} | 数据目录={task.get('data_store_root')} | "
        f"{task.get('local_version') or '读盘'} -> {task.get('remote_version') or '待检测'}"
    )

    # 创建 Qt 应用实例,传入命令行参数
    app = QApplication(sys.argv)
    # 设置应用样式为 Fusion 跨平台统一风格
    app.setStyle("Fusion")
    # 解析应用图标路径
    icon_path = _resolve_resource_path("app_icon.png")
    # 如果图标文件存在,则设置为窗口图标
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    # 禁止"关闭最后一个窗口即退出进程",窗口关闭由 Controller 统一接管
    #  取消需确认;独立模式直接退出,任务模式重启旧版
    app.setQuitOnLastWindowClosed(False)

    # =========** [UpdaterModel][Model] ========= 实例化数据模型
    # 关联组件: Controller 通过 model 获取配置和任务信息,View 通过 model 数据初始化显示
    model = UpdaterModel(task, task_file_path=_TASK_PATH, launch_mode=_LAUNCH_MODE)

    # =========** [UpdaterWindow][View] ========= 实例化主窗口视图
    # 关联组件: Controller 连接窗口信号并调用窗口接口更新 UI
    view = UpdaterWindow(model.local_version, model.remote_version)

    # =========** [UpdateAssistantController][Controller] ========= 实例化主控制器
    # 关联组件: 持有 Model 和 View 引用,协调 workers 线程,是 MVC 的中枢
    controller = UpdateAssistantController(model, view)

    # 启动控制器,开始更新流程 回填设置表单 → 自动检测版本
    controller.start()
    # 进入 Qt 事件循环,阻塞直到程序退出,返回退出码
    return app.exec()


# ==============================================================================
# 脚本入口判断
# ==============================================================================
# 当直接运行本脚本 而非作为模块导入时,执行 main 函数
if __name__ == "__main__":
    # 调用主入口函数
    main()
