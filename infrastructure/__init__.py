# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: __init__.py
# 归属: infrastructure 基础设施层 —— 包初始化与对外导出入口
# ------------------------------------------------------------------------------
# 文件用途:
#   infrastructure 基础设施包的对外导出入口;把各同层基础组件的公开类聚合到包
#   命名空间,对上提供统一、稳定的导入路径.
# ------------------------------------------------------------------------------
# 架构定位:
#   infrastructure 包的包初始化文件;只做同层子模块的聚合再导出,自身不含任何
#   业务逻辑,不依赖 runtime/updater_app 业务层;上层统一通过
#   from infrastructure import Xxx 使用组件,禁止直接引用下划线开头的私有成员.
#
#   关联组件:
#     - 上游: runtime/service/updater_app 等业务层调用方
#     - 下游: .http_client、.http_github、.file_transfer、.http_email(同包子模块)
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 聚合导出 HTTP 客户端基类、GitHub API 客户端、文件上传下载器、邮件通知器
#   2. 通过 __all__ 显式划定对外公开 API 边界
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责符号聚合导出,不包含任何运行时代码
#   - 不做初始化操作,所有初始化逻辑由各子模块内部完成
#   - 禁止直接引用下划线开头的私有成员
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块无运行时代码,仅在 import 时执行一次符号聚合导出,不存在多线程并发问题.
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: 无
#   - 第三方: 无
#   - 项目内: .http_client、.http_github、.file_transfer、.http_email(均为同包子模块)
# ==============================================================================

# ---------------- http_client.py 对外公开导出 ----------------
# 从 http_client 子模块导入 BaseHttpClient 基类,作为基础设施层通用 HTTP 客户端的统一入口
from .http_client import BaseHttpClient  # 导出 HTTP 客户端基类:封装会话、超时、代理等通用联网能力

# ---------------- http_github.py 对外公开导出 ----------------
# 从 http_github 子模块导入 GitHubApiClient,专门用于 GitHub REST-API 元数据查询
from .http_github import GitHubApiClient  # 导出 GitHub API 客户端:供更新检查/资源拉取等场景调用

# ---------------- file_transfer.py 对外公开导出 ----------------
# 从 file_transfer 子模块导入文件下载器与上传器,覆盖 HTTP 文件传输的全部能力
from .file_transfer import FileDownloader, FileUploader  # 导出文件下载器与上传器:统一文件传输能力入口

# ---------------- http_email.py 对外公开导出 ----------------
# 从 http_email 子模块导入 EmailNotifier,提供 SMTP 纯文本邮件发送通知能力
from .http_email import EmailNotifier  # 导出邮件通知器:SMTP 纯文本邮件发送能力

# 显式声明包的公开符号表,控制 from infrastructure import * 的导出范围,形成清晰的 API 边界
__all__ = [  # 显式声明包的公开符号表:控制 from infrastructure import * 的导出范围,形成 API 边界
    # http_client —— 通用 HTTP 客户端基类,所有 HTTP 协议能力的底层基础
    "BaseHttpClient",  # 通用HTTP客户端基类:会话管理/重试/拦截器/Cookie持久化/multipart上传

    # http_github —— GitHub REST-API 元数据查询客户端,仅获取信息不下载文件
    "GitHubApiClient",  # GitHub API客户端:用户/仓库/release/asset元数据查询与标准化

    # file_transfer 文件传输 —— 大文件分片下载与 TUS 断点续传上传
    "FileDownloader",  # 文件下载器:多线程分片/断点续传/sha256校验/自动降级单线程
    "FileUploader",  # 文件上传器:TUS-v1.0协议多线程断点分片上传

    # http_email 邮件 —— SMTP 纯文本邮件发送,支持中文编码与 SSL/STARTTLS
    "EmailNotifier",  # 邮件通知器:SMTP纯文本邮件发送,内置QQ邮箱默认配置
]
