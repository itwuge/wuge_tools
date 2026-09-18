# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: version_check_worker.py
# 归属: runtime/workers 后台任务线程层 —— 线上版本检测 Worker
# ------------------------------------------------------------------------------
# 文件用途:
#   版本检测后台线程实现模块.拉取线上最新 Release 的版本号(tag),
#   与本地版本做语义化比较,产出"是否有新版本 + 资产下载地址列表",
#   供控制器决定是否提示用户更新.
# ------------------------------------------------------------------------------
# 架构定位:
#   本模块是 QThread 工作线程,通过 Signal 向 controller/view 发送事件,
#   不直接操作 UI 控件.只调用 service.github_update 业务层与 app_config 的
#   版本比较/代理解析工具,不写 UI、不直接修改配置文件.
#
#   关联组件:
#     - 上游: Controller(版本检测控制)
#     - 下游: runtime.model.app_config(semver_compare / resolve_effective_proxy)、
#             runtime.workers.base_worker.BaseWorker、
#             service.github_update.ServiceGithubRelease
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 从全局配置读取仓库归属、本地版本、GitHub token/超时/API 镜像地址
#   2. 调用 ServiceGithubRelease 拉取最新 Release 元数据
#   3. 用 semver_compare 比较线上/本地版本,发射成功或失败结果 dict
#   4. 异常统一兜底为失败结果,不向外抛出异常
# ------------------------------------------------------------------------------
# 职责边界:
#   - 只调用 service 业务层 + app_config 的版本比较工具,不写 UI,不直接改配置
#   - 不做任何下载或更新动作,仅负责版本检测与比较
#   - 异常统一兜底为失败结果,不向外抛出异常
# ------------------------------------------------------------------------------
# 线程模型:
#   - signal_result(dict): 版本检测结果,在工作线程 emit
#   - 日志沿用继承自 BaseWorker 的 signal_log 双写(落盘 app.log + 推送 UI)
#   Qt 自动以队列连接(QueuedConnection)跨线程投递给 UI 线程.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: time、urllib3(关闭 InsecureRequestWarning 告警)
#   - 第三方: PySide6.QtCore.Signal
#   - 项目内:
#       runtime.model.app_config.semver_compare / resolve_effective_proxy
#       runtime.workers.base_worker.BaseWorker
#       run() 内延迟导入:
#           service.github_update.ServiceGithubRelease
# ==============================================================================

# ==============================================================================
# [版本检测 Worker - QThread 胶水层]
# 职责: 后台线程拉取线上最新 tag 并与本地版本比较
# 边界: 只调用 service 业务层 + comm_tools 的版本比较工具,不写 UI,不直接改配置
# 信号: signal_result(dict)
# ==============================================================================

import time  # 导入标准库时间模块:用于统计版本检测耗时
import urllib3  # 导入 urllib3 HTTP 底层库:用于关闭 SSL 校验告警
from PySide6.QtCore import Signal  # 从 PySide6.QtCore 导入 Signal:Qt 信号定义工具,用于向 UI 线程回传结果

from runtime.model.app_config import semver_compare, resolve_effective_proxy  # 导入语义化版本比较函数与实际代理解析函数
from runtime.workers.base_worker import BaseWorker  # 导入 Worker 基类:提供 QThread 基础能力与日志双写功能

# 屏蔽关闭 SSL 警告(走代理时关闭 SSL 校验会产生告警)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 调用 urllib3 的禁用警告函数,关闭 verify=False 时的 InsecureRequestWarning 告警


