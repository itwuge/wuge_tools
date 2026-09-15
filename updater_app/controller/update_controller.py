# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: update_controller.py
# 归属: updater_app/controller —— 更新助手控制器层(Controller Layer)
# ------------------------------------------------------------------------------
# 文件用途:
#   更新助手 Controller 层核心模块——连接 View 信号、调度 Worker、协调安装/重启。
#   作为 MVC 架构的中枢，协调 Model(数据)、View(窗口)与 Workers(后台线程)，
#   编排完整的软件更新流程：检测 → 下载 → 安装 → 重启。
# ------------------------------------------------------------------------------
# 架构定位:
#   更新助手 MVC 架构中的 Controller 层(控制层)。
#   持有 Model 和 View 引用，创建并管理 Worker 线程，
#   通过信号槽机制与 Worker 通信，驱动整个更新流程。
#
#   关联组件:
#     - 上游: updater_main(入口装配器，创建 Controller 实例)
#     - 下游:
#       - Model(UpdaterModel，数据读写)
#       - View(UpdaterWindow，UI 交互)
#       - Workers(AssistantUpdateWorker / InstallWorker，后台任务)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. _bind_signals: 绑定 View 信号到控制器槽函数
#   2. start: 入口，回填表单并自动发起检测
#   3. _start_check: 阶段1 - Release 检测
#   4. _on_save_settings / _on_recheck: 设置保存 / 重新检测
#   5. _start_download: 阶段2 - 下载更新包
#   6. _start_install: 阶段3 - 安装替换
#   7. _on_cancel: 取消更新
#   8. _recoverable_fail: 可恢复失败(检测/下载阶段)
#   9. _finish: 删除任务文件并硬退出
# ------------------------------------------------------------------------------
# 阶段编排:
#   start()
#     -> 回填"更新服务设置"表单(聚合 github/version/user_info/proxy 四份 JSON)
#     -> check worker 拉取 Release(更新说明)
#     -> 按配置模板匹配当前平台更新包，自动下载
#     -> install worker 等待主程序退出 → 解压 → 备份 → 复制(失败自动回滚)
#     -> 成功启动新主程序；安装失败/取消按模式重启旧主程序；最后助手硬退出(自毁)
#   检测/下载阶段失败为"可恢复"：留在窗口，用户改设置后可点"重新检测"(文件尚未改动)。
# ------------------------------------------------------------------------------
# 职责边界:
#   - 不写文件算法(installer.py)、不发网络请求(workers/service)、不操作控件细节(View 公开接口)
#   - 不直接读写 JSON 配置(交给 Model)
#   - 不直接操作 UI 控件细节(通过 View 公开接口)
# ------------------------------------------------------------------------------
# 线程模型:
#   Controller 实例运行在 Qt 主线程(GUI 线程)中。
#   耗时操作(检测/下载/安装)由 Worker 在 QThread 子线程中执行，
#   通过信号槽回主线程更新 UI。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: logging, typing
#   - 第三方: PySide6.QtCore(QObject, Slot)
#   - 项目内:
#       updater_app.installer(硬退出/启动主程序)
#       updater_app.model.updater_model(数据模型)
#       updater_app.view.updater_window(主窗口视图)
#       updater_app.workers.install_worker(安装工作线程)
#       updater_app.workers.update_worker(检测/下载工作线程)
# ==============================================================================

# 导入日志记录库，用于控制器流程诊断
import logging

# 导入类型提示库，用于 Optional 可空类型注解
from typing import Optional

# 导入 Qt 核心模块的 QObject(基类)与 Slot(槽函数装饰器)
from PySide6.QtCore import QObject, Slot

# 从安装器模块导入硬退出和启动主程序函数
from updater_app.installer import hard_exit, launch_app

# 从数据模型模块导入 UpdaterModel 类
from updater_app.model.updater_model import UpdaterModel

# 从视图模块导入 UpdaterWindow 类
from updater_app.view.updater_window import UpdaterWindow

# 从工作线程模块导入安装工作线程类
from updater_app.workers.install_worker import InstallWorker

# 从工作线程模块导入检测/下载工作线程类
from updater_app.workers.update_worker import AssistantUpdateWorker

# 创建模块级日志器实例，命名空间为 Updater.Controller
_logger = logging.getLogger("Updater.Controller")


