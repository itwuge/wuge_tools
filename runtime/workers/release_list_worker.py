# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: release_list_worker.py
# 归属: runtime/workers 后台任务线程层 —— GitHub Release 资产列表 Worker
# ------------------------------------------------------------------------------
# 文件用途:
#   Release 资产列表后台线程实现模块.拉取指定 GitHub 仓库最新 Release 的
#   资产元数据列表,仅做通用资产浏览,不做版本比较、不做任何下载/更新动作
#   (与自更新业务完全隔离).
# ------------------------------------------------------------------------------
# 架构定位:
#   本模块是 QThread 工作线程,通过 Signal 向 controller/view 发送事件,
#   不直接操作 UI 控件.只调用 service.github_update 业务层,不写业务算法、
#   不读写 json、不 import 任何 View.支持协作式取消
#   (threading.Event + QThread 中断请求双重通道).
#
#   关联组件:
#     - 上游: Controller(Release 列表浏览)
#     - 下游: runtime.model.app_config.resolve_effective_proxy、
#             runtime.workers.base_worker.BaseWorker、
#             service.github_update.ServiceGithubRelease
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 解析代理并构造 ServiceGithubRelease 拉取最新 Release 元数据
#   2. 为每个资产补齐 display_name/original_name/sha256 兼容字段
#   3. 通过 signal_asset_list/signal_finish 回传资产列表与结束状态
#   4. 取消或关窗时在请求边界静默退出,不向正在销毁的窗口发射信号
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只调用 service 业务层,不写业务算法,不读写 json,不 import 任何 View
#   - 仅做通用资产浏览,不做版本比较、不做任何下载/更新动作
#   - 检测到取消后静默退出,不发射 finish 信号(关窗时界面已销毁)
# ------------------------------------------------------------------------------
# 线程模型:
#   - signal_log(str): 继承自 BaseWorker 的日志双写信号
#   - signal_progress(int): 进度百分比,在工作线程 emit
#   - signal_asset_list(list): 资产元数据列表,在工作线程 emit
#   - signal_finish(dict): 结束状态,在工作线程 emit
#   以上信号均在工作线程发射;request_cancel 由 UI 线程调用置位取消事件.
#   Qt 自动以队列连接(QueuedConnection)跨线程投递给 UI 线程.
#   双重取消通道:threading.Event(_cancel_event) + QThread.requestInterruption().
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: threading
#   - 第三方: PySide6.QtCore.Signal
#   - 项目内:
#       runtime.model.app_config.resolve_effective_proxy
#       runtime.workers.base_worker.BaseWorker
#       run() 内延迟导入:
#           service.github_update.ServiceGithubRelease
# ==============================================================================

# ==============================================================================
# [Release 资产列表 Worker - QThread 胶水层]
# 职责: 后台线程拉取指定 GitHub 仓库最新 Release 的资产元数据列表
#       仅做通用资产浏览,不做版本比较、不做任何下载/更新动作(与自更新业务完全隔离)
# 边界: 只调用 service 业务层,不写业务算法,不读写 json,不 import 任何 View
# 信号:
#   signal_log(str)         - 继承自 BaseWorker,日志双写(落盘+UI)
#   signal_progress(int)    - 进度百分比 0~100
#   signal_asset_list(list) - 资产元数据列表 [{display_name, original_name, sha256, url}]
#   signal_finish(dict)     - 最终结果(ok/msg/tag)
# ==============================================================================
import threading  # 导入线程同步原语模块:用 Event 实现协作式取消标志

from PySide6.QtCore import Signal  # 从 PySide6.QtCore 导入 Signal:Qt 信号定义工具,用于向 UI 线程回传事件

from runtime.model.app_config import resolve_effective_proxy  # 导入实际代理解析函数
from runtime.workers.base_worker import BaseWorker  # 导入 Worker 基类:提供 QThread 基础能力与日志双写功能