# =========** [VersionCheck]Worker - 线上版本检测 **=========
class VersionCheckWorker(BaseWorker):
    """线上版本检测后台 Worker(QThread).

    拉取 GitHub 最新 Release tag 与本地 APP_VERSION 做语义化比较,
    比较结果与资产地址列表通过 signal_result 一次性回传控制器.
    不做任何下载或更新动作,仅负责版本检测与比较.
    """

    # ------------------------------------------------------------------
    # 类属性(Qt 信号定义)
    # ------------------------------------------------------------------
    # 结果信号:载荷为版本检测结果 dict,含 ok/has_new_version/local_version/
    # remote_version/msg/asset_list(失败时仅含 ok/has_new_version/msg)
    signal_result = Signal(dict)  # 定义结果信号,参数类型为字典,携带版本检测的最终结果

    # ------------------------------------------------------------------
    # 实例属性(在 __init__ 中初始化)
    # ------------------------------------------------------------------
    # self.config: dict 全局配置字典,包含 version、github、proxy 等配置节

    def __init__(self, config: dict, parent=None):
        """初始化版本检测 Worker.

        :param config: 全局配置字典(含 version、github、proxy 等配置节)
        :type config: dict
        :param parent: Qt 父对象,用于线程生命周期管理,
                       通常为控制器或窗口对象
        :type parent: QObject | None
        :return: 无返回值
        :rtype: None
        """
        super().__init__(parent)  # 调用 BaseWorker 父类构造函数,初始化 QThread 并创建按类名命名的 logger
        self.config = config  # 持有全局配置字典,供 run() 方法读取使用

    def _build_proxies(self, proxy_cfg: dict):
        """统一解析实际代理:自动跟随系统代理 > 手动开关;无代理返回 None.

        对 resolve_effective_proxy 的薄封装,只返回代理映射而丢弃来源说明,
        供不需要展示代理来源的内部调用使用.

        :param proxy_cfg: 代理配置节字典
        :type proxy_cfg: dict
        :return: requests 代理映射字典;无代理时返回 None
        :rtype: dict | None
        """
        proxies, _source = resolve_effective_proxy(proxy_cfg)  # 调用解析函数,来源说明在此方法内不需要,用下划线丢弃
        return proxies  # 仅返回代理映射字典

    def run(self):
        """QThread 线程入口:拉取最新 Release 并与本地版本比较.

        构造 ServiceGithubRelease 并调用 get_latest_version_info() 拉取最新
        Release 元数据,用 semver_compare 比较线上与本地版本,
        结果通过 signal_result 一次性回传.所有异常在内部捕获并转为失败结果.

        :return: 无返回值;检测结果通过 signal_result(dict) 异步回传
        :rtype: None
        :raises Exception: 拉取/比较过程的全部异常在方法内部捕获并转成失败结果信号
        """
        started_at = time.time()  # 记录检测开始时刻(普通时间戳,用于耗时统计)
        try:  # 包裹整个检测流程,异常统一转失败结果
            from service.github_update import ServiceGithubRelease  # 延迟导入 GitHub Release 业务封装类

            # --------------------------------------------------------------
            # 读取配置
            # --------------------------------------------------------------
            version_cfg = self.config["version"]  # 从全局配置中读取版本配置节(含本地 APP_VERSION)
            github_cfg = self.config["github"]  # 从全局配置中读取 GitHub 配置节(仓库/token/超时/镜像)
            proxies, proxy_source = resolve_effective_proxy(self.config["proxy"])  # 调用解析函数,得到实际代理与来源说明
            # 仓库信息统一从 github.json 读取(界面保存的也是这个文件)
            owner = github_cfg["GITHUB_REPO_OWNER"]  # 从 GitHub 配置中取仓库归属(用户/组织名)
            repo = github_cfg["GITHUB_REPO_NAME"]  # 从 GitHub 配置中取仓库名称
            local_ver = version_cfg["APP_VERSION"]  # 从版本配置中取本地应用版本号

            # --------------------------------------------------------------
            # 记录任务开始
            # --------------------------------------------------------------
            self._logger.info(  # 落盘记录检测开始参数
                f"🔍 开始线上版本检测: {owner}/{repo} | 本地版本={local_ver} | "  # 仓库与本地版本信息
                f"代理来源={proxy_source}"  # 代理来源信息
            )

            # --------------------------------------------------------------
            # 构造服务并发起请求
            # --------------------------------------------------------------
            gh_service = ServiceGithubRelease(  # 构造 GitHub Release 查询服务实例
                repo_owner=owner,  # 传入仓库归属
                repo_name=repo,  # 传入仓库名称
                github_token=github_cfg.get("GITHUB_TOKEN"),  # 传入可选 token,提高 API 限流阈值
                timeout=github_cfg.get("GITHUB_TIMEOUT", 15),  # 传入请求超时秒数,缺省为 15 秒
                verify_ssl=False,  # 关闭 SSL 校验(兼容代理环境)
                proxies=proxies,  # 传入代理映射(None 表示直连)
                api_base_url=github_cfg.get("GITHUB_API_BASE_URL") or None,  # 传入可选 API 镜像地址
            )
            ret_info = gh_service.get_latest_version_info()  # 调用拉取方法,获取最新 Release 元数据
            gh_service.close()  # 关闭底层 HTTP 会话,释放连接资源

            # --------------------------------------------------------------
            # 检查业务返回
            # --------------------------------------------------------------
            if not ret_info.get("ok"):  # 判断拉取是否失败(业务标记)
                raise RuntimeError(ret_info.get("error", "获取Release失败"))  # 抛错进入 except 兜底分支

            # --------------------------------------------------------------
            # 版本比较
            # --------------------------------------------------------------
            remote_ver = ret_info.get("version_tag", "")  # 从返回结果中取线上版本 tag,缺省为空串
            has_new = semver_compare(remote_ver, local_ver) > 0  # 调用语义化比较函数,线上大于本地即为有新版本
            cost_ms = int((time.time() - started_at) * 1000)  # 计算检测耗时,转换为毫秒整数

            # --------------------------------------------------------------
            # 记录结果
            # --------------------------------------------------------------
            if has_new:  # 判断是否存在新版本
                self._logger.info(  # 落盘记录新版本信息
                    f"🆕 检测到新版本:{local_ver} → {remote_ver} "  # 版本升级路径
                    f"| 资产{len(ret_info.get('download_url_list', []))}个 | 耗时{cost_ms}ms"  # 资产数量与耗时
                )
            else:  # 已是最新版本
                self._logger.info(f"✅ 已是最新版本({local_ver}),线上={remote_ver} | 耗时{cost_ms}ms")  # 落盘记录无新版本

            # --------------------------------------------------------------
            # 发射成功结果信号
            # --------------------------------------------------------------
            self.signal_result.emit({  # 发射成功检测结果信号
                "ok": True,  # 检测流程成功标志
                "has_new_version": has_new,  # 是否存在新版本
                "local_version": local_ver,  # 本地版本号字符串
                "remote_version": remote_ver,  # 线上版本号字符串
                "msg": "版本校验完成",  # 结果说明文案
                "asset_list": ret_info.get("download_url_list", []),  # 资产下载地址列表,缺省为空列表
            })

        # ------------------------------------------------------------------
        # 异常兜底分支
        # ------------------------------------------------------------------
        except Exception as e:  # 捕获检测异常:网络错误/解析错误/业务失败等
            self._logger.error(f"❌ 版本检测异常:{str(e)}", exc_info=True)  # 落盘完整错误堆栈
            self.signal_result.emit({  # 发射失败结果信号
                "ok": False,  # 标记流程失败
                "has_new_version": False,  # 失败时按无新版本处理(保守策略)
                "msg": f"版本检测异常:{str(e)}",  # 失败原因说明文案
            })
