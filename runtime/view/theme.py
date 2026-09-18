# -*- coding: utf-8 -*-
# ==============================================================================
# 文件: theme.py
# 归属: runtime/view/ 视图层公共模块 —— 全局深色主题样式表
# ------------------------------------------------------------------------------
# 文件用途:
#   提供应用全局统一深色主题样式表 DARK_THEME_QSS, 供主窗口(MainWindow)、
#   对话框(common_dialog)与全局 QApplication 应用, 确保 Windows/macOS 系统
#   亮色模式下界面仍保持深色(此前 Windows 亮色系统下未设样式的控件
#   跟随系统亮色, 界面呈"半深半亮"状态)。
#
# 配色(与 updater_app/view/updater_window.py 的 _DARK_STYLE 同一套色板):
#   - 窗口/面板背景: #252526 / #2a2a2b
#   - 输入/列表/表格背景: #1e1e1e
#   - 边框: #3a3a3a / #444
#   - 主文字: #e0e0e0; 次要文字: #9a9a9a
#   - 高亮/主操作: #2f6fbd
#
# 样式覆盖规则(Qt):
#   子控件自身的 setStyleSheet 优先级高于祖先/全局样式表, 各控件专属样式
#   (按钮配色/进度条/日志框等)不会被本主题覆盖。
# ==============================================================================

# =====** 全局深色主题样式表 =====
DARK_THEME_QSS = """
QMainWindow, QWidget { background-color:#252526; color:#e0e0e0; }
QGroupBox {
    background-color:#2a2a2b; color:#e0e0e0; font-weight:bold;
    border:1px solid #3a3a3a; border-radius:6px; margin-top:8px; padding:10px;
}
QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; }
QLineEdit {
    background-color:#1e1e1e; color:#e0e0e0; border:1px solid #3a3a3a;
    border-radius:3px; padding:4px 6px; selection-background-color:#2f6fbd;
}
QLineEdit:focus { border-color:#2f6fbd; }
QLineEdit:disabled { background-color:#2a2a2a; color:#888; }
QListWidget {
    background-color:#1e1e1e; color:#e0e0e0; border:1px solid #3a3a3a; border-radius:4px;
}
QListWidget::item { padding:4px 6px; }
QListWidget::item:selected { background-color:#2f6fbd; color:white; }
QListWidget::item:hover { background-color:#333; }
QTableWidget {
    background-color:#1e1e1e; color:#e0e0e0; border:1px solid #3a3a3a;
    gridline-color:#3a3a3a; border-radius:4px;
}
QTableWidget::item { padding:3px 6px; }
QTableWidget::item:selected { background-color:#2f6fbd; color:white; }
QHeaderView::section {
    background-color:#2d2d2d; color:#e0e0e0; border:1px solid #3a3a3a; padding:4px 6px;
}
QTextEdit {
    background-color:#1b1b1b; color:#c8c8c8; border:1px solid #3a3a3a; border-radius:4px;
}
QTabWidget::pane { border:1px solid #3a3a3a; background-color:#252526; }
QTabBar::tab {
    background-color:#2d2d2d; color:#9a9a9a; padding:8px 18px;
    border-top-left-radius:4px; border-top-right-radius:4px;
}
QTabBar::tab:selected { background-color:#2f6fbd; color:white; }
QTabBar::tab:hover { background-color:#3a3a3a; }
QLabel { color:#e0e0e0; background:transparent; }
QCheckBox { color:#e0e0e0; }
QComboBox {
    background-color:#1e1e1e; color:#e0e0e0; border:1px solid #3a3a3a;
    border-radius:3px; padding:4px 6px;
}
QComboBox QAbstractItemView {
    background-color:#2d2d2d; color:#e0e0e0; selection-background-color:#2f6fbd;
}
QProgressBar {
    background-color:#2b2b2b; border:1px solid #444; border-radius:4px;
    color:#e0e0e0; text-align:center;
}
QProgressBar::chunk { background-color:#2f6fbd; border-radius:3px; }
QScrollBar:vertical { background:#252526; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#4a4a4a; border-radius:5px; min-height:30px; }
QScrollBar::handle:vertical:hover { background:#5a5a5a; }
QScrollBar:horizontal { background:#252526; height:10px; margin:0; }
QScrollBar::handle:horizontal { background:#4a4a4a; border-radius:5px; min-width:30px; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
"""
