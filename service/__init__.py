# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: __init__.py
# 归属: service 业务服务层 —— 包初始化与对外导出入口
# ------------------------------------------------------------------------------
# 文件用途:
#   service 业务层包的对外导出入口;把各同层业务服务类聚合到包命名空间,
#   对上提供统一、稳定的导入路径.
# ------------------------------------------------------------------------------
# 架构定位:
#   service 包的包初始化文件;只做同层子模块的聚合再导出,自身不含任何业务逻辑;
#   上层(controller/worker)统一通过 from service import XxxService 使用业务服务,
#   禁止直接引用下划线开头的私有成员.
#
#   关联组件:
#     - 上游: controller/worker 业务调用方
#     - 下游: .file_downloade、.send_email、.github_update、.run_ba(同包子模块)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 聚合导出文件下载上传服务(ServiceFileDownloader / ServiceFileUploader)
#   2. 聚合导出邮件发送服务(ServiceEmailSender)
#   3. 聚合导出 GitHub Release 业务服务(ServiceGithubRelease)
#   4. 聚合导出签到网站 API 业务服务(ServiceApiService)
#   5. 通过 __all__ 显式划定对外公开 API 边界
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责符号聚合导出,不包含任何运行时代码
#   - 只导出业务公开 API,下划线开头私有实现不对外暴露
#   - __all__ 显式划定对外公开 API 边界
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块无运行时代码,仅在 import 时执行一次符号聚合导出,不存在多线程并发问题.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: 无
#   - 第三方: 无
#   - 项目内: .file_downloade、.send_email、.github_update、.run_ba(均为同包子模块)
# ==============================================================================
from typing import Any  # 标准库:Any 任意类型,用于延迟导出占位标注

# ---------------- file_downloade.py 对外公开导出 ----------------
# 从 file_downloade 子模块导入文件下载与上传业务服务类,封装底层 FileDownloader/FileUploader
from .file_downloade import ServiceFileDownloader, ServiceFileUploader  # 导出文件下载器与上传器业务服务

# ---------------- send_email.py 对外公开导出 ----------------
# 从 send_email 子模块导入邮件发送业务服务类,封装底层 EmailNotifier
from .send_email import ServiceEmailSender  # 导出邮件发送业务服务

# ---------------- github_update.py 对外公开导出 ----------------
# 从 github_update 子模块导入 GitHub Release 业务服务类,封装底层 GitHubApiClient
from .github_update import ServiceGithubRelease  # 导出 GitHub Release 业务服务

# ---------------- run_ba.py 延迟导出 ----------------
# run_ba 依赖 runtime.model(签到业务专用),更新助手打包时不包含 runtime 包,
# 若在包初始化时直接导入会导致 ModuleNotFoundError: No module named 'runtime'.
# 改为 __getattr__ 延迟导入:仅在真正访问 service.ServiceApiService 时才加载 run_ba.
# 主程序正常 import service.ServiceApiService 仍可使用,只是首次访问时才触发导入.

def __getattr__(name: str):
    """包级延迟导入:首次访问 ServiceApiService 时才导入 run_ba 模块."""
    if name == "ServiceApiService":
        from .run_ba import ServiceApiService as _cls
        return _cls
    raise AttributeError(f"module 'service' has no attribute '{name}'")


# 类型占位声明:ServiceApiService 由 __getattr__ 延迟导入,模块内无静态定义,
# 该注解仅供类型检查器识别动态导出,不创建运行时属性,不改变延迟导入行为
ServiceApiService: Any  # 动态延迟导出占位类型标注:消除 Pylance 对 __all__ 未定义名称的警告

# 显式声明包的公开符号表,控制 from service import * 的导出范围,形成清晰的 API 边界
__all__ = [  # 显式声明包的公开符号表:控制 from service import * 的导出范围,形成 API 边界
    # file_downloade —— 文件下载与上传业务服务,封装底层传输能力
    "ServiceFileDownloader",  # 文件下载业务服务:封装 FileDownloader,透传进度回调与日志
    "ServiceFileUploader",  # 文件上传业务服务:封装 FileUploader,TUS-v1.0 断点分片上传

    # send_email —— 邮件发送业务服务,封装底层 SMTP 能力
    "ServiceEmailSender",  # 邮件发送业务服务:封装 EmailNotifier,透传日志

    # github_update —— GitHub Release 业务服务,封装底层 API 客户端
    "ServiceGithubRelease",  # GitHub Release 业务服务:获取最新版本与资产列表

    # run_ba —— 签到网站 API 业务服务,封装底层 HTTP 客户端(延迟导入)
    "ServiceApiService",  # 签到网站 API 业务服务:登录/签到/Cookie 管理
]
