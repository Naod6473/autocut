"""Thème sombre, icônes et petits widgets réutilisables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import sys

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QSizePolicy, QToolButton, QVBoxLayout, QWidget

ACCENT = "#ff8a3d"
BG = "#1b1d22"
PANEL = "#23262d"
FIELD = "#2c3038"
BORDER = "#363a44"
TEXT = "#e6e9ef"
MUTED = "#8b93a1"

# Tracés d'icônes au trait (grille 24×24), dans l'esprit de Lucide
_ICONS = {
    "open": '<path d="M6 14l1.5-2.9A2 2 0 0 1 9.2 10H20a2 2 0 0 1 1.9 2.5l-1.5 6a2 2 0 0 1-1.9 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.7.9l.8 1.2a2 2 0 0 0 1.7.9H18a2 2 0 0 1 2 2v2"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/>',
    "close": '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>',
    "play": '<path d="M7 4l13 8-13 8z" fill="currentColor"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor"/>',
    "prev": '<path d="M19 20 9 12l10-8z" fill="currentColor"/><path d="M5 19V5"/>',
    "next": '<path d="M5 4l10 8-10 8z" fill="currentColor"/><path d="M19 5v14"/>',
    "playall": '<path d="M3 6h11"/><path d="M3 12h8"/><path d="M3 18h8"/><path d="M15 11l6 4-6 4z" fill="currentColor"/>',
    "undo": '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/>',
    "redo": '<path d="M21 7v6h-6"/><path d="M3 17a9 9 0 0 1 9-9 9 9 0 0 1 6 2.3l3 2.7"/>',
    "trash": '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "zoomin": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6"/><path d="M8 11h6"/>',
    "zoomout": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M8 11h6"/>',
    "fit": '<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/>',
    "export": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    "detect": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9"/><path d="M14.5 14.5 20 20"/><path d="M8.1 8.1 12 12"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "list": '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/>',
}


@lru_cache(maxsize=None)
def icon(name: str, color: str = TEXT) -> QIcon:
    body = _ICONS[name].replace("currentColor", color)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    result = QIcon()
    for size in (16, 20, 24, 32, 48):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        result.addPixmap(pm)
        disabled = QPixmap(pm.size())
        disabled.fill(Qt.transparent)
        p = QPainter(disabled)
        p.setOpacity(0.35)
        p.drawPixmap(0, 0, pm)
        p.end()
        result.addPixmap(disabled, QIcon.Disabled)
    return result


def asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "autocut" / "assets" / name


def app_icon() -> QIcon:
    ico = QIcon()
    for name in ("autocut.ico", "autocut.png"):
        path = asset_path(name)
        if path.exists():
            ico.addFile(str(path))
    return ico


STYLE = f"""
QWidget {{ color: {TEXT}; font-size: 10pt; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolBar {{ background: {BG}; border: none; border-bottom: 1px solid {BORDER}; padding: 4px; spacing: 2px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px 8px; }}
QToolButton:hover {{ background: {FIELD}; border-color: {BORDER}; }}
QToolButton:pressed, QToolButton:checked {{ background: {BORDER}; }}
QMenuBar {{ background: {BG}; }}
QMenuBar::item:selected {{ background: {FIELD}; border-radius: 4px; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 24px 5px 10px; border-radius: 4px; }}
QMenu::item:selected {{ background: {FIELD}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {FIELD}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 6px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{{chevron}}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {FIELD}; }}
QPushButton {{ background: {FIELD}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {MUTED}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPushButton#primary {{ background: {ACCENT}; color: #1b1d22; border: none; font-weight: 600; padding: 9px 12px; }}
QPushButton#primary:hover {{ background: #ff9d5c; }}
QPushButton#primary:disabled {{ background: #6b4a35; color: #2a2d36; }}
QListWidget, QTableWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; alternate-background-color: #262a31; gridline-color: {BORDER}; }}
QListWidget::item {{ padding: 6px; border-radius: 6px; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background: #3a2b22; color: {TEXT}; }}
QListWidget::item:selected {{ border-left: 3px solid {ACCENT}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 5px; }}
QTableCornerButton::section {{ background: {PANEL}; border: none; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; border: 1px solid {MUTED}; background: {FIELD}; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ image: url("{{check}}"); }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle {{ background: {BORDER}; border-radius: 5px; min-width: 30px; min-height: 30px; }}
QScrollBar::handle:hover {{ background: {MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QSplitter::handle {{ background: {BG}; }}
QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QWidget#sidepanel, QWidget#transport {{ background: {BG}; }}
QWidget#section {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
QToolButton#sectionHeader {{ font-weight: 600; font-size: 10.5pt; padding: 8px 10px; border: none; text-align: left; }}
QToolButton#sectionHeader:hover {{ background: transparent; color: {ACCENT}; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#title {{ color: {MUTED}; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; font-size: 8.5pt; }}
QLabel#time {{ color: {TEXT}; font-family: Consolas, 'DejaVu Sans Mono', monospace; padding: 0 8px; }}
QProgressDialog {{ background: {PANEL}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
"""


def apply_theme(app: QApplication):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in [
        (QPalette.Window, BG),
        (QPalette.WindowText, TEXT),
        (QPalette.Base, PANEL),
        (QPalette.AlternateBase, "#262a31"),
        (QPalette.Text, TEXT),
        (QPalette.Button, FIELD),
        (QPalette.ButtonText, TEXT),
        (QPalette.Highlight, ACCENT),
        (QPalette.HighlightedText, "#1b1d22"),
        (QPalette.ToolTipBase, PANEL),
        (QPalette.ToolTipText, TEXT),
        (QPalette.PlaceholderText, MUTED),
    ]:
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(MUTED))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(MUTED))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(MUTED))
    app.setPalette(pal)
    style = STYLE.replace("{chevron}", asset_path("chevron.svg").as_posix()).replace("{check}", asset_path("check.svg").as_posix())
    app.setStyleSheet(style)


class Section(QWidget):
    """Encadré avec un titre cliquable qui replie ou déplie son contenu."""

    def __init__(self, title: str, content: QWidget, expanded: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("section")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.header = QToolButton()
        self.header.setObjectName("sectionHeader")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.setIconSize(QSize(12, 12))
        self.header.toggled.connect(self._toggle)
        self.content = content
        self.content.setVisible(expanded)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 8)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(self.content)

    def _toggle(self, on: bool):
        self.header.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.content.setVisible(on)
