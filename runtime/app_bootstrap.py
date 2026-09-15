# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: app_bootstrap.py
# 归属: runtime 运行时基础设施层 —— MVC 应用全局启动引导器
# ------------------------------------------------------------------------------
# 文件用途:
#   MVC 应用全局启动引导器;负责应用程序从启动到退出的完整生命周期管理,
#   包括数据目录迁移、全局日志初始化、全局异常钩子注册、Qt 应用实例创建、
#   启动前环境预检、自更新残留清理、以及程序退出时的统一资源释放。
# ------------------------------------------------------------------------------
# 架构定位:
#   属于 runtime 基础设施层;不属于 MVC 三层中的任何一层,而是支撑整个 APP
#   生命周期的底层基础设施;为上层(main.py / AppStarter)提供已就绪的运行
#   环境,上层无需关心日志如何初始化、Qt 如何创建等底层细节。
#
#   关联组件:
#     - 上游: main.py、runtime.controller.app_starter.AppStarter
#     - 下游: runtime.model.app_config、runtime.model.update_launcher、
#             PySide6.QtWidgets
# ------------------------------------------------------------------------------
# 核心功能:
#   【启动阶段】
#   1. _migrate_data_root: 数据目录迁移(启动最早期,将旧数据迁移到自定义位置)
#   2. init_logger: 全局日志初始化(控制台 + 文件双通道,日志文件每次启动重置)
#   3. global_exception_hook: 全局未捕获异常拦截器(主线程崩溃自动写入日志)
#   4. _resolve_resource_path: 资源文件路径解析(兼容开发模式与 PyInstaller 打包模式)
#   5. create_qt_app: 创建 QApplication Qt 核心实例并注册异常钩子
#   6. pre_check: 启动前置校验(配置、路径、权限检查占位)
#   7. init: 统一初始化入口,按顺序编排所有启动步骤
#   【退出阶段】
#   8. release: 程序退出时全局资源销毁清理
#   【辅助类】
#   9. AppBusinessError: 全局应用业务异常基类,所有业务层自定义异常均继承此类
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责基础设施初始化与生命周期管理,不包含任何业务逻辑
#   - 不操作 UI 控件、不调用 service 层
#   - 所有业务相关的启动逻辑交由 AppStarter 处理
# ------------------------------------------------------------------------------
# 线程模型:
#   所有初始化操作运行在 Python 主线程(即 Qt UI 主线程);
#   全局异常钩子仅拦截主线程未捕获异常,子线程异常需各自处理;
#   logger 为线程安全(logging 模块自带线程安全),可在任意线程调用。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging、os、sys、typing
#   - 第三方: PySide6(QtWidgets.QApplication、QtGui.QIcon)
#   - 项目内: runtime.model.app_config(migrate_data_store_if_needed、
#             resolve_log_file_early、get_data_store_root)、
#             runtime.model.update_launcher(apply_pending_and_cleanup)
# ==============================================================================
import logging  # 标准库:提供日志记录功能,支持多级别(debug/info/warning/error/critical)日志输出
import os  # 标准库:提供操作系统相关功能,用于路径拼接、目录创建、文件删除等
import sys  # 标准库:提供 Python 解释器相关功能,用于访问命令行参数、退出进程、异常钩子等
from typing import Any, Optional  # 标准库:类型注解,Any 表示任意类型,Optional 表示可空类型

from PySide6.QtWidgets import QApplication  # 第三方:PySide6 Qt 应用核心类,管理 GUI 应用的控制流与主设置


class AppBusinessError(Exception):
    """
    全局应用业务异常基类,所有业务层自定义异常均继承此类

    用于在业务逻辑中抛出带有错误码和错误信息的可识别异常,
    便于上层统一捕获、分类处理和错误展示。

    Attributes:
        code (int): 业务错误码,用于程序化判断错误类型
        message (str): 人类可读的错误描述文本,用于日志记录与用户展示
    """
    def __init__(self, code: int, message: str):
        """
        初始化业务异常实例

        :param code: 业务错误码,不同数值代表不同错误类型
        :type code: int
        :param message: 错误描述信息,支持中文
        :type message: str
        """
        self.code = code  # 保存业务错误码,供上层通过 isinstance 判断后读取
        self.message = message  # 保存错误描述信息,用于日志输出与用户提示
        super().__init__(self.message)  # 调用父类 Exception 构造函数,将 message 作为异常默认文本


