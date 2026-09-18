# -*- coding: utf-8 -*-
# ==============================================================================
# [通用对话框 View 层]common_dialog.py
# ==============================================================================
# 文件用途:
#   全项目统一的消息提示框入口模块;封装 PySide6 的 QMessageBox 控件,提供信息提示、
#   警告提示、错误提示、确认对话框四类标准化弹窗接口.所有按钮强制使用中文文案,
#   不依赖操作系统语言设置,保证跨平台界面一致性.
# ------------------------------------------------------------------------------
# 架构定位 (MVC 模式):
#   - 所属层级: View 层(视图层) - 通用对话框组件
#   - 对应 Controller: 无(纯UI工具模块,被 Controller 和 View 内部直接调用)
#   - 对应 Model:      无(不涉及任何数据模型)
#   - 上游调用方:      Controller 层(业务控制器)、View 层(主窗口等视图类)
#   - 下游依赖:        仅依赖 PySide6.QtWidgets,不依赖任何业务模块
#   - 职责边界:        仅负责弹窗UI展示与用户点击结果返回,不处理任何业务逻辑
# ------------------------------------------------------------------------------
# 核心功能:
#   1. show_info()     - 信息提示框(单按钮,蓝色图标,默认"确定")
#   2. show_warning()  - 警告提示框(单按钮,黄色图标,默认"知道了")
#   3. show_error()    - 错误提示框(单按钮,红色图标,默认"知道了")
#   4. show_confirm()  - 确认对话框(双按钮,问号图标,支持危险操作红色按钮样式)
#   5. _build_box()    - 内部工具函数:统一构造消息框外观
#   6. _single()       - 内部工具函数:统一处理单按钮告知框逻辑
# ------------------------------------------------------------------------------
# MVC 定位:
#   - Model(模型层):      无(本模块不涉及数据操作)
#   - View(视图层):       本模块,负责对话框的渲染与用户交互
#   - Controller(控制层): 调用方,负责决定何时弹窗及如何处理用户选择结果
# ------------------------------------------------------------------------------
# UI 设计规范:
#   - 所有弹窗使用统一的最小宽度 320px,避免过窄影响阅读
#   - 信息提示使用蓝色 Information 图标 + "确定" 按钮
#   - 警告提示使用黄色 Warning 图标 + "知道了" 按钮
#   - 错误提示使用红色 Critical 图标 + "知道了" 按钮
#   - 确认对话框使用问号 Question 图标 + 自定义双按钮文案
#   - 危险操作(删除/中断等)的确认按钮使用红色文字与边框样式
#   - 所有按钮文案均为中文,不依赖系统语言
#   - ESC 键默认等同取消按钮,避免误触危险操作
# ------------------------------------------------------------------------------
# 线程模型:
#   本模块所有函数均为 UI 操作,必须在 Qt 主线程中调用;
#   QMessageBox.exec() 为模态阻塞调用,会阻塞主线程事件循环直到用户关闭对话框.
# ------------------------------------------------------------------------------
# 依赖:
#   - 标准库: __future__(annotations)、typing(Optional)
#   - 第三方: PySide6.QtWidgets (QMessageBox, QWidget)
#   - 项目内: 无
# ------------------------------------------------------------------------------
# 使用约定:
#   1. 仅"告知"性质的消息用单按钮弹窗:
#      - show_info    成功/常规提示   -> 按钮"确定"
#      - show_warning 警告/输入校验   -> 按钮"知道了"
#      - show_error   失败/异常       -> 按钮"知道了"
#   2. 需要用户决策的用双按钮 show_confirm:
#      - 返回 True 表示点击了确认按钮,False 表示取消/关闭对话框
#      - 按钮文案必须按场景传入,例如:
#        发现新版本 -> 确认="立即更新" 取消="暂不更新"
#        中断任务退出 -> 确认="确认退出" 取消="继续等待"
#        删除账号   -> 确认="确认删除"(danger红色) 取消="取消"
#   3. parent 参数可传 None(启动早期无主窗口时使用)
# ==============================================================================

# =====** 未来语法兼容导入 =====**
from __future__ import annotations  # PEP 563: 将类型注解延迟为字符串求值,兼容旧版本Python的类型注解写法

# =====** 标准库类型导入 =====**
from typing import Optional  # 导入 Optional 类型,用于声明可空类型参数(即可以为 None 的参数)

# =====** PySide6 GUI 组件导入 =====**
from PySide6.QtWidgets import QMessageBox, QWidget  # 导入消息框控件 QMessageBox 和父窗口基类 QWidget


# =========** CommonDialog View =========
# 对应 Controller: 无(纯UI工具模块,被各Controller直接调用)
# 对应 Model:      无(不涉及数据模型)
# 说明: 本模块为全局通用对话框视图组件,提供标准化的弹窗接口,
#       被所有 Controller 和 View 组件共享使用,不属于某个特定业务模块.
# ==============================================================================

