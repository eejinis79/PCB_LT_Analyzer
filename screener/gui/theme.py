"""다크 테마 팔레트와 Qt 스타일시트."""

BG = "#0f1320"
SURFACE = "#171c2c"
SURFACE_2 = "#1e2438"
BORDER = "#2a3150"
TEXT = "#e6e9f2"
MUTED = "#8a93b2"
ACCENT = "#6c8cff"
ACCENT_2 = "#8f6cff"
UP = "#ff5c6c"      # 한국 관례: 상승 = 빨강
DOWN = "#4da3ff"    # 하락 = 파랑
WARN = "#ffb547"
GOOD = "#3ddc97"

QSS = f"""
* {{
    font-family: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR",
                 "NanumBarunGothic", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QWidget#Root {{ background: {BG}; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}

QFrame#Header {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1a2040, stop:1 #221a40);
    border-bottom: 1px solid {BORDER};
}}
QLabel#Title {{ font-size: 19px; font-weight: 700; }}
QLabel#Subtitle {{ color: {MUTED}; font-size: 12px; }}

QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QLabel#CardTitle {{ font-size: 13px; font-weight: 700; color: {TEXT}; }}
QLabel#CardHint {{ color: {MUTED}; font-size: 11px; }}
QLabel#ParamLabel {{ color: {MUTED}; }}

QFrame#Chip {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QLabel#ChipValue {{ font-size: 20px; font-weight: 700; }}
QLabel#ChipLabel {{ color: {MUTED}; font-size: 11px; }}

QPushButton {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border: none; font-weight: 700; color: white;
}}
QPushButton#Primary:hover {{ background: {ACCENT}; }}
QPushButton#Ghost {{ background: transparent; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    width: 0; border: none;  /* NoButtons 는 Qt 6 + 스타일시트에서 입력란이 사라지는 버그가 있어 QSS 로 숨김 */
}}
QAbstractSpinBox QLineEdit, QDateEdit QLineEdit {{
    padding: 0; border: none; background: transparent;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: white; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
    border: 2px solid {ACCENT};
}}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {SURFACE_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QTableView, QTreeWidget {{
    background: {SURFACE};
    alternate-background-color: #1a2033;
    border: 1px solid {BORDER};
    border-radius: 10px;
    gridline-color: transparent;
    selection-background-color: #2b3566;
    selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {SURFACE_2};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 7px 6px;
    font-weight: 600;
}}
QTableView::item {{ padding: 4px; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

QSplitter::handle {{ background: {BG}; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QProgressBar {{
    background: {SURFACE_2}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border-radius: 4px;
}}
QDialog {{ background: {BG}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; background: {BG}; top: -1px; }}
QTabBar::tab {{
    background: {SURFACE_2}; color: {MUTED}; padding: 8px 18px; margin-right: 4px;
    border: 1px solid {BORDER}; border-bottom: none; border-top-left-radius: 8px; border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{ background: {SURFACE}; color: {TEXT}; font-weight: 700; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QMenu {{ background: {SURFACE_2}; border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background: {ACCENT}; }}
"""