# ==============================================================================
# 类: UpdateAssistantController
# ==============================================================================
# =========** [UpdateAssistantController][Controller] =========
# 关联组件:
#   - [UpdaterModel][Model]: Controller 持有 Model 引用，调用其读写配置
#   - [UpdaterWindow][View]: Controller 持有 View 引用，连接信号并调用接口
#   - [AssistantUpdateWorker][Worker]: Controller 创建并管理检测/下载线程
#   - [InstallWorker][Worker]: Controller 创建并管理安装线程
# ==============================================================================
class UpdateAssistantController(QObject):
    """
    更新助手主控制器类，协调 Model、View 与 Worker 完成整个更新流程。

    详细说明:
        作为 MVC 架构的中枢，持有数据模型和窗口视图的引用，
        创建并管理检测/下载/安装三个阶段的工作线程，
        通过信号槽机制接收 Worker 的进度和结果，更新 UI 并驱动流程推进。

    Attributes:
        model (UpdaterModel): 数据模型实例，持有任务与配置
        window (UpdaterWindow): 主窗口视图实例
        check_worker (Optional[AssistantUpdateWorker]): 检测阶段 Worker
        download_worker (Optional[AssistantUpdateWorker]): 下载阶段 Worker
        install_worker (Optional[InstallWorker]): 安装阶段 Worker
        _asset_list (list): Release 资产列表缓存
        _remote_tag (str): 远程版本号(检测后回填)
        _ending (bool): 是否正在退出(防止重复处理)
        _busy (bool): 是否有任务正在执行
    """

    def __init__(self, model: UpdaterModel, window: UpdaterWindow):
        """
        初始化更新助手控制器。

        详细说明:
            保存 Model 和 View 引用，初始化各阶段 Worker 为空，
            绑定 View 信号到控制器槽函数。

        参数:
            model (UpdaterModel): 数据模型实例
            window (UpdaterWindow): 主窗口视图实例

        返回值:
            None

        异常:
            无
        """
        # 调用 QObject 父类构造函数
        super().__init__()
        # 保存数据模型引用
        self.model = model
        # 保存主窗口视图引用
        self.window = window
        # 检测阶段 Worker(初始为空，检测时创建)
        self.check_worker: Optional[AssistantUpdateWorker] = None
        # 下载阶段 Worker(初始为空，下载时创建)
        self.download_worker: Optional[AssistantUpdateWorker] = None
        # 安装阶段 Worker(初始为空，安装时创建)
        self.install_worker: Optional[InstallWorker] = None
        # Release 资产列表缓存(检测后保存，下载时匹配)
        self._asset_list: list = []
        # 远程版本号(检测后回填，初始取 Model 中的值)
        self._remote_tag = model.remote_version
        # 是否正在退出标志(防止重复处理退出逻辑)
        self._ending = False
        # 是否有任务正在执行标志
        self._busy = False
        # 绑定 View 层信号到控制器槽函数
        self._bind_signals()

    def _bind_signals(self):
        """
        绑定 View 层信号到控制器槽函数。

        详细说明:
            将窗口的三个用户操作信号(保存设置、重新检测、取消)
            连接到对应的控制器处理方法。

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 保存设置信号 -> 保存设置槽函数
        self.window.sig_save_settings.connect(self._on_save_settings)
        # 重新检测信号 -> 重新检测槽函数
        self.window.sig_recheck.connect(self._on_recheck)
        # 取消信号 -> 取消槽函数
        self.window.sig_cancel.connect(self._on_cancel)

    def start(self):
        """
        控制器入口：回填更新服务设置表单并自动发起版本检测。

        详细说明:
            1. 从 Model 获取表单数据并回填到 View
            2. 显示窗口
            3. 自动启动版本检测流程

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 回填更新服务设置表单(聚合 github/version/user_info/proxy 四份 JSON)
        self.window.fill_settings_form(self.model.get_update_settings_form())
        # 显示更新助手窗口
        self.window.show()
        # 自动发起版本检测
        self._start_check()

    # ==================================================================
    # 阶段1: Release 检测
    # ==================================================================
    def _start_check(self):
        """
        启动 Release 检测 Worker(阶段1)。

        详细说明:
            创建 AssistantUpdateWorker(check 模式)，
            连接日志、进度、Release 元数据、资产列表、完成等信号，
            然后启动检测线程。

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 已有任务在执行则直接返回，避免重复启动
        if self._busy:
            return
        # 标记忙碌状态
        self._busy = True
        # 禁用设置控件(检测中不允许修改配置)
        self.window.set_controls_enabled(False)
        # 进度归零
        self.window.set_progress(0)
        # 设置状态文本
        self.window.set_status("正在检查最新版本……")
        # 追加检测开始日志
        self.window.append_log("🔍 开始检查 GitHub 最新 Release……")
        # 每次检测前以磁盘最新配置为准(用户可能在主程序改过配置)
        # 创建检测 Worker，传入全量配置和 check 模式
        self.check_worker = AssistantUpdateWorker(self.model.global_config(), mode="check")
        # 连接日志信号 -> 窗口追加日志
        self.check_worker.signal_log.connect(self.window.append_log)
        # 连接进度信号 -> 窗口设置进度
        self.check_worker.signal_progress.connect(self.window.set_progress)
        # 连接 Release 元数据信号 -> 回填版本信息
        self.check_worker.signal_release_meta.connect(self._on_release_meta)
        # 连接资产列表信号 -> 缓存资产列表
        self.check_worker.signal_asset_list.connect(self._on_asset_list)
        # 连接完成信号 -> 检测完成回调
        self.check_worker.signal_finish.connect(self._on_check_finished)
        # 启动检测线程
        self.check_worker.start()

    @Slot(dict)
    def _on_release_meta(self, meta: dict):
        """
        接收 Release 元数据并回填到 View。

        参数:
            meta (dict): Release 元数据字典，包含 tag(版本号)、
                publish_time(发布时间)、release_note(更新说明)

        返回值:
            None

        异常:
            无
        """
        # 更新远程版本号(取 tag 字段，为空则保留原值)
        self._remote_tag = str(meta.get("tag", "")) or self._remote_tag
        # 回填版本信息与更新说明到窗口
        self.window.fill_release(meta)

    @Slot(list)
    def _on_asset_list(self, asset_list: list):
        """
        接收 Release 资产列表并缓存。

        参数:
            asset_list (list): 资产列表，每个元素为包含 url、display_name 等字段的字典

        返回值:
            None

        异常:
            无
        """
        # 缓存资产列表，供后续匹配目标更新包使用
        self._asset_list = asset_list

    @Slot(dict)
    def _on_check_finished(self, res: dict):
        """
        检测完成回调：匹配目标更新包并自动下载。

        详细说明:
            1. 检测失败则走可恢复失败流程
            2. 按平台渲染目标包名，检查资产列表中是否存在
            3. 未找到目标包则走可恢复失败流程
            4. 版本相同则提示仍执行覆盖安装(修复重装)
            5. 匹配成功则自动启动下载

        参数:
            res (dict): 检测结果字典，包含 ok(是否成功)、msg(消息)等字段

        返回值:
            None

        异常:
            无
        """
        # 解除忙碌状态
        self._busy = False
        # 恢复控件可用
        self.window.set_controls_enabled(True)
        # 正在退出则忽略后续处理
        if self._ending:
            return
        # 检测失败，走可恢复失败流程
        if not res.get("ok"):
            self._recoverable_fail(res.get("msg", "版本检查失败"))
            return
        # 按当前平台渲染目标更新包名
        target_name = self.model.target_package_name(self._remote_tag)
        # 检查资产列表中是否包含目标包(同时匹配 display_name 和 original_name，提高容错性)
        # display_name 应为实际文件名(service 层已修正)，original_name 作为兜底
        matched = any(
            item.get("display_name") == target_name or item.get("original_name") == target_name
            for item in self._asset_list
        )
        # 未找到目标更新包，走可恢复失败流程(附带资产列表便于排查)
        if not matched:
            # 收集所有资产名用于日志，方便排查模板与实际资产名不匹配的问题
            asset_names = [item.get("display_name") or item.get("original_name") for item in self._asset_list]
            self._recoverable_fail(
                f"发布资产中未找到当前平台更新包:{target_name}\n"
                f"可用资产:{asset_names}"
            )
            return
        # 独立模式双击助手时可能本来就是最新版：提示后仍执行覆盖安装(可作修复重装)
        # 本地版本号(转小写比较)
        local_v = (self.model.local_version or "").lower()
        # 远程版本号(转小写比较)
        remote_v = (self._remote_tag or "").lower()
        # 版本相同，提示仍执行覆盖安装
        if local_v and remote_v and local_v == remote_v:
            self.window.append_log(f"ℹ️ 当前已是最新版本 {self._remote_tag},仍执行覆盖安装(可用于修复损坏文件)")
        # 追加匹配成功日志
        self.window.append_log(f"✅ 匹配到更新包:{target_name},开始自动下载")
        # 启动下载流程
        self._start_download([target_name])

    # ==================================================================
    # 更新服务设置保存 / 重新检测
    # ==================================================================
    @Slot(dict)
    def _on_save_settings(self, form: dict):
        """
        保存更新服务设置，成功后重新检测。

        详细说明:
            调用 Model 保存表单数据，校验失败则弹窗提示；
            保存成功后回填最新表单(如下载目录被转成绝对路径)并重检。

        参数:
            form (dict): 表单数据字典

        返回值:
            None

        异常:
            无
        """
        # 调用 Model 保存设置，返回 (是否成功, 消息)
        ok, msg = self.model.save_update_settings_form(form)
        # 保存失败，弹窗提示
        if not ok:
            self.window.show_error("配置校验失败", msg)
            return
        # 保存成功后磁盘配置已变更，回填表单(如下载目录被转成绝对路径)
        self.window.fill_settings_form(self.model.get_update_settings_form())
        # 弹出保存成功提示
        self.window.show_info("保存成功", f"{msg}\n将使用新配置重新检测版本。")
        # 追加保存成功日志
        self.window.append_log(f"💾 {msg},重新检测版本")
        # 重新检测版本
        self._on_recheck()

    @Slot()
    def _on_recheck(self):
        """
        重新检测：刷新磁盘配置并重启检测流程。

        详细说明:
            重新检测前从磁盘刷新配置，并同步表单
            (防止用户在主程序改过配置)，然后启动检测。

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 任务执行中不允许重新检测
        if self._busy:
            self.window.show_error("提示", "任务正在执行,请等待当前阶段结束。")
            return
        # 从磁盘刷新配置(用户可能在主程序改过配置)
        self.model.reload_config_from_disk()
        # 同步表单到最新配置
        self.window.fill_settings_form(self.model.get_update_settings_form())
        # 重新启动检测
        self._start_check()

    # ==================================================================
    # 阶段2: 下载
    # ==================================================================
    def _start_download(self, asset_names: list):
        """
        启动更新包下载 Worker(阶段2)。

        参数:
            asset_names (list): 要下载的资产名列表

        返回值:
            None

        异常:
            无
        """
        # 标记忙碌状态
        self._busy = True
        # 禁用设置控件
        self.window.set_controls_enabled(False)
        # 获取下载保存目录
        save_dir = self.model.download_dir()
        # 进度归零
        self.window.set_progress(0)
        # 设置状态文本
        self.window.set_status("正在下载更新包……")
        # 追加下载开始日志
        self.window.append_log(f"⬇️ 下载 {len(asset_names)} 个文件 -> {save_dir}")
        # 创建下载 Worker，传入全量配置和 download 模式
        self.download_worker = AssistantUpdateWorker(self.model.global_config(), mode="download")
        # 设置下载参数(资产名列表和保存目录)
        self.download_worker.set_download_param(asset_names, save_dir)
        # 连接日志信号 -> 窗口追加日志
        self.download_worker.signal_log.connect(self.window.append_log)
        # 连接进度信号 -> 窗口设置进度
        self.download_worker.signal_progress.connect(self.window.set_progress)
        # 连接完成信号 -> 下载完成回调
        self.download_worker.signal_finish.connect(self._on_download_finished)
        # 启动下载线程
        self.download_worker.start()

    @Slot(dict)
    def _on_download_finished(self, res: dict):
        """
        下载完成回调：成功则进入安装阶段。

        参数:
            res (dict): 下载结果字典，包含 ok(是否成功)、msg(消息)、
                saved_files(已下载文件路径列表)

        返回值:
            None

        异常:
            无
        """
        # 解除忙碌状态
        self._busy = False
        # 正在退出则忽略
        if self._ending:
            return
        # 获取已下载文件列表
        saved_files = res.get("saved_files", [])
        # 下载失败或无文件，走可恢复失败流程
        if not res.get("ok") or not saved_files:
            self._recoverable_fail(res.get("msg", "更新包下载失败"))
            return
        # 启动安装流程(取第一个下载的文件作为更新包)
        self._start_install(saved_files[0])

    # ==================================================================
    # 阶段3: 安装替换
    # ==================================================================
    def _start_install(self, package_path: str):
        """
        启动安装 Worker(阶段3)。

        参数:
            package_path (str): 更新包文件的绝对路径

        返回值:
            None

        异常:
            无
        """
        # 标记忙碌状态
        self._busy = True
        # 进入安装状态(禁用取消按钮和设置控件)
        self.window.set_installing(True)
        # 设置状态文本
        self.window.set_status("正在安装,请勿关闭……")
        # 追加安装开始日志
        self.window.append_log("🔧 开始安装更新,请勿关闭……")
        # 创建安装 Worker，传入安装所需的全部参数
        self.install_worker = InstallWorker(
            # 更新包路径
            package_path=package_path,
            # 软件安装目录
            app_dir=self.model.app_dir,
            # 旧主程序路径
            app_exe=self.model.app_exe,
            # 旧主程序 PID
            old_pid=self.model.old_pid,
            # 更新助手自身 exe 名(用于 pending 机制)
            self_exe_name=self.model.self_exe_name,
            # 独立模式(old_pid=0)安装阶段按此进程名等待主程序关闭
            wait_exe_name=self.model.main_exe_name,
        )
        # 连接日志信号 -> 窗口追加日志
        self.install_worker.signal_log.connect(self.window.append_log)
        # 连接完成信号 -> 安装完成回调
        self.install_worker.signal_finish.connect(self._on_install_finished)
        # 启动安装线程
        self.install_worker.start()

    @Slot(dict)
    def _on_install_finished(self, result: dict):
        """
        安装完成回调：成功则启动新主程序；失败则按模式回滚重启。

        详细说明:
            安装成功:
                1. 进度满、状态设为"更新完成"
                2. 将线上版本号写入 version.json
                3. 弹出确认对话框，用户点击确定后启动新主程序
                4. 收尾退出
            安装失败:
                1. 记录失败日志
                2. 退出安装状态
                3. 弹窗提示
                4. 任务模式重启旧主程序
                5. 收尾退出

        参数:
            result (dict): 安装结果字典，包含 ok(是否成功)、msg(消息)、
                launch_exe(应启动的主程序路径)

        返回值:
            None

        异常:
            无
        """
        # 解除忙碌状态
        self._busy = False
        # 是否安装成功
        ok = bool(result.get("ok"))
        # 要启动的主程序路径(成功为新路径，失败回退为旧路径)
        launch_exe = result.get("launch_exe") or self.model.app_exe
        # 结果消息
        msg = result.get("msg", "")
        # 安装成功
        if ok:
            # 进度设为 100%
            self.window.set_progress(100)
            # 状态设为"更新完成"
            self.window.set_status("更新完成")
            # 安装成功后立即把线上版本号写入 version.json，
            # 否则重启后版本检测仍判定有新版
            self.model.update_local_version(self._remote_tag)
            # 追加安装完成日志
            self.window.append_log(f"🎉 更新安装完成(版本 {self._remote_tag})")
            # 弹出提示框，用户点击确认后再启动主程序
            self.window.show_info(
                "更新完成",
                f"已成功更新到版本 {self._remote_tag}。\n\n点击「确定」启动新版本。",
            )
            # 启动新主程序
            launch_app(launch_exe, self.model.app_dir, self.window.append_log)
            # 收尾退出
            self._finish()
        # 安装失败
        else:
            # 追加失败日志
            self.window.append_log(f"❌ {msg}")
            # 退出安装状态(恢复控件可用)
            self.window.set_installing(False)
            # 弹窗提示失败原因
            self.window.show_error("更新失败", f"{msg}\n\n{self._exit_hint_text()}")
            # 任务模式主程序此前已退出，回滚后需重新拉起；
            # 独立模式主程序可能仍在运行，不代启动
            if self.model.should_restart_main:
                # 重启旧主程序
                launch_app(self.model.app_exe, self.model.app_dir, self.window.append_log)
            # 收尾退出
            self._finish()

    # ==================================================================
    # 取消 / 失败兜底 / 退出
    # ==================================================================
    @Slot()
    def _on_cancel(self):
        """
        下载/检测阶段允许取消(二次确认)；安装阶段窗口关闭已被 View 拦截。

        详细说明:
            1. 正在退出或安装中则直接返回
            2. 二次确认，用户取消则返回
            3. 标记退出中
            4. 请求取消检测/下载 Worker 并等待
            5. 任务模式重启旧主程序
            6. 收尾退出

        参数:
            无

        返回值:
            None

        异常:
            无
        """
        # 正在退出或安装中则不处理取消
        if self._ending or (self.install_worker is not None and self.install_worker.isRunning()):
            return
        # 二次确认，用户取消则不执行
        if not self.window.confirm_cancel():
            return
        # 标记退出中
        self._ending = True
        # 设置状态文本
        self.window.set_status("正在取消……")
        # 任务模式需要重启主程序
        if self.model.should_restart_main:
            self.window.append_log("🛑 用户取消更新,重启当前版本……")
        # 独立模式直接关闭
        else:
            self.window.append_log("🛑 用户取消更新,关闭更新助手……")
        # 遍历检测/下载 Worker，请求取消并等待
        for worker in (self.check_worker, self.download_worker):
            # Worker 存在且正在运行
            if worker is not None and worker.isRunning():
                # 请求协作式取消
                worker.request_cancel()
                # 等待最多 5 秒
                worker.wait(5000)
        # 任务模式需要重启主程序
        if self.model.should_restart_main:
            launch_app(self.model.app_exe, self.model.app_dir, self.window.append_log)
        # 收尾退出
        self._finish()

    def _recoverable_fail(self, msg: str):
        """
        检测/下载阶段的可恢复失败：留在助手窗口，不退出，用户改配置后可"重新检测"。

        详细说明:
            与安装阶段失败不同：此时主程序文件完全未改动、未回滚，
            无需退出或重启主程序。
            典型场景：仓库/API 地址/令牌配置错误、网络或代理不通、下载目录不可写——
            用户可直接在本窗口修正后点"重新检测"。

        参数:
            msg (str): 失败消息

        返回值:
            None

        异常:
            无
        """
        # 正在退出则不处理
        if self._ending:
            return
        # 解除忙碌状态
        self._busy = False
        # 记录错误日志
        _logger.error(f"❌ {msg}")
        # 恢复控件可用
        self.window.set_controls_enabled(True)
        # 设置状态文本
        self.window.set_status("待处理:可修改设置后重新检测")
        # 追加失败日志
        self.window.append_log(f"❌ {msg}")
        # 弹窗提示用户可修改设置后重新检测
        self.window.show_error(
            "更新未完成",
            f"{msg}\n\n您可以在上方修改更新设置(仓库/连接/下载目录/代理)后点\"重新检测\"。",
        )

    def _exit_hint_text(self) -> str:
        """
        失败弹窗尾部文案：按启动模式区分。

        返回值:
            str: 提示文案；任务模式返回"将重新启动当前版本。"，
                独立模式返回"更新助手将关闭,您可稍后重试。"

        异常:
            无
        """
        # 任务模式：主程序已退出，需要重启
        if self.model.should_restart_main:
            return "将重新启动当前版本。"
        # 独立模式：不代启动
        return "更新助手将关闭,您可稍后重试。"

    def _finish(self):
        """
        删除任务文件后助手硬退出(自毁)，不触发 PyInstaller 的 _MEI 清理弹窗。

        详细说明:
            1. 删除任务 JSON 文件(避免下次启动被误判为任务模式)
            2. 调用 hard_exit() 强制终止进程

        参数:
            无

        返回值:
            None(函数不会返回，进程直接终止)

        异常:
            无
        """
        # 删除任务 JSON 文件
        self.model.remove_task_file()
        # 硬退出(终止进程，避免 PyInstaller 清理弹窗)
        hard_exit()
