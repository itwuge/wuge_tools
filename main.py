# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: main.py
# 归属: 项目根目录/程序入口层 —— 签到软件唯一入口
# ------------------------------------------------------------------------------
# 文件用途:
#   整个签到软件应用的唯一入口文件;负责组装启动引导器(AppBootstrap)与应用
#   启动器(AppStarter),按顺序完成全局初始化、UI 启动、事件循环运行、
#   退出资源释放的完整生命周期管理;本文件自身不包含任何业务逻辑,仅作为
#   薄入口层协调各组件的启动顺序。
# ------------------------------------------------------------------------------
# 架构定位:
#   属于程序入口层(Entry Point);是 Python 解释器直接执行的第一个文件,
#   对上(操作系统/用户)提供可执行入口,对下调用 runtime 层的 AppBootstrap
#   完成基础设施初始化,再交给 runtime.controller 层的 AppStarter 执行业务
#   启动与 Qt 事件循环。
#
#   关联组件:
#     - 上游: 操作系统/用户
#     - 下游: runtime.app_bootstrap.AppBootstrap、
#             runtime.controller.app_starter.AppStarter
# ------------------------------------------------------------------------------
# 核心功能:
#   1. 实例化 AppBootstrap 单例并调用 init() 完成全局基础设施初始化
#   2. 初始化失败时记录错误并以非零退出码终止程序
#   3. 初始化成功后创建 AppStarter 并调用 run() 进入 Qt 主事件循环
#   4. 事件循环退出后调用 bootstrap.release() 释放全局资源
#   5. 以事件循环返回的退出码调用 sys.exit() 结束进程
# ------------------------------------------------------------------------------
# 职责边界:
#   - 仅负责启动流程编排,不包含任何业务逻辑
#   - 不直接操作 UI 控件、不直接调用 service 层
#   - 所有初始化细节委托给 AppBootstrap,所有业务启动委托给 AppStarter
# ------------------------------------------------------------------------------
# 线程模型:
#   整个 main() 运行在 Python 主线程(即 Qt UI 主线程);
#   QApplication.exec() 启动的事件循环同样运行在主线程;
#   所有网络/耗时操作必须由业务层放入 QThread 子线程,禁止阻塞主线程。
# ------------------------------------------------------------------------------
# 依赖关系:
#   - 标准库: sys(退出进程、命令行参数)
#   - 第三方: 无(间接依赖 PySide6,由 AppBootstrap 引入)
#   - 项目内: runtime.app_bootstrap.AppBootstrap、
#             runtime.controller.app_starter.AppStarter
# ==============================================================================
import sys  # 标准库:提供 sys.exit() 进程退出函数与 sys.argv 命令行参数访问

from runtime.app_bootstrap import AppBootstrap  # 项目内:全局应用启动引导器,负责日志/Qt/预检/迁移等基础设施初始化
from runtime.controller.app_starter import AppStarter  # 项目内:应用启动控制器,负责 UI 构建与 Qt 事件循环运行


def main():
    """
    程序主入口函数,编排整个应用的启动、运行与退出生命周期

    执行流程:
        1. 创建 AppBootstrap 单例并调用 init() 完成全局初始化
        2. 初始化失败:记录错误日志并以退出码 1 终止进程
        3. 初始化成功:创建 AppStarter 并调用 run() 进入 Qt 主事件循环(阻塞调用)
        4. 事件循环退出后:调用 bootstrap.release() 释放全局资源
        5. 以事件循环返回的退出码调用 sys.exit() 正常结束进程

    :return: 无返回值,函数结束时进程已通过 sys.exit() 退出
    :rtype: None
    """
    bootstrap = AppBootstrap()  # 实例化全局启动引导器(单例模式,全局唯一)
    init_ok, code, msg = bootstrap.init()  # 执行完整初始化流程:数据迁移→日志→预检→自更新清理→Qt应用创建
    if not init_ok:  # 判断初始化是否成功
        bootstrap.logger.error(f"应用启动失败 code={code}, msg={msg}")  # 初始化失败时记录错误详情(含错误码与错误信息)
        sys.exit(1)  # 以非零退出码(1)终止进程,表示启动失败

    starter = AppStarter(bootstrap)  # 创建应用启动器,注入已初始化的 bootstrap 实例以供全局访问
    exit_code = starter.run()  # 启动应用并进入 Qt 主事件循环(阻塞调用),窗口关闭后返回退出码

    bootstrap.release()  # 事件循环退出后,统一释放全局资源(Qt实例、日志句柄等)
    sys.exit(exit_code)  # 以事件循环返回的退出码正常结束进程(0=正常退出,非0=异常退出)


if __name__ == "__main__":  # 判断是否作为主脚本直接运行(而非被其他模块 import)
    main()  # 调用主入口函数启动整个应用
