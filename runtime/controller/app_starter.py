# -*- coding: utf-8 -*-
# ==============================================================================
# 文件名称: app_starter.py
# 文件路径: runtime/controller/app_starter.py
# ------------------------------------------------------------------------------
# 文件用途:
#   应用启动流程控制器(Application Starter),负责编排整个应用从启动到
#   主窗口显示、再到后台静默版本检测的完整启动流程.本文件是连接
#   app_bootstrap(应用引导层)与主窗口MVC三层之间的桥梁.
# ------------------------------------------------------------------------------
# 架构定位:
#   位于应用架构的[启动编排层],隶属于Controller层.
#   其上层是 app_bootstrap(负责Qt应用初始化、日志、路径等基础环境搭建),
#   其下层是主窗口MVC三层(MainModel / MainWindow / MainController).
#
#   启动流程调用链:
#   app_bootstrap.main() → AppStarter(bootstrap).run() → 主窗口MVC组装 → Qt事件循环
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 加载配置与本地版本号(通过Model层)
#   2. 组装主窗口MVC三层(Model + View + Controller)并立即显示主窗口
#      目的: 避免启动期网络检测阻塞,让用户先看到主界面而非无响应状态
#   3. 主窗口就绪后,由MainController在后台静默检测新版本(延迟500ms执行,
#      留出窗口绘制时间,避免弹窗抢在绘制完成之前出现)
#   4. 发现新版本时,由MainController弹出确认框;用户确认后启动独立的
#      "更新助手"(update.exe),主程序随即退出释放文件锁,
#      下载/解压/替换/重启全部由更新助手完成(全程无cmd脚本,无黑窗)
# ------------------------------------------------------------------------------
# 边界职责:
#   - 仅负责启动流程的编排,不包含具体业务逻辑
#   - 不直接操作UI控件(UI操作通过View层接口完成)
#   - 不直接读写配置文件(数据读写通过Model层完成)
#   - 不执行网络请求(网络操作通过Worker线程完成)
#   - 启动流程结束后,控制权完全交给Qt事件循环和MainController
# ------------------------------------------------------------------------------
# MVC定位:
#   Controller层 —— 启动流程控制器.
#   负责在应用启动时完成Model加载、View创建、Controller实例化的
#   完整MVC组装工作,并处理启动期的异常与退出码.
# ==============================================================================

# 导入 PySide6 的 QTimer 类 —— 用于实现单次延迟定时器,在主窗口显示后
# 延迟一段时间再发起静默版本检测,避免阻塞UI绘制
from PySide6.QtCore import QTimer

# 导入 AppBootstrap 类 —— 应用引导器,提供Qt应用实例、日志、路径等全局资源
# 导入 AppBusinessError 异常类 —— 业务异常基类,用于区分业务异常与系统异常
from runtime.app_bootstrap import AppBootstrap, AppBusinessError
# 导入 MainController 类 —— 主窗口控制器,MVC中的Controller层核心
from runtime.controller.main_controller import MainController
# 导入 MainModel 类 —— 主窗口数据模型,MVC中的Model层,负责数据读写
from runtime.model.main_model import MainModel
# 导入 MainWindow 类 —— 主窗口视图,MVC中的View层,负责界面渲染
from runtime.view.main_window import MainWindow


