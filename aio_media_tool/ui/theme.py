from __future__ import annotations

DARK_THEME = """
* {
    font-family: "Inter", "Segoe UI", "Noto Sans", sans-serif;
    font-size: 13px;
}
QMainWindow, QWidget#Root { background: #0b0d12; color: #edf0f7; }
QWidget { color: #edf0f7; }
QFrame#Sidebar { background: #11141b; border-right: 1px solid #242936; }
QFrame#Topbar { background: #0b0d12; border-bottom: 1px solid #202532; }
QFrame#Card, QFrame#DropZone {
    background: #141821;
    border: 1px solid #272d3a;
    border-radius: 14px;
}
QWidget#BoardCanvas { background: #0f1219; }
QFrame#BoardBlock {
    background: #171c26;
    border: 1px solid #394255;
    border-radius: 11px;
}
QFrame#BoardBlock:hover { border-color: #7b69f7; }
QLabel#BoardMedia, QTextBrowser#BoardNote {
    background: #10141c;
    border: 1px solid #2c3442;
    border-radius: 7px;
    padding: 7px;
}
QFrame#Card:hover { border-color: #363f50; }
QLabel#Brand { font-size: 19px; font-weight: 750; color: #ffffff; }
QLabel#BrandAccent { font-size: 10px; font-weight: 700; color: #8a7dff; }
QLabel#PageTitle { font-size: 26px; font-weight: 760; color: #ffffff; }
QLabel#PageDescription { color: #9098aa; font-size: 13px; }
QLabel#SectionTitle { font-size: 16px; font-weight: 700; color: #f8f9fc; }
QLabel#CardTitle { font-size: 14px; font-weight: 700; color: #ffffff; }
QLabel#Muted { color: #8992a5; }
QLabel#Kpi { font-size: 28px; font-weight: 800; color: #ffffff; }
QLabel#Badge { background: #24203f; color: #b8adff; border-radius: 9px; padding: 3px 9px; font-weight: 650; }
QPushButton {
    min-height: 36px;
    padding: 0 15px;
    border: 1px solid #313746;
    border-radius: 9px;
    background: #1b202b;
    color: #edf0f7;
    font-weight: 600;
}
QPushButton:hover { background: #252b38; border-color: #444d60; }
QPushButton:pressed { background: #151922; }
QPushButton:disabled { color: #60697b; background: #161a22; border-color: #232833; }
QPushButton#Primary { background: #705cf6; border-color: #7c69ff; color: #ffffff; }
QPushButton#Primary:hover { background: #806cff; }
QPushButton#Danger { color: #ff9d9d; border-color: #6a353d; background: #2a1b20; }
QPushButton#Nav {
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: #9199aa;
    text-align: left;
    min-height: 41px;
    padding-left: 14px;
    font-weight: 600;
}
QPushButton#Nav:hover { background: #1b202a; color: #e9ecf4; }
QPushButton#Nav:checked { background: #25213f; color: #c8c0ff; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QListWidget, QTreeWidget, QTableWidget {
    background: #10131a;
    border: 1px solid #303644;
    border-radius: 8px;
    min-height: 36px;
    padding: 0 10px;
    selection-background-color: #6f5bf3;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QListWidget:focus {
    border-color: #7c69ff;
}
QPlainTextEdit, QListWidget, QTreeWidget { padding: 8px; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox QAbstractItemView { background: #171b24; border: 1px solid #343b4a; selection-background-color: #6251d8; }
QCheckBox { spacing: 8px; color: #c5cad5; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #495163; border-radius: 4px; background: #10131a; }
QCheckBox::indicator:checked { background: #715df5; border-color: #8877ff; }
QSlider::groove:horizontal { height: 5px; background: #2a303d; border-radius: 2px; }
QSlider::handle:horizontal { width: 17px; margin: -6px 0; border-radius: 8px; background: #8170ff; }
QSlider::sub-page:horizontal { background: #6f5cf1; border-radius: 2px; }
QProgressBar { background: #202532; border: 0; border-radius: 4px; height: 8px; text-align: center; color: transparent; }
QProgressBar::chunk { background: #7562f6; border-radius: 4px; }
QHeaderView::section { background: #161a23; color: #929bad; border: 0; border-bottom: 1px solid #2d3340; padding: 9px; }
QTableWidget { gridline-color: #232936; padding: 0; }
QTableWidget::item { padding: 7px; border-bottom: 1px solid #202530; }
QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: #363d4b; border-radius: 5px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: transparent; color: #8992a4; padding: 10px 16px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #c7beff; border-bottom-color: #7967f6; }
QToolTip { background: #202632; color: white; border: 1px solid #3c4454; padding: 5px; }
"""


LIGHT_THEME = (
    DARK_THEME
    + """
QMainWindow, QWidget#Root { background: #f4f5f8; color: #20232b; }
QWidget { color: #20232b; }
QFrame#Sidebar { background: #ffffff; border-right-color: #e2e5ec; }
QFrame#Topbar { background: #f4f5f8; border-bottom-color: #dfe3ea; }
QFrame#Card, QFrame#DropZone { background: #ffffff; border-color: #dfe3eb; }
QWidget#BoardCanvas { background: #eef0f5; }
QFrame#BoardBlock { background: #ffffff; border-color: #cdd3df; }
QLabel#BoardMedia, QTextBrowser#BoardNote { background: #f7f8fb; border-color: #dfe3eb; }
QLabel#Brand, QLabel#PageTitle, QLabel#SectionTitle, QLabel#CardTitle, QLabel#Kpi { color: #171920; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QListWidget, QTreeWidget, QTableWidget { background: #ffffff; border-color: #d7dbe4; color: #20232b; }
QPushButton { background: #ffffff; color: #252832; border-color: #d5d9e2; }
QPushButton:hover { background: #f0f1f5; }
QPushButton#Nav { color: #5c6473; }
QPushButton#Nav:hover { background: #f0f1f6; color: #20242c; }
QPushButton#Nav:checked { background: #edeaff; color: #5c47d5; }
QHeaderView::section { background: #f4f5f8; color: #677080; border-bottom-color: #dfe3ea; }
QTableWidget::item { border-bottom-color: #eceef3; }
"""
)