# =====** 常量定义组件 =====**
# 默认中文按钮文案常量集合(可被调用方按具体场景覆盖)
DEFAULT_OK = "确定"          # 信息提示框默认确认按钮文案 - 用于常规成功/提示场景
DEFAULT_KNOW = "知道了"      # 警告/错误提示框默认确认按钮文案 - 用于警告/错误告知场景
DEFAULT_CONFIRM = "确认"     # 确认对话框默认确认按钮文案 - 用于一般确认场景
DEFAULT_CANCEL = "取消"      # 确认对话框默认取消按钮文案 - 用于取消/放弃场景


# =====** [消息框构造器]内部工具函数 =====**
def _build_box(
    icon: QMessageBox.Icon,
    parent: Optional[QWidget],
    title: str,
    text: str,
) -> QMessageBox:
    """
    内部工具函数:构造统一外观的 QMessageBox 消息框实例

    本函数负责创建消息框并设置基础属性(图标、标题、正文、最小宽度),
    按钮的添加与事件处理由各公开函数自行完成.

    :param icon: 消息框图标类型,取值为 QMessageBox.Icon 枚举值
                 - Information: 蓝色信息图标(常规提示)
                 - Warning:     黄色警告图标(警告提示)
                 - Critical:    红色错误图标(错误提示)
                 - Question:    问号图标(确认对话框)
    :type icon: QMessageBox.Icon
    :param parent: 父窗口控件,None 表示无父窗口(应用启动早期无主窗口时使用)
    :type parent: Optional[QWidget]
    :param title: 对话框标题栏显示的文字
    :type title: str
    :param text: 对话框正文显示的内容
    :type text: str
    :return: 配置好图标、标题、正文、最小宽度的 QMessageBox 实例
    :rtype: QMessageBox
    """
    box = QMessageBox(parent)  # 创建 QMessageBox 实例,绑定父窗口(用于窗口居中与模态)
    box.setIcon(icon)          # 设置消息框左侧图标(信息/警告/错误/疑问四种)
    box.setWindowTitle(title)  # 设置对话框标题栏文字
    box.setText(text)          # 设置对话框正文内容文字
    # 设置消息框最小宽度,防止长内容被压缩导致布局异常,与项目整体视觉风格保持一致
    box.setMinimumWidth(320)   # 强制最小宽度为 320 像素,避免出现过窄的对话框
    return box                 # 返回配置完成的消息框实例,供调用方继续添加按钮等


# =====** [单按钮告知框]内部工具函数 =====**
def _single(
    icon: QMessageBox.Icon,
    parent: Optional[QWidget],
    title: str,
    text: str,
    ok_text: str,
) -> None:
    """
    内部工具函数:构造并显示单按钮告知型对话框

    单按钮对话框用于纯告知场景,用户点击唯一按钮、按 ESC 键或点击窗口关闭按钮(X)
    均会关闭对话框,无返回值(因为用户没有选择余地).

    :param icon: 消息框图标类型
    :type icon: QMessageBox.Icon
    :param parent: 父窗口控件
    :type parent: Optional[QWidget]
    :param title: 对话框标题
    :type title: str
    :param text: 对话框正文内容
    :type text: str
    :param ok_text: 唯一确认按钮的显示文案
    :type ok_text: str
    :return: 无返回值(模态阻塞直到用户关闭对话框)
    :rtype: None
    """
    box = _build_box(icon, parent, title, text)          # 调用内部构造函数创建统一外观的消息框
    ok_btn = box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)  # 添加一个 Accept 角色的按钮,文案由调用方指定
    box.setDefaultButton(ok_btn)                          # 将该按钮设为默认按钮(用户按回车键时触发)
    box.exec()                                            # 模态显示对话框,阻塞主线程直到用户关闭


# =====** [信息提示框]公开接口 =====**
def show_info(parent: Optional[QWidget], title: str, text: str,
              ok_text: str = DEFAULT_OK) -> None:
    """
    信息提示框(单按钮,蓝色信息图标,默认按钮文案"确定")

    用于成功提示、常规告知等正向场景,如"保存成功"、"操作完成"等.
    用户点击"确定"按钮或按回车即可关闭.

    :param parent: 父窗口控件,None 表示无父窗口
    :type parent: Optional[QWidget]
    :param title: 对话框标题栏文字
    :type title: str
    :param text: 对话框正文内容
    :type text: str
    :param ok_text: 确认按钮显示文案,默认为"确定"
    :type ok_text: str
    :return: 无返回值
    :rtype: None
    """
    # 使用蓝色 Information 图标 + 单按钮模式显示信息提示
    _single(QMessageBox.Icon.Information, parent, title, text, ok_text)