class AppBootstrap:
    """
    单例启动引导器,管理 APP 完整生命周期

    采用单例模式确保全局只有一个引导器实例,负责编排从启动到退出的所有基础设施初始化步骤,
    为上层业务提供统一、就绪的运行环境。

    类属性:
        _instance (Optional["AppBootstrap"]): 单例实例引用,None 表示尚未创建

    实例属性:
        app (Optional[QApplication]): Qt 应用实例,create_qt_app() 后可用
        logger (logging.Logger): 全局日志对象,创建时即持有真实 Logger 引用
        _is_inited (bool): 是否已完成初始化标志,防止重复初始化
        _migration_error (str): 数据目录迁移失败时的错误信息,日志初始化后补记
    """
    # 类级单例实例存储:首次 __new__ 时创建,后续返回同一实例
    _instance: Optional["AppBootstrap"] = None  # 单例实例引用,类型注解使用字符串前向引用自身类

    def __new__(cls):
        """
        单例模式的 __new__ 方法:确保全局只有一个 AppBootstrap 实例

        首次调用时创建实例并存入 _instance,后续调用直接返回已存实例。

        :param cls: 类本身(AppBootstrap),由 Python 自动传入
        :type cls: type
        :return: 全局唯一的 AppBootstrap 实例
        :rtype: AppBootstrap
        """
        if cls._instance is None:  # 判断单例实例是否已创建
            cls._instance = super().__new__(cls)  # 调用父类 object.__new__ 创建新实例并存入类属性
        return cls._instance  # 返回单例实例(首次或复用)

    def __init__(self):
        """
        初始化 AppBootstrap 实例属性

        注意:由于单例模式,__init__ 可能被多次调用(每次实例化都会调用),
             因此属性初始化需具备幂等性,重复赋值不影响正确性。
        """
        self.app: Optional[QApplication] = None  # Qt 应用实例初始化为 None,由 create_qt_app() 实际创建
        # 创建时即持有真实 Logger,避免外部访问时类型为 Optional[Logger];
        # init_logger() 再为其绑定 basicConfig 的控制台/文件处理器
        self.logger: logging.Logger = logging.getLogger("MVC-APP")  # 获取名为 "MVC-APP" 的日志器实例(此时尚未绑定输出 handler)
        self._is_inited: bool = False  # 初始化完成标志,防止重复执行 init()
        self._migration_error: str = ""  # 数据迁移错误暂存:迁移在日志初始化前执行,失败信息先暂存,日志就绪后补记

    def _migrate_data_root(self) -> Optional[str]:
        """
        启动最早期:按指针把旧数据目录迁移到自定义位置;任何异常都不阻塞启动

        此方法在日志初始化之前执行,因此迁移失败时仅将错误信息暂存到 _migration_error,
        待 init_logger() 完成后再补记到日志文件,同时直接打印到 stderr 确保开发者可见。

        :return: 迁移成功返回旧目录路径字符串,未迁移返回 None
        :rtype: Optional[str]
        """
        try:  # 尝试执行数据目录迁移
            from runtime.model.app_config import migrate_data_store_if_needed  # 延迟导入:避免循环依赖,且迁移模块仅此处使用
            return migrate_data_store_if_needed()  # 调用迁移函数,返回旧目录路径或 None
        except Exception as e:  # 捕获迁移过程中的任何异常
            # 此时 logger 尚未初始化,先暂存错误,init_logger 后补记
            self._migration_error = str(e)  # 将异常信息字符串暂存到实例属性,待日志初始化后补记
            print(f"[启动迁移] 数据目录迁移失败(不影响启动):{e}", file=sys.stderr)  # 同时打印到标准错误流,确保开发者可见
            return None  # 返回 None 表示迁移未成功(但不阻塞启动流程)

    def init_logger(self) -> None:
        """
        初始化全局日志,同时输出控制台+文件

        日志文件路径取界面"路径配置-日志目录"的配置(user_info.json:log_dir,文件名固定 app.log),
        此处早于 MainModel 创建,用 resolve_log_file_early() 独立轻量解析;
        相对路径锚定程序目录,失败回退 data_store/logs/app.log。

        日志策略:每次启动先删除旧日志再重建,日志只保留本次运行内容,避免历史堆积。

        :return: 无返回值
        :rtype: None
        """
        from runtime.model.app_config import resolve_log_file_early  # 延迟导入:避免循环依赖,仅此处需要日志路径解析
        log_path = resolve_log_file_early()  # 解析日志文件完整路径(支持配置自定义目录,失败回退默认路径)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)  # 确保日志文件所在目录存在,不存在则递归创建

        # 每次启动先删除旧日志再重建:日志只保留本次运行内容,避免历史堆积
        # (FileHandler 同时用 mode='w',即使文件被占用删除失败也会被截断清空)
        if os.path.exists(log_path):  # 检查旧日志文件是否存在
            try:  # 尝试删除旧日志文件
                os.remove(log_path)  # 删除旧日志文件
            except OSError:  # 捕获删除失败异常(文件被占用、权限不足等)
                pass  # 删除失败不处理,后续 FileHandler mode='w' 会截断清空

        log_format = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"  # 定义日志输出格式:时间 | 日志器名 | 级别 | 消息
        logging.basicConfig(  # 配置全局日志基础设置
            level=logging.INFO,  # 设置全局日志级别为 INFO,DEBUG 及以下级别不输出
            format=log_format,  # 设置日志输出格式
            handlers=[  # 设置日志输出处理器列表(同时输出到文件和控制台)
                logging.FileHandler(log_path, mode="w", encoding="utf-8"),  # 文件处理器:写入日志文件,mode='w' 每次启动覆盖
                logging.StreamHandler(sys.stdout)  # 控制台处理器:输出到标准输出流
            ]
        )
        self.logger = logging.getLogger("MVC-APP")  # 重新获取日志器(此时已绑定 handler),确保实例引用最新配置
        self.logger.info(f"✅ 全局日志模块初始化完成,日志文件:{log_path}")  # 记录日志初始化完成信息
        self.logger.info(  # 记录程序启动环境信息(Python 版本、平台、工作目录)
            f"🚀 程序启动:Python {sys.version.split()[0]} | 平台={sys.platform} | "  # 第一行:Python 版本与操作系统平台
            f"工作目录={os.getcwd()}"  # 第二行:当前工作目录路径
        )

    def global_exception_hook(self, exc_type, exc_value, exc_traceback):
        """
        主线程全局未捕获异常拦截器

        当主线程中发生未被任何 try/except 捕获的异常时,Python 会调用 sys.excepthook,
        本方法替换默认钩子,将异常信息以 CRITICAL 级别写入日志文件,便于事后排查。
        KeyboardInterrupt(Ctrl+C) 特殊处理:交给默认钩子处理,避免干扰正常中断退出。

        :param exc_type: 异常类型类对象(如 ValueError、TypeError 等)
        :type exc_type: type
        :param exc_value: 异常实例对象,包含异常具体信息
        :type exc_value: BaseException
        :param exc_traceback: 异常回溯栈对象,记录异常发生的调用栈
        :type exc_traceback: traceback
        :return: 无返回值
        :rtype: None
        """
        if issubclass(exc_type, KeyboardInterrupt):  # 判断是否为键盘中断异常(Ctrl+C)
            sys.__excepthook__(exc_type, exc_value, exc_traceback)  # 键盘中断交给 Python 默认钩子处理(正常退出)
            return  # 直接返回,不记录日志

        if self.logger:  # 确保日志器已初始化(防御性检查)
            self.logger.critical("🔥【全局未捕获异常】", exc_info=(exc_type, exc_value, exc_traceback))  # 以 CRITICAL 级别记录完整异常信息(含类型、值、回溯栈)

    def _resolve_resource_path(self, filename: str) -> str:
        """
        解析资源文件路径,兼容开发模式与 PyInstaller 打包模式

        打包后数据文件解压在 sys._MEIPASS(PyInstaller 临时解压目录),
        开发模式在程序根目录(当前文件的上两级目录)。

        :param filename: 资源文件名(相对路径,如 "app_icon.png")
        :type filename: str
        :return: 资源文件的完整绝对路径
        :rtype: str
        """
        if getattr(sys, "frozen", False):  # 判断是否为 PyInstaller 打包后的运行环境(frozen 属性仅打包后存在)
            base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))  # 获取 PyInstaller 临时解压目录,失败回退到可执行文件所在目录
        else:  # 开发模式(直接用 Python 解释器运行)
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 计算项目根目录(当前文件的上两级目录)
        return os.path.join(base, filename)  # 拼接基础路径与文件名,返回完整资源路径

    def create_qt_app(self) -> QApplication:
        """
        实例化 Qt 应用,注册全局异常钩子

        创建 QApplication 实例并设置应用风格、窗口图标,同时将全局异常钩子
        注册到 sys.excepthook,确保主线程崩溃能被记录。

        :return: 已创建的 QApplication 实例
        :rtype: QApplication
        """
        self.app = QApplication(sys.argv)  # 创建 Qt 应用实例,传入命令行参数供 Qt 解析(如 -style、-platform 等)
        self.app.setStyle("Fusion")  # 设置应用 UI 风格为 Fusion(跨平台一致的现代化风格)
        # 设置应用图标(任务栏/窗口标题栏),开发与打包环境均生效
        from PySide6.QtGui import QIcon  # 延迟导入:QIcon 仅此处使用,且在 create_qt_app 中才需要
        icon_path = self._resolve_resource_path("app_icon.png")  # 解析应用图标文件路径(兼容开发与打包环境)
        if os.path.exists(icon_path):  # 检查图标文件是否存在(避免文件缺失导致启动失败)
            self.app.setWindowIcon(QIcon(icon_path))  # 设置应用窗口图标(任务栏和标题栏显示)
        sys.excepthook = self.global_exception_hook  # 注册全局未捕获异常钩子,替换 Python 默认行为
        self.logger.info("✅ QApplication 创建成功")  # 记录 Qt 应用创建成功日志
        return self.app  # 返回已创建的 Qt 应用实例

    def pre_check(self) -> tuple[bool, int, str]:
        """
        启动前置校验:配置、路径、权限检查位置

        当前为占位实现,直接返回成功;预留此方法用于后续增加配置文件加载、
        必要资源文件存在性检测、目录写入权限验证等启动前检查。

        :return: 三元组 (是否成功, 错误码, 错误信息)
        :rtype: tuple[bool, int, str]
        """
        try:  # 尝试执行前置校验
            # 在这里增加配置加载、资源文件检测
            return True, 0, "ok"  # 当前直接返回成功(占位实现)
        except AppBusinessError as e:  # 捕获业务异常(预定义的错误码异常)
            self.logger.error(f"❌ 前置校验失败 code:{e.code} msg:{e.message}")  # 记录业务校验失败详情
            return False, e.code, e.message  # 返回失败结果,携带业务错误码与信息
        except Exception as e:  # 捕获其他未知异常
            self.logger.error(f"❌ 前置校验未知异常: {str(e)}", exc_info=True)  # 记录未知异常详情(含完整回溯栈)
            return False, 9999, f"前置校验异常:{str(e)}"  # 返回失败结果,使用通用错误码 9999

    def init(self) -> tuple[bool, int, str]:
        """
        统一初始化入口,完整启动流程

        按顺序执行:数据目录迁移 → 日志初始化 → 前置校验 → 自更新残留清理 → 创建 Qt 应用
        任何关键步骤失败都会提前返回失败结果,非关键步骤(迁移、清理)失败不阻塞启动。

        :return: 三元组 (是否成功, 错误码, 错误信息)
        :rtype: tuple[bool, int, str]
        """
        if self._is_inited:  # 判断是否已完成初始化(防止重复初始化)
            return True, 0, "已初始化"  # 已初始化直接返回成功,避免重复执行
        try:  # 包裹整个初始化流程,捕获任何意外异常
            # 数据目录迁移必须在日志 handler 绑定前:旧日志文件未被占用,且日志直接落新目录
            migrated_from = self._migrate_data_root()  # 执行数据目录迁移(最早执行,确保日志文件落在新目录)
            self.init_logger()  # 初始化全局日志系统(控制台+文件双通道)
            if migrated_from:  # 判断是否发生了迁移(非 None 表示迁移成功)
                from runtime.model.app_config import get_data_store_root  # 延迟导入:仅迁移成功时需要获取新目录路径
                self.logger.info(  # 记录数据目录迁移成功日志
                    f"📦 数据目录已迁移: {migrated_from} -> {get_data_store_root()}"  # 显示旧路径到新路径的迁移
                )
            if self._migration_error:  # 判断是否有暂存的迁移错误(迁移失败但未阻塞启动)
                self.logger.error(  # 补记迁移失败错误到日志文件(日志系统已就绪)
                    f"⚠ 数据目录迁移失败,已使用新空目录启动,请手动检查旧数据: {self._migration_error}"  # 提示用户手动检查旧数据
                )
            ok, code, msg = self.pre_check()  # 执行启动前置校验(配置、路径、权限等)
            if not ok:  # 判断前置校验是否失败
                return False, code, msg  # 前置校验失败,直接返回错误结果
            # 自更新收尾:应用更新助手的延迟自升级,并清理上轮安装残留/旧 bat 脚本
            # 纯文件操作且不依赖 Qt,放在创建 QApplication 之前,任何失败都不阻塞启动
            try:  # 尝试执行自更新残留清理
                from runtime.model.update_launcher import apply_pending_and_cleanup  # 延迟导入:自更新模块仅此处使用
                handled = apply_pending_and_cleanup(self.logger)  # 执行待处理更新与清理,返回已处理项列表
                if handled:  # 判断是否有实际处理的项
                    self.logger.info(f"🧹 自更新残留清理完成:{', '.join(handled)}")  # 记录清理完成的项目
            except Exception as e:  # 捕获清理过程中的任何异常(非关键步骤,失败不阻塞)
                self.logger.warning(f"⚠ 自更新残留清理异常(不影响启动):{e}")  # 仅记录警告,不中断启动
            self.create_qt_app()  # 创建 Qt 应用实例(UI 基础设施)
            self._is_inited = True  # 标记初始化完成,防止重复执行
            self.logger.info("🎉 AppBootstrap 全部初始化完成")  # 记录全部初始化完成日志
            return True, 0, "ok"  # 返回初始化成功结果
        except Exception as e:  # 捕获初始化流程中的任何未预期异常
            if self.logger:  # 防御:确保日志器已存在(可能在日志初始化前就异常)
                self.logger.error(f"❌ AppBootstrap初始化失败: {str(e)}", exc_info=True)  # 记录初始化失败详情(含完整回溯栈)
            return False, 9999, f"应用初始化失败:{str(e)}"  # 返回失败结果,使用通用错误码 9999

    def release(self):
        """
        程序退出时,全局资源销毁清理

        释放 Qt 应用实例引用、重置初始化标志,确保资源正确回收。
        日志对象保持可用,以便记录退出日志。

        :return: 无返回值
        :rtype: None
        """
        if self.logger:  # 确保日志器可用(防御性检查)
            self.logger.info("📤 开始释放全局资源")  # 记录开始释放资源日志
        self.app = None  # 释放 Qt 应用实例引用(帮助 GC 回收,Qt 内部会自行清理)
        self._is_inited = False  # 重置初始化标志,允许下次重新初始化(理论上不会)
        if self.logger:  # 确保日志器可用
            self.logger.info("✅ 全局资源释放完毕")  # 记录资源释放完成日志
            self.logger.info("🛑 程序退出")  # 记录程序最终退出日志
