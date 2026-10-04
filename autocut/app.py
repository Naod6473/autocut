"""Fenêtre principale d'Autocut."""

from __future__ import annotations

import copy
import sys
import tempfile
import traceback
from pathlib import Path

import soundfile as sf
from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
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
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .core import (
    SUPPORTED_INPUT,
    DetectionSettings,
    ExportSettings,
    Track,
    detect_segments,
    export_track,
    format_time,
    load_track,
    names_from_text,
    sanitize_filename,
)
from .waveform import WaveformView

SAMPLERATES = [("Fréquence d'origine", None), ("22 050 Hz", 22050), ("44 100 Hz", 44100), ("48 000 Hz", 48000)]
MP3_QUALITIES = [("Haute", 0.0), ("Normale", 0.3), ("Légère", 0.6)]
WAV_DEPTHS = [("16 bits", "PCM_16"), ("24 bits", "PCM_24"), ("32 bits flottant", "FLOAT")]


def _spin(minimum, maximum, value, step=1.0, suffix="", decimals=0):
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setValue(value)
    box.setSuffix(suffix)
    return box


class NamesDialog(QDialog):
    def __init__(self, parent, count: int, current: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Coller une liste de noms")
        self.resize(420, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Un nom par ligne, dans l'ordre des sons ({count} sons sur cette piste).\nLes sons sans nom gardent le titre numéroté."))
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
        self.setWindowTitle(f"Autocut {__version__} · découpe de bruitages")
        self.resize(1280, 800)
        self.setAcceptDrops(True)
        self.settings = QSettings("Autocut", "Autocut")
        self.tracks: list[Track] = []
        self.undo: dict[int, list] = {}
        self.redo: dict[int, list] = {}
        self._updating = False
        self._play_offset = 0
        self._play_end = 0
        self._tmpdir = tempfile.TemporaryDirectory(prefix="autocut_")
        self._tmpcount = 0

        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.player.positionChanged.connect(self._on_play_position)
        self.player.playbackStateChanged.connect(self._on_play_state)

        self._build_ui()
        self._load_settings()
        self._refresh_all()

    # --- Construction de l'interface ----------------------------------------

    def _build_ui(self):
        tb = QToolBar("Actions")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)

        def action(text, slot, shortcut=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
            tb.addAction(a)
            return a

        action("Ouvrir des pistes…", self.open_files, "Ctrl+O")
        action("Retirer la piste", self.remove_track)
        tb.addSeparator()
        self.act_play = action("▶ Écouter", self.play_selected, "Space", "Écoute le son sélectionné (Espace)")
        action("▶ Piste entière", self.play_track)
        action("■ Stop", self.player.stop, "Escape")
        tb.addSeparator()
        action("Annuler", self.undo_edit, "Ctrl+Z")
        action("Rétablir", self.redo_edit, "Ctrl+Y")
        action("Supprimer le son", self.delete_selected, "Delete")
        tb.addSeparator()
        action("Zoom +", lambda: self.wave.zoom(0.5), "Ctrl++")
        action("Zoom −", lambda: self.wave.zoom(2.0), "Ctrl+-")
        action("Tout afficher", lambda: self.wave.zoom_fit(), "Ctrl+0")
        tb.addSeparator()
        action("Exporter la piste", self.export_current, "Ctrl+E")
        action("Exporter toutes les pistes", self.export_all, "Ctrl+Shift+E")
        tb.addSeparator()
        action("Aide", self.show_help, "F1")

        # Liste des pistes
        self.track_list = QListWidget()
        self.track_list.currentRowChanged.connect(self._on_track_changed)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        ll.addWidget(QLabel("<b>Pistes</b>"))
        ll.addWidget(self.track_list)

        # Forme d'onde + tableau
        self.wave = WaveformView()
        self.wave.selectionChanged.connect(self._on_wave_selection)
        self.wave.aboutToEdit.connect(self._push_undo)
        self.wave.segmentsEdited.connect(self._on_segments_edited)
        self.wave.playRequested.connect(self.play_segment)
        self.wave.viewChanged.connect(self._sync_scrollbar)
        self.scroll = QScrollBar(Qt.Horizontal)
        self.scroll.valueChanged.connect(self._on_scroll)
        self.hint = QLabel(
            "Clic sur un son : écouter · Glisser un bord : ajuster · Glisser dans le vide : nouveau son · "
            "Clic droit : couper, fusionner, supprimer · Molette : zoom · Maj+molette : défiler"
        )
        self.hint.setStyleSheet("color: gray;")
        self.hint.setWordWrap(True)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Début", "Fin", "Durée", "Nom du fichier"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        for c in range(4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.itemChanged.connect(self._on_table_edit)
        self.table.cellDoubleClicked.connect(lambda r, c: self.play_segment(r) if c < 4 else None)

        center = QSplitter(Qt.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.wave, 1)
        tl.addWidget(self.scroll)
        tl.addWidget(self.hint)
        center.addWidget(top)
        center.addWidget(self.table)
        center.setSizes([450, 300])

        # Panneau de réglages
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.addWidget(self._build_detection())
        sl.addWidget(self._build_naming())
        sl.addWidget(self._build_export())
        sl.addStretch(1)
        scroll_side = QScrollArea()
        scroll_side.setWidget(side)
        scroll_side.setWidgetResizable(True)
        scroll_side.setMinimumWidth(320)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(center)
        split.addWidget(scroll_side)
        split.setSizes([200, 760, 340])
        self.setCentralWidget(split)
        self.statusBar().showMessage("Ouvre ou glisse une ou plusieurs pistes pour commencer.")

    def _build_detection(self):
        box = QGroupBox("1. Détection des sons")
        f = QFormLayout(box)
        self.det_threshold = _spin(-90, -5, -45, 1, " dB")
        self.det_threshold.setToolTip("Tout ce qui est plus faible est considéré comme du silence. Monte-le si le bruit de fond est fort.")
        self.det_silence = _spin(10, 5000, 250, 10, " ms")
        self.det_silence.setToolTip("Un silence plus court ne sépare pas deux sons (utile pour les sons avec rebonds ou échos).")
        self.det_minsound = _spin(0, 5000, 60, 10, " ms")
        self.det_minsound.setToolTip("Les sons plus courts sont ignorés (clics, bruits parasites).")
        self.det_padding = _spin(0, 1000, 20, 5, " ms")
        self.det_padding.setToolTip("Marge de sécurité gardée avant et après chaque son pour ne pas couper l'attaque ou la queue.")
        f.addRow("Seuil de silence", self.det_threshold)
        f.addRow("Silence minimum", self.det_silence)
        f.addRow("Son minimum", self.det_minsound)
        f.addRow("Marge", self.det_padding)
        row = QHBoxLayout()
        b1 = QPushButton("Détecter (piste)")
        b1.clicked.connect(lambda: self.detect(all_tracks=False))
        b2 = QPushButton("Détecter (toutes)")
        b2.clicked.connect(lambda: self.detect(all_tracks=True))
        row.addWidget(b1)
        row.addWidget(b2)
        f.addRow(row)
        self.det_auto = QCheckBox("Détecter automatiquement à l'ouverture")
        self.det_auto.setChecked(True)
        f.addRow(self.det_auto)
        return box

    def _build_naming(self):
        box = QGroupBox("2. Nommage")
        v = QVBoxLayout(box)
        self.name_unique = QRadioButton("Titre unique numéroté (Porte_01, Porte_02…)")
        self.name_multi = QRadioButton("Un nom par son")
        group = QButtonGroup(box)
        group.addButton(self.name_unique)
        group.addButton(self.name_multi)
        self.name_unique.setChecked(True)
        self.name_unique.toggled.connect(self._on_naming_changed)
        v.addWidget(self.name_unique)
        v.addWidget(self.name_multi)

        f = QFormLayout()
        self.name_title = QLineEdit()
        self.name_title.setPlaceholderText("Titre de la piste")
        self.name_title.editingFinished.connect(self._on_naming_changed)
        self.name_start = QSpinBox()
        self.name_start.setRange(0, 99999)
        self.name_start.setValue(1)
        self.name_start.valueChanged.connect(self._on_naming_changed)
        self.name_digits = QSpinBox()
        self.name_digits.setRange(1, 5)
        self.name_digits.setValue(2)
        self.name_digits.valueChanged.connect(self._on_naming_changed)
        f.addRow("Titre", self.name_title)
        f.addRow("Commencer à", self.name_start)
        f.addRow("Chiffres", self.name_digits)
        v.addLayout(f)

        self.btn_paste = QPushButton("Coller une liste de noms…")
        self.btn_paste.clicked.connect(self.paste_names)
        v.addWidget(self.btn_paste)
        tip = QLabel("En mode « un nom par son », double-clique sur un nom dans le tableau pour le modifier.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        v.addWidget(tip)
        return box

    def _build_export(self):
        box = QGroupBox("3. Export")
        f = QFormLayout(box)
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
        self.exp_norm = QCheckBox("Normaliser le volume")
        self.exp_norm_db = _spin(-30, 0, -1, 0.5, " dBFS", 1)
        self.exp_fade_in = _spin(0, 2000, 0, 1, " ms")
        self.exp_fade_out = _spin(0, 5000, 5, 1, " ms")
        self.exp_dir = QLineEdit()
        browse = QPushButton("…")
        browse.setFixedWidth(30)
        browse.clicked.connect(self.choose_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.exp_dir)
        dir_row.addWidget(browse)
        self.exp_subdir = QCheckBox("Un sous-dossier par piste")
        self.exp_overwrite = QCheckBox("Remplacer les fichiers existants")
        self.exp_overwrite.setToolTip("Sinon, un fichier déjà présent est conservé et le nouveau reçoit un suffixe _2, _3…")
        self.exp_open = QCheckBox("Ouvrir le dossier après l'export")
        self.exp_open.setChecked(True)

        f.addRow("Format", self.exp_format)
        f.addRow("Profondeur WAV", self.exp_wav)
        f.addRow("Qualité MP3", self.exp_mp3)
        f.addRow("Fréquence", self.exp_rate)
        f.addRow(self.exp_mono)
        norm_row = QHBoxLayout()
        norm_row.addWidget(self.exp_norm)
        norm_row.addWidget(self.exp_norm_db)
        f.addRow(norm_row)
        f.addRow("Fondu d'entrée", self.exp_fade_in)
        f.addRow("Fondu de sortie", self.exp_fade_out)
        f.addRow("Dossier", dir_row)
        f.addRow(self.exp_subdir)
        f.addRow(self.exp_overwrite)
        f.addRow(self.exp_open)
        return box

    # --- Réglages persistants -----------------------------------------------

    _persisted = [
        ("det_threshold", "value", float),
        ("det_silence", "value", float),
        ("det_minsound", "value", float),
        ("det_padding", "value", float),
        ("exp_norm_db", "value", float),
        ("exp_fade_in", "value", float),
        ("exp_fade_out", "value", float),
        ("exp_format", "currentIndex", int),
        ("exp_wav", "currentIndex", int),
        ("exp_mp3", "currentIndex", int),
        ("exp_rate", "currentIndex", int),
        ("name_digits", "value", int),
    ]
    _persisted_checks = ["det_auto", "exp_mono", "exp_norm", "exp_subdir", "exp_overwrite", "exp_open"]

    def _load_settings(self):
        s = self.settings
        for name, getter, kind in self._persisted:
            if s.contains(name):
                widget = getattr(self, name)
                setter = "set" + getter[0].upper() + getter[1:]
                getattr(widget, setter)(kind(s.value(name)))
        for name in self._persisted_checks:
            if s.contains(name):
                getattr(self, name).setChecked(str(s.value(name)).lower() == "true")
        self.exp_dir.setText(s.value("exp_dir", str(Path.home() / "Music" / "Autocut")))
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self._on_format_changed(self.exp_format.currentText())

    def _save_settings(self):
        s = self.settings
        for name, getter, _ in self._persisted:
            s.setValue(name, getattr(getattr(self, name), getter)())
        for name in self._persisted_checks:
            s.setValue(name, getattr(self, name).isChecked())
        s.setValue("exp_dir", self.exp_dir.text())
        s.setValue("geometry", self.saveGeometry())

    def closeEvent(self, e):
        self.player.stop()
        self._save_settings()
        super().closeEvent(e)

    # --- Pistes --------------------------------------------------------------

    @property
    def track(self) -> Track | None:
        row = self.track_list.currentRow()
        return self.tracks[row] if 0 <= row < len(self.tracks) else None

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
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
        self.player.stop()
        del self.tracks[row]
        self.undo.clear()
        self.redo.clear()
        self._refresh_track_list()
        self.track_list.setCurrentRow(min(row, len(self.tracks) - 1))
        if not self.tracks:
            self._on_track_changed(-1)

    def _refresh_track_list(self):
        row = self.track_list.currentRow()
        self.track_list.blockSignals(True)
        self.track_list.clear()
        for t in self.tracks:
            item = QListWidgetItem(f"{t.path.name}\n{len(t.segments)} sons · {format_time(t.duration)}")
            item.setToolTip(str(t.path))
            self.track_list.addItem(item)
        self.track_list.setCurrentRow(row if row < len(self.tracks) else len(self.tracks) - 1)
        self.track_list.blockSignals(False)

    def _update_track_item(self):
        row = self.track_list.currentRow()
        t = self.track
        if t is not None:
            self.track_list.item(row).setText(f"{t.path.name}\n{len(t.segments)} sons · {format_time(t.duration)}")

    def _on_track_changed(self, _row):
        self.player.stop()
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

    def _refresh_all(self):
        self._fill_table()
        self._update_track_item()
        self.btn_paste.setEnabled(self.track is not None)
        t = self.track
        if t is None:
            self.statusBar().showMessage(f"{len(self.tracks)} piste(s) ouverte(s).")
        else:
            self.statusBar().showMessage(
                f"{t.path.name} · {t.samplerate} Hz · {t.channels} voie(s) · {format_time(t.duration)} · {len(t.segments)} sons détectés"
            )
        self.wave.update()

    # --- Tableau des sons ----------------------------------------------------

    def _fill_table(self):
        self._updating = True
        t = self.track
        segs = t.segments if t else []
        names = t.resolved_names() if t else []
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
            name_item.setFlags(flags)
            self.table.setItem(r, 4, name_item)
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
        if self._updating or item.column() != 4 or self.track is None:
            return
        r = item.row()
        self._push_undo()
        self.track.segments[r].name = sanitize_filename(item.text()) if item.text().strip() else ""
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

    def _on_segments_edited(self):
        self._refresh_all()

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
        edited = [t for t in targets if t.segments]
        if edited and QMessageBox.question(
            self, "Relancer la détection", "La détection remplace les sons actuels (annulable avec Ctrl+Z). Continuer ?"
        ) != QMessageBox.Yes:
            return
        settings = self._detection_settings()
        current = self.track
        for t in targets:
            key = id(t)
            self.undo.setdefault(key, []).append(copy.deepcopy(t.segments))
            t.segments = detect_segments(t.data, t.samplerate, settings)
        self._refresh_track_list()
        self.wave.set_track(current)
        self._refresh_all()

    def _on_naming_changed(self, *_):
        if self._updating or self.track is None:
            return
        t = self.track
        t.naming_mode = "unique" if self.name_unique.isChecked() else "multiple"
        t.base_title = sanitize_filename(self.name_title.text()) if self.name_title.text().strip() else sanitize_filename(t.path.stem)
        t.start_index = self.name_start.value()
        t.digits = self.name_digits.value()
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

    def _play_range(self, start: int, end: int):
        t = self.track
        if t is None or end <= start:
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self._tmpcount += 1
        path = Path(self._tmpdir.name) / f"preview_{self._tmpcount % 4}.wav"
        sf.write(str(path), t.data[start:end], t.samplerate, subtype="FLOAT")
        self._play_offset, self._play_end = start, end
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def play_segment(self, idx: int):
        t = self.track
        if t and 0 <= idx < len(t.segments):
            self._play_range(t.segments[idx].start, t.segments[idx].end)

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
            start = self.wave.playhead or 0
            self._play_range(start, len(t.data))

    def _on_play_position(self, ms):
        t = self.track
        if t is None:
            return
        self.wave.playhead = self._play_offset + int(ms * t.samplerate / 1000)
        self.wave.update()

    def _on_play_state(self, state):
        if state == QMediaPlayer.StoppedState:
            QTimer.singleShot(0, self.wave.update)

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
        )

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
        total = sum(len(t.segments) for t in tracks)
        progress = QProgressDialog("Export…", "Annuler", 0, len(tracks), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(200)
        written = []
        try:
            for i, t in enumerate(tracks):
                progress.setValue(i)
                progress.setLabelText(f"Export de {t.path.name} ({len(t.segments)} sons)…")
                QApplication.processEvents()
                if progress.wasCanceled():
                    break
                target = Path(out) / sanitize_filename(t.base_title or t.path.stem) if self.exp_subdir.isChecked() else Path(out)
                written += export_track(t, target, settings, overwrite=self.exp_overwrite.isChecked())
        except Exception as exc:
            progress.cancel()
            QMessageBox.critical(self, "Erreur d'export", f"{exc}\n\n{len(written)} fichier(s) exporté(s) avant l'erreur.")
            return
        progress.setValue(len(tracks))
        self._save_settings()
        self.statusBar().showMessage(f"{len(written)} fichier(s) exporté(s) sur {total} dans {out}", 10000)
        if self.exp_open.isChecked() and written:
            QDesktopServices.openUrl(QUrl.fromLocalFile(out))

    # --- Aide ----------------------------------------------------------------

    def show_help(self):
        QMessageBox.information(
            self,
            "Aide Autocut",
            "<b>1. Ouvrir</b> : glisse des fichiers (ou un dossier) dans la fenêtre, ou Ctrl+O.<br>"
            "<b>2. Détecter</b> : les sons séparés par du silence sont repérés automatiquement. "
            "Si deux sons sont collés, baisse le « silence minimum » ; si un son est coupé en morceaux, augmente-le. "
            "Si le bruit de fond est détecté comme un son, monte le seuil.<br>"
            "<b>3. Ajuster</b> : glisse les bords d'un son, glisse dans une zone vide pour en créer un, "
            "clic droit pour couper, fusionner ou supprimer. Ctrl+Z annule.<br>"
            "<b>4. Écouter</b> : clic sur un son ou Espace. Échap arrête.<br>"
            "<b>5. Nommer</b> : titre unique numéroté, ou un nom par son (double-clic dans le tableau, ou coller une liste).<br>"
            "<b>6. Exporter</b> : WAV, MP3 ou OGG, avec mono, fréquence, normalisation et fondus en option.<br><br>"
            "Raccourcis : Espace écouter · Échap stop · Suppr supprimer · Ctrl+Z / Ctrl+Y · molette zoom · Maj+molette défiler · double-clic zoom sur un son · Ctrl+0 tout afficher.",
        )

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
    app.setStyle("Fusion")

    def excepthook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        print(text, file=sys.stderr)
        QMessageBox.critical(None, "Erreur inattendue", text[-2000:])

    sys.excepthook = excepthook
    win = MainWindow()
    win.show()
    files = [Path(a) for a in sys.argv[1:] if Path(a).suffix.lower() in SUPPORTED_INPUT]
    if files:
        win.add_files(files)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