# =====** [警告提示框]公开接口 =====**
def show_warning(parent: Optional[QWidget], title: str, text: str,
                 ok_text: str = DEFAULT_KNOW) -> None:
    """
    警告提示框(单按钮,黄色警告图标,默认按钮文案"知道了")

    用于输入校验不通过、操作条件不满足等警告场景,如"请填写必填项"、
    "网络连接异常"等.用户点击"知道了"按钮关闭对话框.

    :param parent: 父窗口控件
    :type parent: Optional[QWidget]
    :param title: 对话框标题栏文字
    :type title: str
    :param text: 对话框正文内容
    :type text: str
    :param ok_text: 确认按钮显示文案,默认为"知道了"
    :type ok_text: str
    :return: 无返回值
    :rtype: None
    """
    # 使用黄色 Warning 图标 + 单按钮模式显示警告提示
    _single(QMessageBox.Icon.Warning, parent, title, text, ok_text)


# =====** [错误提示框]公开接口 =====**
def show_error(parent: Optional[QWidget], title: str, text: str,
               ok_text: str = DEFAULT_KNOW) -> None:
    """
    错误提示框(单按钮,红色错误图标,默认按钮文案"知道了")

    用于操作失败、程序异常等错误场景,如"登录失败"、"文件读取错误"等.
    用户点击"知道了"按钮关闭对话框.

    :param parent: 父窗口控件
    :type parent: Optional[QWidget]
    :param title: 对话框标题栏文字
    :type title: str
    :param text: 对话框正文内容
    :type text: str
    :param ok_text: 确认按钮显示文案,默认为"知道了"
    :type ok_text: str
    :return: 无返回值
    :rtype: None
    """
    # 使用红色 Critical 图标 + 单按钮模式显示错误提示
    _single(QMessageBox.Icon.Critical, parent, title, text, ok_text)


# =====** [确认对话框]公开接口 =====**
def show_confirm(
    parent: Optional[QWidget],
    title: str,
    text: str,
    ok_text: str = DEFAULT_CONFIRM,
    cancel_text: str = DEFAULT_CANCEL,
    danger: bool = False,
    default_ok: bool = True,
    icon: QMessageBox.Icon = QMessageBox.Icon.Question,
) -> bool:
    """
    确认/取消双按钮对话框

    用于需要用户做出决策的场景,返回布尔值表示用户的选择结果.
    支持危险操作模式,确认按钮会显示红色样式以提醒用户后果.
    ESC 键默认等同取消按钮,避免误触危险操作.

    :param parent: 父窗口控件
    :type parent: Optional[QWidget]
    :param title: 对话框标题栏文字
    :type title: str
    :param text: 对话框正文内容
    :type text: str
    :param ok_text: 确认按钮显示文案,需按场景定制(如"立即更新"、"确认删除")
    :type ok_text: str
    :param cancel_text: 取消按钮显示文案,需按场景定制(如"暂不更新"、"继续等待")
    :type cancel_text: str
    :param danger: 是否为危险操作,True 时确认按钮显示红色样式以警示用户,默认 False
    :type danger: bool
    :param default_ok: 回车键默认触发的按钮,True=确认按钮,False=取消按钮,默认 True
    :type default_ok: bool
    :param icon: 消息框图标类型,默认问号图标,可按需替换为警告/错误图标
    :type icon: QMessageBox.Icon
    :return: 用户点击结果,True=点击了确认按钮,False=点击了取消按钮/按ESC/点X关闭
    :rtype: bool
    """
    box = _build_box(icon, parent, title, text)           # 调用内部构造函数创建统一外观的消息框
    cancel_btn = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)  # 添加 Reject 角色的取消按钮
    ok_btn = box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)          # 添加 Accept 角色的确认按钮

    # 判断是否为危险操作,如果是则为确认按钮添加红色警示样式
    if danger:
        # 危险操作样式:红色粗体文字 + 红色边框 + 圆角,悬停时浅红背景
        ok_btn.setStyleSheet(
            "QPushButton{color:#c62828;font-weight:bold;"     # 设置按钮文字为深红色并加粗
            "border:1px solid #e57373;padding:5px 14px;border-radius:4px;}"  # 设置红色边框、内边距、圆角
            "QPushButton:hover{background:#fdecea;}"          # 鼠标悬停时背景变为浅红色,增强交互反馈
        )

    # 设置回车键默认触发的按钮(根据 default_ok 参数选择确认或取消按钮)
    box.setDefaultButton(ok_btn if default_ok else cancel_btn)
    box.setEscapeButton(cancel_btn)                           # 设置 ESC 键触发取消按钮,防止误触危险操作
    box.exec()                                                # 模态显示对话框,阻塞主线程直到用户点击按钮

    # 判断用户点击的按钮是否为确认按钮,返回布尔值结果
    return box.clickedButton() is ok_btn
