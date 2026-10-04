"""Fenêtre principale d'Autocut."""

from __future__ import annotations

import copy
import json
import random
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf
from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .core import (
    DEFAULT_TEMPLATE,
    SUPPORTED_INPUT,
    DetectionSettings,
    ExportSettings,
    Track,
    detect_segments,
    estimate_threshold,
    export_track,
    format_time,
    group_variations,
    load_session,
    load_track,
    names_from_text,
    process_segment,
    sanitize_filename,
    save_session,
    variation_params,
    write_listing,
)
from .theme import Section, app_icon, apply_theme, asset_path, icon
from .waveform import WaveformView

SESSION_EXT = ".autocut"
SAMPLERATES = [("Fréquence d'origine", None), ("22 050 Hz", 22050), ("44 100 Hz", 44100), ("48 000 Hz", 48000)]
MP3_QUALITIES = [("Haute", 0.0), ("Normale", 0.3), ("Légère", 0.6)]
WAV_DEPTHS = [("16 bits", "PCM_16"), ("24 bits", "PCM_24"), ("32 bits flottant", "FLOAT")]
NORM_MODES = [("dBFS crête", "peak"), ("LUFS (volume perçu)", "lufs")]
HIGHPASS = [("Aucun", 0.0), ("40 Hz", 40.0), ("80 Hz", 80.0), ("120 Hz", 120.0)]
FADE_CURVES = [("Linéaire", "linear"), ("Douce (en S)", "smooth"), ("Rapide", "sharp")]
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
COL_NAME, COL_LOOP = 4, 5
LISTING_NAME = "liste_sons.csv"

# Préréglages de détection fournis : (seuil dB, silence min ms, son min ms, marge ms)
BUILTIN_PRESETS = {
    "Par défaut": (-45.0, 250.0, 60.0, 20.0),
    "Sons courts (pas, impacts, clics)": (-45.0, 120.0, 30.0, 10.0),
    "Sons longs (ambiances, explosions)": (-55.0, 600.0, 300.0, 60.0),
    "Bruit de fond fort": (-35.0, 250.0, 60.0, 20.0),
}


def _spin(minimum, maximum, value, step=1.0, suffix="", decimals=0):
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setButtonSymbols(QDoubleSpinBox.NoButtons)
    return box


def _form() -> tuple[QWidget, QFormLayout]:
    w = QWidget()
    f = QFormLayout(w)
    f.setContentsMargins(10, 4, 10, 4)
    f.setHorizontalSpacing(10)
    f.setVerticalSpacing(7)
    f.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return w, f


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(True)
    return label