# =========** [ReleaseList]Worker - GitHub Release 资产列表拉取 **=========
class ReleaseListWorker(BaseWorker):
    """通用 GitHub Release 资产列表拉取 Worker.

    供"文件传输"Tab 浏览仓库最新 Release 资产使用;只取元数据,
    版本比对/自动更新由独立更新助手 updater_app 负责,与本类无关.
    支持协作式取消,在请求边界检测到取消后静默退出.
    """

    # ------------------------------------------------------------------
    # 类属性(Qt 信号定义)
    # ------------------------------------------------------------------
    # 进度信号:载荷为 0~100 的进度百分比整数(int),UI 线程据此刷新进度条
    signal_progress = Signal(int)  # 定义进度信号,参数类型为整数,表示当前完成百分比
    # 资产列表信号:载荷为资产元数据 list,每项含 display_name/original_name/sha256/url 等字段
    signal_asset_list = Signal(list)  # 定义资产列表信号,参数类型为列表,携带资产元数据
    # 结束信号:载荷为最终结果 dict,含 ok(是否成功)/msg(说明)/tag(版本标签)
    signal_finish = Signal(dict)  # 定义结束信号,参数类型为字典,携带拉取任务的最终状态

    # ------------------------------------------------------------------
    # 实例属性(在 __init__ 中初始化)
    # ------------------------------------------------------------------
    # self.config: dict 全局配置字典,包含 github、proxy 等配置节
    # self._cancel_event: threading.Event 协作式取消事件标志,由 request_cancel 置位

    def __init__(self, config: dict, parent=None):
        """初始化 Release 资产列表拉取 Worker.

        :param config: 全局配置字典(含 github、proxy 等配置节)
        :type config: dict
        :param parent: Qt 父对象,用于线程生命周期管理,
                       通常为控制器或窗口对象
        :type parent: QObject | None
        :return: 无返回值
        :rtype: None
        """
        super().__init__(parent)  # 调用 BaseWorker 父类构造函数,初始化 QThread 并创建按类名命名的 logger
        self.config = config  # 持有全局配置字典,供 run() 方法读取使用
        # 协作式取消事件:Controller 在关窗时 set,run() 在请求边界轮询后退出
        self._cancel_event = threading.Event()  # 创建取消标志事件对象(初始未置位状态)

    def request_cancel(self):
        """请求中断任务(非阻塞);线程在当前请求边界尽快安全退出.

        通过双重通道请求取消:
        1. 设置 threading.Event 标志,供 run() 在请求边界轮询检测
        2. 同时调用 QThread.requestInterruption(),作为 Qt 原生中断通道

        :return: 无返回值
        :rtype: None
        """
        self._cancel_event.set()  # 置位取消标志,供 run() 在请求边界轮询发现
        self.requestInterruption()  # 同时请求 Qt 线程中断(双重取消通道,更可靠)

    def run(self):
        """QThread 线程入口:拉取最新 Release 资产元数据列表.

        构造 ServiceGithubRelease 并调用 get_latest_version_info() 拉取最新
        Release 元数据,为资产补齐兼容字段后通过信号回传.
        被取消时静默 return,不发射结束信号(关窗时界面已销毁).

        :return: 无返回值;资产列表经 signal_asset_list(list)、结束状态经
                 signal_finish(dict) 异步回传;被取消时静默 return 不发结束信号
        :rtype: None
        :raises Exception: 非取消类异常在方法内部捕获并转成 signal_finish 失败结果
        """
        try:  # 包裹整个拉取流程,取消与异常分别处理
            from service.github_update import ServiceGithubRelease  # 延迟导入 GitHub Release 业务封装类

            # --------------------------------------------------------------
            # 读取配置
            # --------------------------------------------------------------
            github_cfg = self.config["github"]  # 从全局配置中读取 GitHub 配置节(仓库/token/超时/镜像)
            proxies, proxy_source = resolve_effective_proxy(self.config["proxy"])  # 调用解析函数,得到实际代理与来源说明
            gh_owner = github_cfg["GITHUB_REPO_OWNER"]  # 从 GitHub 配置中取仓库归属(用户/组织名)
            gh_repo = github_cfg["GITHUB_REPO_NAME"]  # 从 GitHub 配置中取仓库名称

            # --------------------------------------------------------------
            # 记录任务开始
            # --------------------------------------------------------------
            self._logger.info(  # 落盘记录拉取开始参数
                f"📋 Release资产列表拉取开始 | 仓库={gh_owner}/{gh_repo} | "  # 目标仓库信息
                f"代理来源={proxy_source}"  # 代理来源信息
            )

            # --------------------------------------------------------------
            # 构造服务并发起请求
            # --------------------------------------------------------------
            gh_service = ServiceGithubRelease(  # 构造 GitHub Release 查询服务实例
                repo_owner=gh_owner,  # 传入仓库归属
                repo_name=gh_repo,  # 传入仓库名称
                github_token=github_cfg.get("GITHUB_TOKEN"),  # 传入可选 token,提高 API 限流阈值
                timeout=github_cfg.get("GITHUB_TIMEOUT", 15),  # 传入请求超时秒数,缺省为 15 秒
                verify_ssl=False,  # 关闭 SSL 校验(兼容代理环境)
                proxies=proxies,  # 传入代理映射(None 表示直连)
                api_base_url=github_cfg.get("GITHUB_API_BASE_URL") or None,  # 传入可选 API 镜像地址
            )
            try:  # 内层 try 保证会话一定被关闭
                info_ret = gh_service.get_latest_version_info()  # 发起网络请求,拉取最新 Release 元数据
            finally:  # 请求返回(无论成功/异常)后执行清理
                # 即使被取消/异常也保证会话释放,不能依赖 terminate 强杀
                gh_service.close()  # 关闭底层 HTTP 会话,释放连接资源

            # --------------------------------------------------------------
            # 检查业务返回
            # --------------------------------------------------------------
            if not info_ret.get("ok"):  # 判断业务标记是否失败
                raise RuntimeError(info_ret.get("error", "获取Release失败"))  # 抛错进入 except 兜底分支

            # --------------------------------------------------------------
            # 请求边界取消检查
            # --------------------------------------------------------------
            # 请求等待期间可能已被取消,直接静默退出(关窗流程不再接收 finish 信号)
            if self._cancel_event.is_set():  # 在请求边界轮询取消标志
                self._logger.warning("🛑 Release资产拉取阶段收到取消信号,任务退出")  # 仅落盘记录取消退出
                return  # 静默结束线程,不再发射任何信号(避免向已销毁窗口发信号)

            # --------------------------------------------------------------
            # 解析并补齐资产字段
            # --------------------------------------------------------------
            tag = info_ret.get("version_tag", "")  # 从返回结果中取最新 Release 的 tag,缺省为空串
            asset_list = info_ret.get("download_url_list", [])  # 从返回结果中取资产元数据原始列表
            # 兼容字段:统一补齐 display_name/label/original_name/sha256
            # 保证界面层可以统一访问这些字段,无需判断是否存在
            for item in asset_list:  # 逐项遍历资产列表,补齐界面所需的兼容字段
                item.setdefault("display_name", item.get("url", "").rsplit("/", 1)[-1])  # 缺省实际文件名取 URL 最后一段
                item.setdefault("label", item.get("display_name", ""))  # 缺省 UI 展示名与 display_name 相同(可能是中文 label)
                item.setdefault("original_name", "")  # 原始文件名字段缺省为空串
                item.setdefault("sha256", None)  # 校验值字段缺省为 None(表示未知)

            # --------------------------------------------------------------
            # 发射结果信号
            # --------------------------------------------------------------
            self._log(f"✅ 最新Release({tag or '无tag'})共{len(asset_list)}个资产")  # 双写日志提示资产数量
            self.signal_asset_list.emit(asset_list)  # 发射资产列表信号,供界面渲染
            self.signal_progress.emit(100)  # 进度置满
            self._logger.info(f"✅ Release资产列表拉取完成:tag={tag},资产{len(asset_list)}个")  # 落盘记录完成信息
            self.signal_finish.emit({  # 发射成功结束状态信号
                "ok": True,  # 拉取成功标志
                "msg": "Release资产列表加载完成",  # 结果说明文案
                "tag": tag,  # 最新 Release 版本标签
            })

        # ------------------------------------------------------------------
        # 异常兜底分支
        # ------------------------------------------------------------------
        except Exception as e:  # 捕获拉取流程中的任何异常
            # 取消路径静默退出,避免向正在销毁的窗口发射 finish 信号
            if self._cancel_event.is_set():  # 判断异常是否发生在取消之后
                self._logger.warning(f"🛑 Release资产拉取因取消退出:{e}")  # 仅落盘记录,不发信号
                return  # 静默退出线程,不发失败信号
            self._logger.error(f"❌ Release资产拉取异常:{str(e)}", exc_info=True)  # 落盘完整错误堆栈
            self._log(f"❌ 获取Release资产失败:{str(e)}")  # 双写日志提示失败原因
            self.signal_finish.emit({"ok": False, "msg": str(e)})  # 发射失败结束状态信号