# =========** AppStarter Controller =========
class AppStarter:
    """应用启动流程控制器类.

    本类负责编排整个应用的启动流程,包括:
    - 加载配置与版本信息(Model层)
    - 创建并显示主窗口(View层)
    - 组装主窗口控制器(Controller层)
    - 触发后台静默版本检测
    - 启动Qt事件循环并处理退出码

    Attributes:
        bootstrap (AppBootstrap): 全局应用引导器实例,提供QApplication、
            logger、路径配置等全局资源.由上层传入,本类不负责创建.
        model (MainModel): 主窗口数据模型实例,负责配置加载、账号管理、
            版本读取等数据操作.在run()方法中创建.
        main_view (MainWindow | None): 主窗口视图实例,负责界面渲染与
            用户交互信号发出.在_show_main_window()中创建,初始为None.
        main_controller (MainController | None): 主窗口控制器实例,
            负责连接View信号与业务逻辑.在_show_main_window()中创建,
            初始为None.

    Raises:
        AppBusinessError: 当启动流程中发生业务逻辑错误时抛出,
            如配置文件格式错误、数据目录不可用等.
        Exception: 当发生未预期的系统错误时抛出,由run()方法捕获并记录日志.
    """

    # 类常量:主窗口显示后,延迟多少毫秒发起静默版本检测
    # 设置500ms的目的:留出窗口绘制时间,避免版本检测的弹窗抢在窗口绘制完成之前出现
    STARTUP_CHECK_DELAY_MS = 500

    def __init__(self, bootstrap: AppBootstrap):
        """AppStarter 构造函数.

        初始化启动控制器,保存全局引导器引用,并声明各MVC组件的属性占位.
        注意:构造函数仅做初始化,不执行任何耗时操作(如加载配置、创建窗口等),
        实际的启动逻辑在 run() 方法中执行.

        Args:
            bootstrap (AppBootstrap): 应用引导器实例,必须包含以下可用资源:
                - app: QApplication 实例(可能为None,在run()中会检查)
                - logger: 日志记录器实例
                - 各种路径配置信息

        Returns:
            None

        Raises:
            无.构造函数不抛出异常.
        """
        # 保存全局引导器引用,后续通过它访问QApplication、logger等全局资源
        self.bootstrap = bootstrap
        # 声明主窗口数据模型属性,类型注解为MainModel
        # 实际实例在 run() 方法中创建,此处仅做类型声明
        self.model: MainModel
        # 声明主窗口视图属性,初始为None,在 _show_main_window() 中创建
        # 使用 | None 联合类型注解,表示该属性可能为None(未创建时)
        self.main_view: MainWindow | None = None
        # 声明主窗口控制器属性,初始为None,在 _show_main_window() 中创建
        self.main_controller: MainController | None = None

    def run(self) -> int:
        """执行完整的应用启动流程,并返回Qt事件循环的退出码.

        本方法是AppStarter的核心入口,按以下顺序执行启动流程:
        1. 通过Model层加载配置与本地版本号
        2. 组装并显示主窗口(MVC三层装配)
        3. 根据配置决定是否启动后台静默版本检测
        4. 启动Qt事件循环,阻塞直到主窗口关闭
        5. 捕获并处理启动过程中的各类异常

        Args:
            无参数.

        Returns:
            int: Qt事件循环的退出码.
                - 0: 正常退出
                - 1: 异常退出(业务异常或未知异常)

        Raises:
            本方法内部捕获所有异常,不向外抛出.
            异常通过日志记录并将退出码设为1返回.
        """
        # 从引导器中获取日志记录器,用于记录启动过程中的各种信息
        logger = self.bootstrap.logger
        # 初始化退出码为0(默认正常退出),后续异常时会被修改为1
        exit_code = 0
        # 使用 try-except 块包裹整个启动流程,确保任何异常都能被捕获并记录
        try:
            # ---- Model 层:加载配置 ----
            # 创建主窗口数据模型实例,负责所有数据的读写操作
            # Model层在构造时会加载配置文件、账号数据等
            self.model = MainModel()
            # 从Model层获取本地版本号,用于日志记录和后续版本比较
            local_version = self.model.get_local_version()
            # 记录启动日志,包含本地版本号,便于排查问题
            logger.info(f"🚀 启动主程序,本地版本={local_version}")

            # ---- 先显示主窗口,更新检测放到窗口就绪后静默进行 ----
            # 调用内部方法 _show_main_window() 完成MVC组装并显示主窗口
            # 返回值是主窗口控制器实例(非Optional类型,便于后续直接调用)
            main_controller = self._show_main_window()
            # 检查是否启用了启动时自动检查更新的开关
            # 如果启用,则在主窗口显示后延迟一段时间发起静默版本检测
            if self.model.is_auto_check_update_enabled():
                # 使用 QTimer.singleShot 创建单次定时器
                # 参数1: 延迟毫秒数(STARTUP_CHECK_DELAY_MS = 500ms)
                # 参数2: 定时器触发后要调用的槽函数
                # 目的: 留出窗口绘制时间,避免弹窗抢在绘制完成前出现
                QTimer.singleShot(
                    self.STARTUP_CHECK_DELAY_MS,
                    main_controller.startup_auto_check_update,
                )
            else:
                # 如果自动检查更新开关关闭,则跳过静默版本检测
                # 用户仍可通过手动点击"检查更新"按钮来检测版本
                logger.info("ℹ️ 已关闭启动自动检查更新,跳过静默版本检测")

            # 主窗口即程序唯一生命周期窗口,关闭主窗口即退出整个程序
            # 检查QApplication实例是否存在(理论上不应为None,做防御性检查)
            if self.bootstrap.app is not None:
                # 设置当最后一个窗口关闭时自动退出Qt事件循环
                # 这确保了关闭主窗口后程序会正常退出
                self.bootstrap.app.setQuitOnLastWindowClosed(True)
                # 启动Qt事件循环,程序进入事件处理状态,阻塞在此直到退出
                # exec() 返回退出码,0表示正常退出
                exit_code = self.bootstrap.app.exec()
            else:
                # 如果QApplication实例为空,记录错误日志
                # 这是极端异常情况,理论上不会发生
                logger.error("❌ QApplication 实例为空,无法启动事件循环")
                # 设置退出码为1,表示异常退出
                exit_code = 1
            # 记录事件循环退出日志,包含退出码,便于排查问题
            logger.info(f"🔚 Qt事件循环退出,exit_code={exit_code}")

        # 捕获业务异常(AppBusinessError及其子类)
        # 业务异常是预期内的错误,如配置格式错误、数据目录不可用等
        except AppBusinessError as e:
            # 记录业务异常的错误码和错误消息,同时打印异常堆栈
            logger.error(f"❌ 业务异常 code:{e.code} msg:{e.message}", exc_info=True)
            # 设置退出码为1,表示异常退出
            exit_code = 1
        # 捕获所有其他未预期的异常(系统级异常)
        except Exception as e:
            # 记录未知异常的错误消息,同时打印异常堆栈
            logger.error(f"❌ 启动流程未知异常:{str(e)}", exc_info=True)
            # 设置退出码为1,表示异常退出
            exit_code = 1
        # 返回最终的退出码给调用方
        return exit_code

    # ======================================================================
    # 主窗口
    # ======================================================================
    def _show_main_window(self) -> MainController:
        """创建并显示主窗口,完成MVC三层的组装.

        本方法按以下步骤执行:
        1. 创建MainWindow(View层),并传入本地版本号用于界面显示
        2. 创建MainController(Controller层),将Model和View关联起来
        3. 显示主窗口
        4. 记录组装完成日志

        Args:
            无参数.所需数据(model、local_version)通过实例属性获取.

        Returns:
            MainController: 已完成组装的主窗口控制器实例.
                返回类型为非Optional的MainController,避免调用方使用
                self.main_controller 的 Optional 声明引发静态检查告警.

        Raises:
            无显式抛出.如果View或Controller创建失败,会由底层异常向上传递,
            最终在run()的try-except中被捕获.
        """
        # 创建主窗口视图实例(View层),传入本地版本号用于窗口标题或关于页显示
        view = MainWindow(local_version=self.model.get_local_version())
        # 将视图实例保存到实例属性中,供其他方法访问
        self.main_view = view
        # 创建主窗口控制器实例(Controller层),将Model和View关联起来
        # Controller在构造时会完成信号绑定和初始数据加载
        controller = MainController(model=self.model, view=view)
        # 将控制器实例保存到实例属性中,供其他方法访问
        self.main_controller = controller
        # 显示主窗口,使窗口在屏幕上可见
        # 注意:show()是非阻塞的,窗口显示后立即返回,后续由事件循环驱动
        view.show()
        # 记录MVC组装完成的日志,标记主窗口已就绪
        self.bootstrap.logger.info("🟢 主窗口加载完成,MVC组装就绪")
        # 返回控制器实例,供调用方直接使用(非Optional类型,避免静态检查告警)
        return controller