class NamesDialog(QDialog):
    def __init__(self, parent, count: int, current: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Coller une liste de noms")
        self.resize(420, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Un nom par ligne, dans l'ordre des sons ({count} sons sur cette piste).\nLes sons sans nom gardent le nom du modèle."))
        self.edit = QPlainTextEdit("\n".join(current))
        lay.addWidget(self.edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def names(self) -> list[str]:
        return names_from_text(self.edit.toPlainText())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Autocut {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1360, 840)
        self.setAcceptDrops(True)
        self.settings = QSettings("Autocut", "Autocut")
        self.tracks: list[Track] = []
        self.undo: dict[int, list] = {}
        self.redo: dict[int, list] = {}
        self.session_path: Path | None = None
        self._updating = False
        self._play_offset = 0
        self._queue: list[int] = []
        self._tmpdir = tempfile.TemporaryDirectory(prefix="autocut_", ignore_cleanup_errors=True)
        self._tmpcount = 0

        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.player.positionChanged.connect(self._on_play_position)
        self.player.mediaStatusChanged.connect(self._on_media_status)

        self._build_actions()
        self._build_ui()
        self._load_settings()
        self._refresh_all()

    # --- Actions, menus et barre d'outils ------------------------------------

    def _act(self, text, slot, shortcut=None, icon_name=None, tip=None):
        a = QAction(text, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if icon_name:
            a.setIcon(icon(icon_name))
        if tip or shortcut:
            a.setToolTip(f"{tip or text} ({QKeySequence(shortcut).toString(QKeySequence.NativeText)})" if shortcut else tip)
        return a

    def _build_actions(self):
        A = self._act
        self.a_open = A("Ouvrir des pistes…", self.open_files, "Ctrl+O", "open")
        self.a_open_session = A("Ouvrir une session…", self.open_session, "Ctrl+Shift+O")
        self.a_save_session = A("Enregistrer la session", self.save_session, "Ctrl+S", "save")
        self.a_save_session_as = A("Enregistrer la session sous…", lambda: self.save_session(ask=True), "Ctrl+Shift+S")
        self.a_remove = A("Retirer la piste", self.remove_track, None, "close")
        self.a_quit = A("Quitter", self.close, "Ctrl+Q")
        self.a_undo = A("Annuler", self.undo_edit, "Ctrl+Z", "undo")
        self.a_redo = A("Rétablir", self.redo_edit, "Ctrl+Y", "redo")
        self.a_delete = A("Supprimer le son", self.delete_selected, "Delete", "trash")
        self.a_loop = A("Boucle sans coupure", self.toggle_loop, "L", None, "Exporter ce son comme une boucle sans coupure (L)")
        self.a_play = A("Écouter le son", self.play_selected, "Space", "play")
        self.a_play_all = A("Tout écouter à la suite", self.play_all, "Ctrl+Space", "playall")
        self.a_play_track = A("Écouter la piste entière", self.play_track, None)
        self.a_stop = A("Stop", self.stop, "Escape", "stop")
        self.a_prev = A("Son précédent", lambda: self.step(-1), None, "prev", "Son précédent (↑)")
        self.a_next = A("Son suivant", lambda: self.step(1), None, "next", "Son suivant (↓)")
        self.a_zoom_in = A("Zoom avant", lambda: self.wave.zoom(0.5), "Ctrl++", "zoomin")
        self.a_zoom_out = A("Zoom arrière", lambda: self.wave.zoom(2.0), "Ctrl+-", "zoomout")
        self.a_zoom_fit = A("Tout afficher", lambda: self.wave.zoom_fit(), "Ctrl+0", "fit")
        self.a_export = A("Exporter la piste", self.export_current, "Ctrl+E", "export")
        self.a_export_all = A("Exporter toutes les pistes", self.export_all, "Ctrl+Shift+E", "export")
        self.a_help = A("Aide", self.show_help, "F1", "help")
        self.a_about = A("À propos d'Autocut", self.show_about)
        self.a_preview_fx = QAction("Écouter avec les réglages d'export", self, checkable=True)
        self.a_preview_fx.setToolTip("Applique mono, normalisation, fondus et fréquence pendant l'écoute")

        mb = self.menuBar()
        m = mb.addMenu("&Fichier")
        for a in (self.a_open, self.a_open_session, None, self.a_save_session, self.a_save_session_as, None, self.a_remove, None, self.a_export, self.a_export_all, None, self.a_quit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("É&dition")
        for a in (self.a_undo, self.a_redo, None, self.a_delete, self.a_loop):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Lecture")
        for a in (self.a_play, self.a_play_all, self.a_play_track, self.a_stop, None, self.a_prev, self.a_next, None, self.a_preview_fx):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Affichage")
        for a in (self.a_zoom_in, self.a_zoom_out, self.a_zoom_fit):
            m.addAction(a)
        m = mb.addMenu("&Aide")
        m.addAction(self.a_help)
        m.addAction(self.a_about)

        tb = QToolBar("Actions")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        tb.addAction(self.a_open)
        tb.addAction(self.a_save_session)
        tb.addSeparator()
        for a in (self.a_undo, self.a_redo, self.a_delete):
            tb.addAction(a)
            tb.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonIconOnly)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.a_help)
        tb.widgetForAction(self.a_help).setToolButtonStyle(Qt.ToolButtonIconOnly)

    # --- Construction de l'interface ----------------------------------------

    def _build_ui(self):
        # Liste des pistes
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 4, 10)
        title = QLabel("Pistes")
        title.setObjectName("title")
        ll.addWidget(title)
        self.track_list = QListWidget()
        self.track_list.currentRowChanged.connect(self._on_track_changed)
        ll.addWidget(self.track_list)
        add = QPushButton(icon("plus"), " Ajouter des pistes")
        add.clicked.connect(self.open_files)
        ll.addWidget(add)

        # Forme d'onde, barre de lecture, tableau
        self.wave = WaveformView()
        self.wave.selectionChanged.connect(self._on_wave_selection)
        self.wave.aboutToEdit.connect(self._push_undo)
        self.wave.segmentsEdited.connect(self._refresh_all)
        self.wave.playRequested.connect(self.play_segment)
        self.wave.viewChanged.connect(self._sync_scrollbar)
        self.scroll = QScrollBar(Qt.Horizontal)
        self.scroll.valueChanged.connect(self._on_scroll)

        transport = QWidget()
        transport.setObjectName("transport")
        tl = QHBoxLayout(transport)
        tl.setContentsMargins(0, 2, 0, 2)
        tl.setSpacing(2)

        def tbtn(action, primary=False):
            b = QToolButton()
            b.setDefaultAction(action)
            b.setIconSize(QSize(22, 22) if primary else QSize(18, 18))
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            return b

        for a in (self.a_prev,):
            tl.addWidget(tbtn(a))
        self.play_button = tbtn(self.a_play, True)
        tl.addWidget(self.play_button)
        for a in (self.a_next, self.a_stop, self.a_play_all):
            tl.addWidget(tbtn(a))
        self.time_label = QLabel("0:00.000")
        self.time_label.setObjectName("time")
        tl.addWidget(self.time_label)
        tl.addStretch(1)
        for a in (self.a_zoom_out, self.a_zoom_in, self.a_zoom_fit):
            tl.addWidget(tbtn(a))

        hint = _muted(
            "Clic sur un son : écouter · Glisser un bord : ajuster · Glisser dans le vide : nouveau son · "
            "Clic droit : couper, fusionner, supprimer · Molette : zoom · Maj+molette : défiler"
        )

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "Début", "Fin", "Durée", "Nom du fichier", "Boucle"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        for c in range(4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        hh.setSectionResizeMode(COL_LOOP, QHeaderView.ResizeToContents)
        self.table.horizontalHeaderItem(COL_LOOP).setToolTip("Boucle sans coupure : la fin du son est fondue sur son début à l'export (ambiances)")
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.itemChanged.connect(self._on_table_edit)
        self.table.cellDoubleClicked.connect(lambda r, c: self.play_segment(r) if c < 4 else None)

        top = QWidget()
        topl = QVBoxLayout(top)
        topl.setContentsMargins(4, 10, 4, 0)
        topl.setSpacing(4)
        topl.addWidget(self.wave, 1)
        topl.addWidget(self.scroll)
        topl.addWidget(transport)
        topl.addWidget(hint)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(4, 4, 4, 10)
        bl.addWidget(self.table)
        self.center = QSplitter(Qt.Vertical)
        self.center.addWidget(top)
        self.center.addWidget(bottom)
        self.center.setSizes([470, 290])

        # Navigation au clavier (↑/↓) quand la forme d'onde ou le tableau a le focus
        for key, delta in (("Down", 1), ("Up", -1)):
            for w in (self.wave, self.table):
                sc = QShortcut(QKeySequence(key), w)
                sc.setContext(Qt.WidgetShortcut)
                sc.activated.connect(lambda d=delta: self.step(d))

        # Panneau de droite : trois étapes repliables + boutons d'export toujours visibles
        steps = QWidget()
        sl = QVBoxLayout(steps)
        sl.setContentsMargins(4, 10, 10, 4)
        sl.setSpacing(10)
        sl.addWidget(Section("1   Détecter les sons", self._build_detection()))
        sl.addWidget(Section("2   Nommer", self._build_naming()))
        sl.addWidget(Section("3   Exporter", self._build_export()))
        sl.addStretch(1)
        scroll_side = QScrollArea()
        scroll_side.setWidget(steps)
        scroll_side.setWidgetResizable(True)
        scroll_side.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.btn_export = QPushButton(icon("export", "#1b1d22"), "  Exporter la piste")
        self.btn_export.setObjectName("primary")
        self.btn_export.clicked.connect(self.export_current)
        self.btn_export_all = QPushButton("Exporter toutes les pistes")
        self.btn_export_all.clicked.connect(self.export_all)
        side = QWidget()
        side.setObjectName("sidepanel")
        side.setMinimumWidth(390)
        side_l = QVBoxLayout(side)
        side_l.setContentsMargins(0, 0, 0, 10)
        side_l.addWidget(scroll_side, 1)
        buttons = QVBoxLayout()
        buttons.setContentsMargins(4, 0, 10, 0)
        buttons.addWidget(self.btn_export)
        buttons.addWidget(self.btn_export_all)
        side_l.addLayout(buttons)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(self.center)
        split.addWidget(side)
        split.setStretchFactor(1, 1)
        split.setSizes([210, 760, 410])
        self.setCentralWidget(split)
        self.statusBar().showMessage("Ouvre ou glisse une ou plusieurs pistes pour commencer.")

    def _build_detection(self):
        w, f = _form()
        preset_row = QHBoxLayout()
        self.det_preset = QComboBox()
        self.det_preset.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.det_preset.setMinimumContentsLength(10)
        self.det_preset.activated.connect(self._apply_preset)
        save = QToolButton()
        save.setIcon(icon("save"))
        save.setToolTip("Enregistrer les réglages actuels comme préréglage")
        save.clicked.connect(self._save_preset)
        delete = QToolButton()
        delete.setIcon(icon("trash"))
        delete.setToolTip("Supprimer ce préréglage")
        delete.clicked.connect(self._delete_preset)
        preset_row.addWidget(self.det_preset, 1)
        preset_row.addWidget(save)
        preset_row.addWidget(delete)
        f.addRow("Préréglage", preset_row)

        self.det_threshold = _spin(-90, -5, -45, 1, " dB")
        self.det_threshold.setToolTip("Tout ce qui est plus faible est considéré comme du silence. Monte-le si le bruit de fond est fort.")
        self.det_silence = _spin(10, 5000, 250, 10, " ms")
        self.det_silence.setToolTip("Un silence plus court ne sépare pas deux sons (utile pour les sons avec rebonds ou échos).")
        self.det_minsound = _spin(0, 5000, 60, 10, " ms")
        self.det_minsound.setToolTip("Les sons plus courts sont ignorés (clics, bruits parasites).")
        self.det_padding = _spin(0, 1000, 20, 5, " ms")
        self.det_padding.setToolTip("Marge gardée avant et après chaque son pour ne pas couper l'attaque ou la queue.")
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(self.det_threshold, 1)
        self.det_auto_threshold = QPushButton("Auto")
        self.det_auto_threshold.setToolTip("Régler le seuil d'après le bruit de fond de la piste, puis relancer la détection")
        self.det_auto_threshold.clicked.connect(self.auto_threshold)
        threshold_row.addWidget(self.det_auto_threshold)
        f.addRow("Seuil de silence", threshold_row)
        f.addRow("Silence minimum", self.det_silence)
        f.addRow("Son minimum", self.det_minsound)
        f.addRow("Marge", self.det_padding)
        row = QHBoxLayout()
        b1 = QPushButton(icon("detect"), " Cette piste")
        b1.clicked.connect(lambda: self.detect(all_tracks=False))
        b2 = QPushButton("Toutes les pistes")
        b2.clicked.connect(lambda: self.detect(all_tracks=True))
        row.addWidget(b1)
        row.addWidget(b2)
        f.addRow(row)
        self.det_auto = QCheckBox("Détecter automatiquement à l'ouverture")
        self.det_auto.setChecked(True)
        f.addRow(self.det_auto)
        return w

    def _build_naming(self):
        w, f = _form()
        self.name_unique = QRadioButton("Un titre numéroté")
        self.name_multi = QRadioButton("Un nom par son")
        group = QButtonGroup(w)
        group.addButton(self.name_unique)
        group.addButton(self.name_multi)
        self.name_unique.setChecked(True)
        self.name_unique.toggled.connect(self._on_naming_changed)
        modes = QHBoxLayout()
        modes.addWidget(self.name_unique)
        modes.addWidget(self.name_multi)
        f.addRow(modes)

        self.name_title = QLineEdit()
        self.name_title.setPlaceholderText("Titre de la piste")
        self.name_title.editingFinished.connect(self._on_naming_changed)
        self.name_template = QLineEdit(DEFAULT_TEMPLATE)
        self.name_template.setToolTip(
            "Modèle des noms de fichier, pour toutes les pistes.\n"
            "{titre} : le titre ci-dessus · {n} : le numéro · {piste} : le nom du fichier source\n"
            "Exemple : SFX_{titre}_{n} donne SFX_Porte_01"
        )
        self.name_template.editingFinished.connect(self._on_template_changed)
        self.name_start = QSpinBox()
        self.name_start.setRange(0, 99999)
        self.name_start.setValue(1)
        self.name_start.setButtonSymbols(QSpinBox.NoButtons)
        self.name_start.valueChanged.connect(self._on_naming_changed)
        self.name_digits = QSpinBox()
        self.name_digits.setRange(1, 5)
        self.name_digits.setValue(2)
        self.name_digits.setButtonSymbols(QSpinBox.NoButtons)
        self.name_digits.valueChanged.connect(self._on_naming_changed)
        nums = QHBoxLayout()
        nums.addWidget(self.name_start)
        nums.addWidget(QLabel("Chiffres"))
        nums.addWidget(self.name_digits)
        f.addRow("Titre", self.name_title)
        f.addRow("Modèle", self.name_template)
        f.addRow("Commencer à", nums)
        f.addRow(_muted("Étiquettes : {titre}, {n}, {piste}"))

        self.btn_paste = QPushButton(icon("list"), " Coller une liste de noms…")
        self.btn_paste.clicked.connect(self.paste_names)
        f.addRow(self.btn_paste)
        self.name_repeats = QCheckBox("Numéroter les noms répétés (pas_01, pas_02…)")
        self.name_repeats.setChecked(True)
        self.name_repeats.toggled.connect(self._refresh_all)
        f.addRow(self.name_repeats)
        self.btn_variations = QPushButton("Repérer les variantes")
        self.btn_variations.setToolTip("Regroupe les sons qui se ressemblent (timbre et durée) et leur donne un nom commun")
        self.btn_variations.clicked.connect(self.find_variations)
        f.addRow(self.btn_variations)
        f.addRow(_muted("En mode « un nom par son », double-clique sur un nom dans le tableau pour le modifier."))
        return w

    def _build_export(self):
        w, f = _form()
        self.exp_format = QComboBox()
        self.exp_format.addItems(["WAV", "MP3", "OGG"])
        self.exp_format.currentTextChanged.connect(self._on_format_changed)
        self.exp_wav = QComboBox()
        for label, _ in WAV_DEPTHS:
            self.exp_wav.addItem(label)
        self.exp_mp3 = QComboBox()
        for label, _ in MP3_QUALITIES:
            self.exp_mp3.addItem(label)
        self.exp_rate = QComboBox()
        for label, _ in SAMPLERATES:
            self.exp_rate.addItem(label)
        self.exp_mono = QCheckBox("Convertir en mono")
        self.exp_norm = QCheckBox("Normaliser à")
        self.exp_norm_db = _spin(-30, 0, -1, 0.5, " dBFS", 1)
        self.exp_norm_lufs = _spin(-40, -6, -16, 1, " LUFS")
        self.exp_norm_mode = QComboBox()
        for label, _ in NORM_MODES:
            self.exp_norm_mode.addItem(label)
        self.exp_norm_mode.setToolTip("Crête : même niveau maximal pour chaque son. LUFS : même volume perçu (norme ITU-R BS.1770), crête limitée à -1 dBFS.")
        self.exp_norm_mode.currentIndexChanged.connect(self._on_norm_mode)
        self.exp_highpass = QComboBox()
        for label, _ in HIGHPASS:
            self.exp_highpass.addItem(label)
        self.exp_highpass.setToolTip("Retire les grondements graves : vent, manipulation, ventilation")
        self.exp_trim = QCheckBox("Rogner les silences restants")
        self.exp_trim.setToolTip("Retire ce qui reste sous le seuil de silence au début et à la fin de chaque son")
        self.exp_fade_in = _spin(0, 2000, 0, 1, " ms")
        self.exp_fade_out = _spin(0, 5000, 5, 1, " ms")
        self.exp_curve = QComboBox()
        for label, _ in FADE_CURVES:
            self.exp_curve.addItem(label)
        self.exp_curve.setToolTip("Douce : ni attaque sèche ni coupure audible. Rapide : le son baisse vite puis s'éteint en douceur (impacts).")
        self.exp_loop_fade = _spin(10, 5000, 200, 10, " ms")
        self.exp_loop_fade.setToolTip("Durée du fondu enchaîné qui relie la fin d'une boucle à son début")
        self.exp_variants = QSpinBox()
        self.exp_variants.setRange(0, 8)
        self.exp_variants.setSpecialValueText("Aucune")
        self.exp_variants.setButtonSymbols(QSpinBox.NoButtons)
        self.exp_variants.setToolTip("Chaque son est aussi exporté en versions un peu plus aiguës ou graves et moins fortes (porte_v1, porte_v2…), pour éviter l'effet répétitif dans un jeu")
        self.exp_variant_pitch = _spin(0, 12, 1, 0.5, " demi-ton(s)", 1)
        self.exp_variant_volume = _spin(0, 12, 2, 0.5, " dB", 1)
        self.exp_variants.valueChanged.connect(lambda n: (self.exp_variant_pitch.setEnabled(n > 0), self.exp_variant_volume.setEnabled(n > 0)))
        self.exp_dir = QLineEdit()
        browse = QToolButton()
        browse.setIcon(icon("open"))
        browse.setToolTip("Choisir le dossier")
        browse.clicked.connect(self.choose_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.exp_dir, 1)
        dir_row.addWidget(browse)
        self.exp_subdir = QCheckBox("Un sous-dossier par piste")
        self.exp_overwrite = QCheckBox("Remplacer les fichiers existants")
        self.exp_overwrite.setToolTip("Sinon, un fichier déjà présent est conservé et le nouveau reçoit un suffixe _2, _3…")
        self.exp_csv = QCheckBox(f"Créer une liste des sons ({LISTING_NAME})")
        self.exp_csv.setToolTip("Fichier CSV avec le nom, la piste d'origine, le début, la fin et la durée de chaque son")
        self.exp_open = QCheckBox("Ouvrir le dossier après l'export")
        self.exp_open.setChecked(True)
        self.exp_preview = QCheckBox("Écouter avec ces réglages")
        self.exp_preview.setToolTip("Applique mono, normalisation, fondus et fréquence pendant l'écoute")
        self.exp_preview.toggled.connect(self.a_preview_fx.setChecked)
        self.a_preview_fx.toggled.connect(self.exp_preview.setChecked)

        f.addRow("Format", self.exp_format)
        f.addRow("Profondeur WAV", self.exp_wav)
        f.addRow("Qualité MP3", self.exp_mp3)
        f.addRow("Fréquence", self.exp_rate)
        f.addRow(self.exp_mono)
        norm_row = QHBoxLayout()
        norm_row.addWidget(self.exp_norm)
        norm_row.addWidget(self.exp_norm_db, 1)
        norm_row.addWidget(self.exp_norm_lufs, 1)
        norm_row.addWidget(self.exp_norm_mode)
        f.addRow(norm_row)
        f.addRow("Coupe-bas", self.exp_highpass)
        f.addRow(self.exp_trim)
        f.addRow("Fondu d'entrée", self.exp_fade_in)
        f.addRow("Fondu de sortie", self.exp_fade_out)
        f.addRow("Forme des fondus", self.exp_curve)
        f.addRow("Fondu de boucle", self.exp_loop_fade)
        f.addRow("Variations par son", self.exp_variants)
        f.addRow("Hauteur ±", self.exp_variant_pitch)
        f.addRow("Volume jusqu'à −", self.exp_variant_volume)
        f.addRow(self.exp_preview)
        f.addRow("Dossier", dir_row)
        f.addRow(self.exp_subdir)
        f.addRow(self.exp_overwrite)
        f.addRow(self.exp_csv)
        f.addRow(self.exp_open)
        return w

    # --- Réglages persistants -----------------------------------------------

    _persisted = [
        ("det_threshold", "value", float),
        ("det_silence", "value", float),
        ("det_minsound", "value", float),
        ("det_padding", "value", float),
        ("exp_norm_db", "value", float),
        ("exp_norm_lufs", "value", float),
        ("exp_norm_mode", "currentIndex", int),
        ("exp_highpass", "currentIndex", int),
        ("exp_curve", "currentIndex", int),
        ("exp_loop_fade", "value", float),
        ("exp_variants", "value", int),
        ("exp_variant_pitch", "value", float),
        ("exp_variant_volume", "value", float),
        ("exp_fade_in", "value", float),
        ("exp_fade_out", "value", float),
        ("exp_format", "currentIndex", int),
        ("exp_wav", "currentIndex", int),
        ("exp_mp3", "currentIndex", int),
        ("exp_rate", "currentIndex", int),
        ("name_digits", "value", int),
    ]
    _persisted_checks = ["det_auto", "exp_mono", "exp_norm", "exp_trim", "exp_subdir", "exp_overwrite", "exp_csv", "exp_open", "exp_preview", "name_repeats"]

    def _load_settings(self):
        s = self.settings
        for name, getter, kind in self._persisted:
            if s.contains(name):
                widget = getattr(self, name)
                setter = "set" + getter[0].upper() + getter[1:]
                try:
                    getattr(widget, setter)(kind(s.value(name)))
                except (TypeError, ValueError):
                    pass
        for name in self._persisted_checks:
            if s.contains(name):
                getattr(self, name).setChecked(str(s.value(name)).lower() == "true")
        self.exp_dir.setText(s.value("exp_dir", str(Path.home() / "Music" / "Autocut")))
        self.name_template.setText(s.value("template", DEFAULT_TEMPLATE))
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self._refresh_presets()
        self._on_format_changed(self.exp_format.currentText())
        self._on_norm_mode()
        n = self.exp_variants.value()
        self.exp_variant_pitch.setEnabled(n > 0)
        self.exp_variant_volume.setEnabled(n > 0)

    def _save_settings(self):
        s = self.settings
        for name, getter, _ in self._persisted:
            s.setValue(name, getattr(getattr(self, name), getter)())
        for name in self._persisted_checks:
            s.setValue(name, getattr(self, name).isChecked())
        s.setValue("exp_dir", self.exp_dir.text())
        s.setValue("template", self.template)
        s.setValue("geometry", self.saveGeometry())

    def closeEvent(self, e):
        self.stop()
        # Libère le dernier fichier d'écoute avant d'effacer le dossier temporaire (Windows le verrouille)
        self.player.setSource(QUrl())
        QApplication.processEvents()
        self._tmpdir.cleanup()
        self._save_settings()
        super().closeEvent(e)

    # --- Préréglages de détection -------------------------------------------

    def _user_presets(self) -> dict:
        try:
            return json.loads(self.settings.value("presets", "{}"))
        except (TypeError, ValueError):
            return {}

    def _refresh_presets(self, select: str | None = None):
        self.det_preset.blockSignals(True)
        self.det_preset.clear()
        for name in BUILTIN_PRESETS:
            self.det_preset.addItem(name)
        for name in self._user_presets():
            self.det_preset.addItem(name)
        self.det_preset.setCurrentIndex(max(0, self.det_preset.findText(select or self.settings.value("preset", "Par défaut"))))
        self.det_preset.blockSignals(False)

    def _apply_preset(self, *_):
        name = self.det_preset.currentText()
        values = BUILTIN_PRESETS.get(name) or self._user_presets().get(name)
        if not values:
            return
        for box, v in zip((self.det_threshold, self.det_silence, self.det_minsound, self.det_padding), values):
            box.setValue(float(v))
        self.settings.setValue("preset", name)
        self.statusBar().showMessage(f"Préréglage « {name} » appliqué. Clique sur « Cette piste » pour relancer la détection.", 6000)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Enregistrer un préréglage", "Nom du préréglage :")
        name = name.strip()
        if not ok or not name:
            return
        if name in BUILTIN_PRESETS:
            QMessageBox.information(self, "Nom réservé", "Ce nom est celui d'un préréglage fourni : choisis-en un autre.")
            return
        presets = self._user_presets()
        presets[name] = [self.det_threshold.value(), self.det_silence.value(), self.det_minsound.value(), self.det_padding.value()]
        self.settings.setValue("presets", json.dumps(presets, ensure_ascii=False))
        self.settings.setValue("preset", name)
        self._refresh_presets(name)

    def _delete_preset(self):
        name = self.det_preset.currentText()
        if name in BUILTIN_PRESETS:
            QMessageBox.information(self, "Préréglage fourni", "Les préréglages fournis ne peuvent pas être supprimés.")
            return
        presets = self._user_presets()
        presets.pop(name, None)
        self.settings.setValue("presets", json.dumps(presets, ensure_ascii=False))
        self._refresh_presets("Par défaut")

    # --- Pistes --------------------------------------------------------------

    @property
    def track(self) -> Track | None:
        row = self.track_list.currentRow()
        return self.tracks[row] if 0 <= row < len(self.tracks) else None

    @property
    def template(self) -> str:
        return self.name_template.text().strip() or DEFAULT_TEMPLATE

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.suffix.lower() == SESSION_EXT:
                self.load_session_file(p)
                return
            if p.is_dir():
                paths += sorted(x for x in p.iterdir() if x.suffix.lower() in SUPPORTED_INPUT)
            elif p.suffix.lower() in SUPPORTED_INPUT:
                paths.append(p)
        self.add_files(paths)

    def open_files(self):
        start = self.settings.value("last_open_dir", str(Path.home()))
        exts = " ".join(f"*{e}" for e in SUPPORTED_INPUT)
        files, _ = QFileDialog.getOpenFileNames(self, "Ouvrir des pistes audio", start, f"Audio ({exts});;Tous les fichiers (*)")
        if files:
            self.settings.setValue("last_open_dir", str(Path(files[0]).parent))
            self.add_files([Path(f) for f in files])

    def add_files(self, paths):
        if not paths:
            return
        errors = []
        progress = QProgressDialog("Chargement des pistes…", "Annuler", 0, len(paths), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)
        first_new = len(self.tracks)
        for i, p in enumerate(paths):
            progress.setValue(i)
            progress.setLabelText(f"Chargement de {p.name}…")
            QApplication.processEvents()
            if progress.wasCanceled():
                break
            try:
                t = load_track(p)
            except Exception as exc:  # fichier illisible ou format non pris en charge
                errors.append(f"{p.name} : {exc}")
                continue
            if self.det_auto.isChecked():
                t.segments = detect_segments(t.data, t.samplerate, self._detection_settings())
            self.tracks.append(t)
        progress.setValue(len(paths))
        self._refresh_track_list()
        if len(self.tracks) > first_new:
            self.track_list.setCurrentRow(first_new)
        if errors:
            QMessageBox.warning(self, "Fichiers non chargés", "\n".join(errors))

    def remove_track(self):
        row = self.track_list.currentRow()
        if row < 0:
            return
        self.stop()
        t = self.tracks.pop(row)
        self.undo.pop(id(t), None)
        self.redo.pop(id(t), None)
        self._refresh_track_list()
        self.track_list.setCurrentRow(min(row, len(self.tracks) - 1))
        if not self.tracks:
            self._on_track_changed(-1)

    def _track_label(self, t: Track) -> str:
        return f"{t.path.name}\n{len(t.segments)} sons · {format_time(t.duration)}"

    def _refresh_track_list(self):
        row = self.track_list.currentRow()
        self.track_list.blockSignals(True)
        self.track_list.clear()
        for t in self.tracks:
            item = QListWidgetItem(self._track_label(t))
            item.setToolTip(str(t.path))
            self.track_list.addItem(item)
        self.track_list.setCurrentRow(row if row < len(self.tracks) else len(self.tracks) - 1)
        self.track_list.blockSignals(False)

    def _on_track_changed(self, _row):
        self.stop()
        t = self.track
        self.wave.set_track(t)
        self._updating = True
        if t is not None:
            self.name_title.setText(t.base_title)
            self.name_start.setValue(t.start_index)
            self.name_digits.setValue(t.digits)
            (self.name_unique if t.naming_mode == "unique" else self.name_multi).setChecked(True)
        self._updating = False
        self._refresh_all()

    def _refresh_all(self, *_):
        for t in self.tracks:
            t.number_repeats = self.name_repeats.isChecked()
        self.wave.template = self.template
        self._fill_table()
        t = self.track
        row = self.track_list.currentRow()
        if t is not None and row >= 0:
            self.track_list.item(row).setText(self._track_label(t))
        has_track = t is not None
        for w in (self.btn_paste, self.btn_variations, self.det_auto_threshold, self.btn_export, self.a_export, self.a_remove, self.a_play_all, self.a_play_track, self.a_loop):
            w.setEnabled(has_track)
        self.btn_export_all.setEnabled(len(self.tracks) > 0)
        self.a_export_all.setEnabled(len(self.tracks) > 0)
        if t is None:
            self.statusBar().showMessage(f"{len(self.tracks)} piste(s) ouverte(s).")
        else:
            self.statusBar().showMessage(
                f"{t.path.name} · {t.samplerate} Hz · {t.channels} voie(s) · {format_time(t.duration)} · {len(t.segments)} sons"
            )
        self.wave.update()

    # --- Tableau des sons ----------------------------------------------------

    def _fill_table(self):
        self._updating = True
        t = self.track
        segs = t.segments if t else []
        names = t.resolved_names(self.template) if t else []
        editable = t is not None and t.naming_mode == "multiple"
        self.table.setRowCount(len(segs))
        for r, seg in enumerate(segs):
            sr = t.samplerate
            cells = [str(r + 1), format_time(seg.start / sr), format_time(seg.end / sr), f"{seg.length / sr:.3f} s"]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self.table.setItem(r, c, item)
            name_item = QTableWidgetItem(seg.name if editable and seg.name.strip() else names[r])
            flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
            if editable:
                flags |= Qt.ItemIsEditable
                if not seg.name.strip():
                    name_item.setForeground(Qt.gray)
            if editable and seg.name.strip() and names[r] != seg.name.strip():
                name_item.setToolTip(f"Nom du fichier : {names[r]}")  # noms répétés numérotés, doublons
            name_item.setFlags(flags)
            self.table.setItem(r, COL_NAME, name_item)
            loop_item = QTableWidgetItem("")
            loop_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            loop_item.setCheckState(Qt.Checked if seg.loop else Qt.Unchecked)
            loop_item.setToolTip("Boucle sans coupure (L)")
            self.table.setItem(r, COL_LOOP, loop_item)
        if 0 <= self.wave.selected < len(segs):
            self.table.selectRow(self.wave.selected)
        self._updating = False

    def _on_table_selection(self):
        if self._updating:
            return
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.wave.select(rows[0].row())

    def _on_table_edit(self, item):
        if self._updating or self.track is None:
            return
        if item.column() == COL_LOOP:
            seg = self.track.segments[item.row()]
            if seg.loop != (item.checkState() == Qt.Checked):
                self._push_undo()
                seg.loop = item.checkState() == Qt.Checked
                self.wave.update()
            return
        if item.column() != COL_NAME:
            return
        self._push_undo()
        self.track.segments[item.row()].name = sanitize_filename(item.text()) if item.text().strip() else ""
        self._fill_table()
        self.wave.update()

    def _on_wave_selection(self, idx):
        self._updating = True
        if idx >= 0:
            self.table.selectRow(idx)
            self.table.scrollToItem(self.table.item(idx, 0))
        else:
            self.table.clearSelection()
        self._updating = False

    # --- Annuler / rétablir --------------------------------------------------

    def _push_undo(self):
        t = self.track
        if t is None:
            return
        key = id(t)
        self.undo.setdefault(key, []).append(copy.deepcopy(t.segments))
        del self.undo[key][:-100]
        self.redo.pop(key, None)

    def undo_edit(self):
        self._swap(self.undo, self.redo)

    def redo_edit(self):
        self._swap(self.redo, self.undo)

    def _swap(self, src, dst):
        t = self.track
        if t is None or not src.get(id(t)):
            return
        dst.setdefault(id(t), []).append(copy.deepcopy(t.segments))
        t.segments[:] = src[id(t)].pop()
        self.wave.select(min(self.wave.selected, len(t.segments) - 1), ensure_visible=False)
        self._refresh_all()

    def delete_selected(self):
        if self.table.state() == QAbstractItemView.EditingState:
            return
        self.wave.delete(self.wave.selected)

    # --- Détection et nommage ------------------------------------------------

    def _detection_settings(self) -> DetectionSettings:
        return DetectionSettings(
            threshold_db=self.det_threshold.value(),
            min_silence_ms=self.det_silence.value(),
            min_sound_ms=self.det_minsound.value(),
            padding_ms=self.det_padding.value(),
        )

    def detect(self, all_tracks: bool):
        targets = self.tracks if all_tracks else ([self.track] if self.track else [])
        if not targets:
            return
        settings = self._detection_settings()
        current = self.track
        for t in targets:
            key = id(t)
            self.undo.setdefault(key, []).append(copy.deepcopy(t.segments))
            self.redo.pop(key, None)
            t.segments = detect_segments(t.data, t.samplerate, settings)
        self._refresh_track_list()
        self.wave.set_track(current)
        self._refresh_all()
        self.statusBar().showMessage("Détection relancée. Ctrl+Z pour revenir au découpage précédent.", 6000)

    def auto_threshold(self):
        t = self.track
        if t is None:
            return
        db = estimate_threshold(t.data, t.samplerate)
        self.det_threshold.setValue(db)
        self.detect(all_tracks=False)
        self.statusBar().showMessage(f"Seuil réglé à {db:.0f} dB d'après le bruit de fond, détection relancée.", 6000)

    def find_variations(self):
        t = self.track
        if t is None or not t.segments:
            return
        groups = group_variations(t.data, t.samplerate, t.segments)
        self._push_undo()
        # Une lettre par type de son, dans l'ordre : les variantes partagent la leur
        letters: dict[str, str] = {}
        for i, (seg, g) in enumerate(zip(t.segments, groups)):
            key = f"g{g}" if g >= 0 else f"s{i}"
            if key not in letters:
                k = len(letters)
                letters[key] = LETTERS[k] if k < 26 else LETTERS[k // 26 - 1] + LETTERS[k % 26]
            seg.name = f"{t.base_title or sanitize_filename(t.path.stem)}_{letters[key]}"
        count = len({g for g in groups if g >= 0})
        self.name_multi.setChecked(True)
        self.name_repeats.setChecked(True)
        self._on_naming_changed()
        self.statusBar().showMessage(
            f"{count} groupe(s) de variantes : les sons qui se ressemblent portent le même nom. Renomme-les dans le tableau."
            if count else "Aucune variante trouvée : chaque son est différent.", 10000)

    def toggle_loop(self):
        t = self.track
        idx = self.wave.selected
        if t is None or not 0 <= idx < len(t.segments) or self.table.state() == QAbstractItemView.EditingState:
            return
        self._push_undo()
        t.segments[idx].loop = not t.segments[idx].loop
        self._fill_table()
        self.wave.update()

    def _on_naming_changed(self, *_):
        if self._updating or self.track is None:
            return
        t = self.track
        t.naming_mode = "unique" if self.name_unique.isChecked() else "multiple"
        t.base_title = sanitize_filename(self.name_title.text()) if self.name_title.text().strip() else sanitize_filename(t.path.stem)
        t.start_index = self.name_start.value()
        t.digits = self.name_digits.value()
        self._refresh_all()

    def _on_template_changed(self):
        self.settings.setValue("template", self.template)
        self._refresh_all()

    def paste_names(self):
        t = self.track
        if t is None:
            return
        current = [s.name for s in t.segments if s.name.strip()]
        dlg = NamesDialog(self, len(t.segments), current)
        if dlg.exec() != QDialog.Accepted:
            return
        names = dlg.names()
        self._push_undo()
        for i, seg in enumerate(t.segments):
            seg.name = names[i] if i < len(names) else ""
        self.name_multi.setChecked(True)
        self._on_naming_changed()
        if len(names) != len(t.segments):
            self.statusBar().showMessage(f"{len(names)} noms collés pour {len(t.segments)} sons : vérifie l'ordre dans le tableau.", 8000)

    # --- Lecture -------------------------------------------------------------

    def _play_clip(self, data, samplerate: int, offset: int):
        self.player.stop()
        self.player.setSource(QUrl())
        self._tmpcount += 1
        path = Path(self._tmpdir.name) / f"preview_{self._tmpcount % 4}.wav"
        try:
            sf.write(str(path), data, samplerate, subtype="FLOAT")
        except OSError:
            # Fichier encore verrouillé par le lecteur : on en prend un autre
            self._tmpcount += 1
            path = Path(self._tmpdir.name) / f"preview_{self._tmpcount % 4}.wav"
            sf.write(str(path), data, samplerate, subtype="FLOAT")
        self._play_offset = offset
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def play_segment(self, idx: int, keep_queue: bool = False):
        t = self.track
        if not (t and 0 <= idx < len(t.segments)):
            return
        if not keep_queue:
            self._queue = []
        seg = t.segments[idx]
        if self.exp_preview.isChecked() or seg.loop:
            settings = self._export_settings() if self.exp_preview.isChecked() else ExportSettings(fade_out_ms=0, loop_crossfade_ms=self.exp_loop_fade.value())
            params = variation_params(settings.variants, settings.variant_pitch, settings.variant_volume, seg.start % 2147483647) if self.exp_preview.isChecked() else []
            # L'aperçu tire au hasard l'original ou une variation, comme le ferait un jeu
            pick = random.randint(0, len(params))
            if pick:
                settings.pitch_semitones, settings.gain_db = params[pick - 1]
            clip, sr = process_segment(t.data, t.samplerate, seg, settings)
            if seg.loop:
                clip = np.tile(clip, (3, 1))  # trois tours pour entendre la jonction
            self._play_clip(clip, sr, seg.start)
        else:
            self._play_clip(t.data[seg.start : seg.end], t.samplerate, seg.start)

    def play_selected(self):
        if self.table.state() == QAbstractItemView.EditingState:
            return
        if self.wave.selected >= 0:
            self.play_segment(self.wave.selected)
        else:
            self.play_track()

    def play_track(self):
        t = self.track
        if t:
            self._queue = []
            start = self.wave.playhead or 0
            self._play_clip(t.data[start:], t.samplerate, start)

    def play_all(self):
        t = self.track
        if not t or not t.segments:
            return
        first = max(0, self.wave.selected)
        self._queue = list(range(first + 1, len(t.segments)))
        self._select(first)
        self.play_segment(first, keep_queue=True)

    def step(self, delta: int):
        t = self.track
        if not t or not t.segments:
            return
        idx = min(len(t.segments) - 1, max(0, self.wave.selected + delta)) if self.wave.selected >= 0 else 0
        self._select(idx)
        self.play_segment(idx)

    def stop(self):
        self._queue = []
        self.player.stop()

    def _select(self, idx: int):
        self.wave.select(idx)
        self._on_wave_selection(idx)

    def _on_media_status(self, status):
        if status == QMediaPlayer.EndOfMedia and self._queue:
            nxt = self._queue.pop(0)
            self._select(nxt)
            QTimer.singleShot(150, lambda: self.play_segment(nxt, keep_queue=True))

    def _on_play_position(self, ms):
        t = self.track
        if t is None:
            return
        self.wave.playhead = self._play_offset + int(ms * t.samplerate / 1000)
        self.time_label.setText(format_time(self.wave.playhead / t.samplerate))
        self.wave.update()

    # --- Sessions ------------------------------------------------------------

    def save_session(self, ask: bool = False):
        if not self.tracks:
            return
        path = self.session_path
        if ask or path is None:
            start = str(path or Path(self.settings.value("last_open_dir", str(Path.home()))) / "session.autocut")
            name, _ = QFileDialog.getSaveFileName(self, "Enregistrer la session", start, f"Session Autocut (*{SESSION_EXT})")
            if not name:
                return
            path = Path(name).with_suffix(SESSION_EXT)
        save_session(path, self.tracks, {"template": self.template})
        self.session_path = path
        self.setWindowTitle(f"Autocut {__version__} · {path.stem}")
        self.statusBar().showMessage(f"Session enregistrée : {path}", 6000)

    def open_session(self):
        start = self.settings.value("last_open_dir", str(Path.home()))
        name, _ = QFileDialog.getOpenFileName(self, "Ouvrir une session", start, f"Session Autocut (*{SESSION_EXT})")
        if name:
            self.load_session_file(Path(name))

    def load_session_file(self, path: Path):
        if self.tracks and QMessageBox.question(
            self, "Ouvrir une session", "Les pistes ouvertes seront remplacées par celles de la session. Continuer ?"
        ) != QMessageBox.Yes:
            return
        try:
            tracks, settings, errors = load_session(path)
        except Exception as exc:
            QMessageBox.critical(self, "Session illisible", str(exc))
            return
        self.stop()
        self.tracks = tracks
        self.undo.clear()
        self.redo.clear()
        if settings.get("template"):
            self.name_template.setText(settings["template"])
        self.session_path = path
        self.setWindowTitle(f"Autocut {__version__} · {path.stem}")
        self._refresh_track_list()
        self.track_list.setCurrentRow(0 if tracks else -1)
        self._on_track_changed(0)
        if errors:
            QMessageBox.warning(self, "Pistes introuvables", "Ces pistes n'ont pas pu être rechargées :\n" + "\n".join(errors))

    # --- Export --------------------------------------------------------------

    def _on_format_changed(self, fmt):
        self.exp_wav.setEnabled(fmt == "WAV")
        self.exp_mp3.setEnabled(fmt == "MP3")

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Dossier d'export", self.exp_dir.text())
        if d:
            self.exp_dir.setText(d)

    def _export_settings(self) -> ExportSettings:
        return ExportSettings(
            fmt=self.exp_format.currentText().lower(),
            samplerate=SAMPLERATES[self.exp_rate.currentIndex()][1],
            mono=self.exp_mono.isChecked(),
            normalize=self.exp_norm.isChecked(),
            normalize_db=self.exp_norm_db.value(),
            fade_in_ms=self.exp_fade_in.value(),
            fade_out_ms=self.exp_fade_out.value(),
            wav_subtype=WAV_DEPTHS[self.exp_wav.currentIndex()][1],
            mp3_quality=MP3_QUALITIES[self.exp_mp3.currentIndex()][1],
            normalize_mode=NORM_MODES[self.exp_norm_mode.currentIndex()][1],
            normalize_lufs=self.exp_norm_lufs.value(),
            highpass_hz=HIGHPASS[self.exp_highpass.currentIndex()][1],
            trim=self.exp_trim.isChecked(),
            trim_db=self.det_threshold.value(),
            fade_curve=FADE_CURVES[self.exp_curve.currentIndex()][1],
            loop_crossfade_ms=self.exp_loop_fade.value(),
            variants=self.exp_variants.value(),
            variant_pitch=self.exp_variant_pitch.value(),
            variant_volume=self.exp_variant_volume.value(),
        )

    def _on_norm_mode(self, *_):
        lufs = NORM_MODES[self.exp_norm_mode.currentIndex()][1] == "lufs"
        self.exp_norm_db.setVisible(not lufs)
        self.exp_norm_lufs.setVisible(lufs)

    def export_current(self):
        if self.track:
            self._export([self.track])

    def export_all(self):
        self._export(self.tracks)

    def _export(self, tracks):
        tracks = [t for t in tracks if t.segments]
        if not tracks:
            QMessageBox.information(self, "Rien à exporter", "Aucun son à exporter : lance la détection ou dessine des sons sur la forme d'onde.")
            return
        out = self.exp_dir.text().strip()
        if not out:
            self.choose_dir()
            out = self.exp_dir.text().strip()
            if not out:
                return
        settings = self._export_settings()
        total = sum(len(t.segments) for t in tracks) * (1 + settings.variants)
        progress = QProgressDialog("Export…", "Annuler", 0, len(tracks), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(200)
        exported = []
        try:
            for i, t in enumerate(tracks):
                progress.setValue(i)
                progress.setLabelText(f"Export de {t.path.name} ({len(t.segments)} sons)…")
                QApplication.processEvents()
                if progress.wasCanceled():
                    break
                target = Path(out) / sanitize_filename(t.base_title or t.path.stem) if self.exp_subdir.isChecked() else Path(out)
                files = export_track(t, target, settings, overwrite=self.exp_overwrite.isChecked(), template=self.template)
                exported.append((t, files))
            if self.exp_csv.isChecked() and exported:
                write_listing(Path(out) / LISTING_NAME, exported, variants=settings.variants)
        except Exception as exc:
            progress.cancel()
            done = sum(len(f) for _, f in exported)
            QMessageBox.critical(self, "Erreur d'export", f"{exc}\n\n{done} fichier(s) exporté(s) avant l'erreur.")
            return
        progress.setValue(len(tracks))
        self._save_settings()
        done = sum(len(f) for _, f in exported)
        self.statusBar().showMessage(f"{done} fichier(s) exporté(s) sur {total} dans {out}", 10000)
        if self.exp_open.isChecked() and done:
            QDesktopServices.openUrl(QUrl.fromLocalFile(out))

    # --- Aide ----------------------------------------------------------------

    def show_help(self):
        QMessageBox.information(
            self,
            "Aide Autocut",
            "<b>1. Ouvrir</b> : glisse des fichiers (ou un dossier) dans la fenêtre, ou Ctrl+O.<br>"
            "<b>2. Détecter</b> : les sons séparés par du silence sont repérés automatiquement. "
            "Si deux sons sont collés, baisse le « silence minimum » ; si un son est coupé en morceaux, augmente-le. "
            "Si le bruit de fond est détecté comme un son, monte le seuil. Enregistre tes réglages en préréglage.<br>"
            "<b>3. Ajuster</b> : glisse les bords d'un son, glisse dans une zone vide pour en créer un, "
            "clic droit pour couper, fusionner ou supprimer. Ctrl+Z annule.<br>"
            "<b>4. Écouter</b> : clic sur un son ou Espace, ↑/↓ pour passer d'un son à l'autre, Ctrl+Espace pour tout écouter.<br>"
            "<b>5. Nommer</b> : un titre numéroté ou un nom par son. Le modèle (ex. SFX_{titre}_{n}) s'applique à toutes les pistes. "
            "« Repérer les variantes » donne le même nom aux sons qui se ressemblent ; les noms répétés sont numérotés (pas_01, pas_02).<br>"
            "<b>6. Exporter</b> : WAV, MP3 ou OGG, mono, fréquence, normalisation en crête ou en LUFS, coupe-bas, rognage, fondus "
            "(linéaires, doux ou rapides) et liste CSV en option. « Variations par son » ajoute des versions plus aiguës ou graves "
            "(porte_v1, porte_v2…) pour éviter la répétition dans un jeu.<br>"
            "<b>Boucles</b> : coche « Boucle » dans le tableau (ou touche L) pour une ambiance : la fin du son est fondue sur son début, "
            "sans clic. Une boucle s'écoute trois fois de suite.<br>"
            "<b>Session</b> : Ctrl+S enregistre ton découpage pour le reprendre plus tard.<br><br>"
            "Raccourcis : Espace écouter · Échap stop · ↑/↓ son précédent/suivant · Suppr supprimer · L boucle · Ctrl+Z / Ctrl+Y · "
            "molette zoom · Maj+molette défiler · double-clic zoom sur un son · Ctrl+0 tout afficher.",
        )

    def show_about(self):
        QMessageBox.about(self, "À propos d'Autocut", f"<b>Autocut {__version__}</b><br>Découpe, nommage et export de bruitages.")

    # --- Défilement ----------------------------------------------------------

    def _sync_scrollbar(self):
        span = self.wave.view_end - self.wave.view_start
        self.scroll.blockSignals(True)
        self.scroll.setRange(0, max(0, self.wave.total - span))
        self.scroll.setPageStep(max(1, span))
        self.scroll.setSingleStep(max(1, span // 20))
        self.scroll.setValue(self.wave.view_start)
        self.scroll.blockSignals(False)

    def _on_scroll(self, value):
        span = self.wave.view_end - self.wave.view_start
        self.wave.set_view(value, value + span)


def main():
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Autocut.Autocut")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("Autocut")
    apply_theme(app)
    app.setWindowIcon(app_icon())

    def excepthook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        print(text, file=sys.stderr)
        QMessageBox.critical(None, "Erreur inattendue", text[-2000:])

    sys.excepthook = excepthook
    win = MainWindow()
    win.show()
    if "--selftest" in sys.argv:
        # Utilisé par la CI : vérifie que l'exe démarre (tous les modules présents) puis quitte.
        missing = [n for n in ("autocut.ico", "chevron.svg", "check.svg") if not asset_path(n).exists()]
        if missing:
            print("Fichiers manquants dans l'exe :", ", ".join(missing), file=sys.stderr)
            sys.exit(2)
        QTimer.singleShot(1500, win.close)
        sys.exit(app.exec())
    args = [Path(a) for a in sys.argv[1:]]
    sessions = [a for a in args if a.suffix.lower() == SESSION_EXT]
    if sessions:
        win.load_session_file(sessions[0])
    else:
        win.add_files([a for a in args if a.suffix.lower() in SUPPORTED_INPUT])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
