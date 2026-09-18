# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: __init__.py
# 归属: runtime/workers 后台任务线程层 —— 包初始化与工具函数
# ------------------------------------------------------------------------------
# 文件用途:
#   本文件是 runtime.workers 包的入口模块,承担两项核心职责:
#   1. 提前定义 interruptible_sleep 可中断睡眠工具函数,供所有 Worker 子模块
#      在防风控延时场景下使用,确保关窗/取消操作能立即生效而非死等 sleep 结束.
#   2. 集中导出各 Worker 子类,作为包对外的统一门面,Controller 层只需从本包
#      import 即可获得所有可用 Worker 类,无需逐个引用子模块.
# ------------------------------------------------------------------------------
# 架构定位:
#   本模块位于 runtime/workers 包的最顶层,是整个 Worker 层的入口点.
#   包内子模块(如 sign_mail_worker、batch_sign_worker 等)以
#   `from runtime.workers import interruptible_sleep` 的方式引用本模块的
#   工具函数,因此该函数必须在子模块导入之前定义完毕.
#   Worker 子类的导入统一放在文件尾部,避免与子模块之间产生循环导入.
#   本模块自身不创建任何线程,也不执行业务逻辑,纯粹是工具函数 + 导出门面.
#
#   关联组件:
#     - 上游: Controller 层
#     - 下游: .release_list_worker、.sign_mail_worker、.version_check_worker、
#             .batch_sign_worker、.file_transfer_worker(尾部延迟导入)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. interruptible_sleep(): 可被 QThread 中断请求打断的分段睡眠函数
#      - 把长睡眠时间切割成多个 tick 小段
#      - 每段睡眠前轮询 QThread.isInterruptionRequested()
#      - 收到中断请求时立即返回 False,未中断睡满则返回 True
#   2. 统一导出 Worker 类列表,维护 __all__ 公开符号清单
# ------------------------------------------------------------------------------
# 职责边界:
#   - 本模块自身不创建线程,也不执行业务逻辑
#   - interruptible_sleep 必须在子模块导入之前定义完毕
#   - Worker 子类导入放在文件尾部,避免循环导入
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块自身不创建线程.interruptible_sleep 由各 QThread 子类在其 run()
#   方法内部调用,通过轮询 QThread 的中断请求标志实现协作式取消.
#   典型调用方:SignMailWorker、BatchSignWorker 等在防风控等待时调用.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: time(提供分段睡眠用的 time.sleep)
#   - 第三方: 无直接依赖
#   - 项目内: 子模块 .release_list_worker、.sign_mail_worker、
#             .version_check_worker、.batch_sign_worker、
#             .file_transfer_worker(尾部延迟导入)
# ==============================================================================

# =========** 工具函数区 **=========
# 本区域定义所有 Worker 共用的工具函数,必须在子模块导入之前定义完毕
import time  # 导入标准库时间模块:提供分段睡眠用的 time.sleep 函数


def interruptible_sleep(thread, seconds: float, tick: float = 0.2) -> bool:
    """防风控随机延时用的可中断睡眠函数.

    把长睡眠切割成多个 tick 小段,每段睡眠前检查 QThread.isInterruptionRequested(),
    使关窗/取消操作能立即生效,而不是死等几十秒后被 terminate 强杀在 HTTP 请求中途.
    必须定义在子模块导入之前:子模块以
    `from runtime.workers import interruptible_sleep` 引用时函数已就绪.

    :param thread: 调用方 QThread 实例(通常传入 Worker 自身 self),
                   用于轮询其中断请求标志位
    :type thread: QThread
    :param seconds: 希望睡眠的总时长,单位为秒,支持浮点数精度
    :type seconds: float
    :param tick: 每个睡眠小段的时长,单位为秒,默认 0.2 秒,
                 决定取消响应的粒度,值越小响应越快但 CPU 开销略高
    :type tick: float
    :return: 返回布尔值,True 表示睡满了全部时长,
             False 表示睡眠期间收到了中断请求,提前退出
    :rtype: bool
    :raises TypeError: 如果 thread 参数不是 QThread 实例且无 isInterruptionRequested 方法
    """
    remain = float(seconds)  # 将总时长转换为浮点数,赋值给剩余秒数变量,避免整数类型导致的精度问题
    while remain > 0:  # 当剩余睡眠时长大于 0 时,继续循环分段睡眠
        if thread.isInterruptionRequested():  # 每段睡眠前检查 QThread 是否收到了关窗/取消的中断请求
            return False  # 已请求中断:立即返回 False,不再继续等待,让线程尽快退出
        time.sleep(min(tick, remain))  # 睡眠一个 tick 小段,最后一段不足 tick 时只睡剩余的量
        remain -= tick  # 扣减本段已睡眠的时长,进入下一轮循环判断
    return True  # 全部时长都睡满了,没有被中断,返回 True


# =========** Worker 组件导出区 **=========
# 以下导入必须位于 interruptible_sleep 定义之后:子模块导入时即从本包引用该函数
# 按功能分类导出,便于 Controller 层按需引用

# =========** [ReleaseList]Worker - GitHub Release 资产列表拉取 **=========
from .release_list_worker import ReleaseListWorker  # 导出 ReleaseListWorker: 拉取 GitHub 仓库最新 Release 的资产元数据列表,供文件传输 Tab 浏览使用

# =========** [SignMail]Worker - 单账号签到+邮件通知 **=========
from .sign_mail_worker import SignMailWorker  # 导出 SignMailWorker: 单账号签到并按开关发送通知邮件的后台线程

# =========** [VersionCheck]Worker - 线上版本检测 **=========
from .version_check_worker import VersionCheckWorker  # 导出 VersionCheckWorker: 拉取线上最新 Release tag 并与本地版本做语义化比较的后台线程

# =========** [BatchSign]Worker - 多账号批量签到 **=========
from .batch_sign_worker import BatchSignWorker  # 导出 BatchSignWorker: 多账号顺序批量签到,账号间防风控间隔,结束后发汇总邮件

# =========** [FileTransfer]Worker - 文件传输(下载/上传) **=========
from .file_transfer_worker import DownloadWorker, UploadWorker  # 导出 DownloadWorker(通用多线程下载)和 UploadWorker(TUS 分片上传)


# 包对外公开符号清单:约束 `from runtime.workers import *` 的导出范围
# 按字母顺序排列,便于维护和查阅
__all__ = [  # 定义包的公开 API 列表,控制星号导入的导出范围
    "BatchSignWorker",     # 多账号批量签到 Worker
    "DownloadWorker",      # 通用多线程下载 Worker
    "ReleaseListWorker",   # GitHub Release 资产列表拉取 Worker
    "SignMailWorker",      # 单账号签到+邮件通知 Worker
    "UploadWorker",        # TUS 分片上传 Worker
    "VersionCheckWorker",  # 线上版本检测 Worker
    "interruptible_sleep", # 可中断分段睡眠工具函数
]
