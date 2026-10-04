"""Affichage de la forme d'onde avec les sons détectés, éditables à la souris."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QMenu, QWidget

from .core import DEFAULT_TEMPLATE, Segment, Track, format_time

EDGE_GRAB_PX = 6
PEAK_BLOCK = 256

COLORS = {
    "bg": QColor("#16181c"),
    "wave": QColor("#7f8ea3"),
    "wave_in": QColor("#c8d4e6"),
    "axis": QColor("#3a3d46"),
    "seg_a": QColor(76, 145, 255, 55),
    "seg_b": QColor(60, 200, 160, 55),
    "seg_sel": QColor(255, 138, 61, 70),
    "edge": QColor("#e8e8e8"),
    "edge_sel": QColor("#ff8a3d"),
    "text": QColor("#e8e8e8"),
    "playhead": QColor("#ffffff"),
    "draft": QColor(255, 255, 255, 50),
    "label_bg": QColor(22, 24, 28, 200),
}


class WaveformView(QWidget):
    """Les positions sont en échantillons ; la vue montre [view_start, view_end)."""

    selectionChanged = Signal(int)  # index du son, -1 si aucun
    segmentsEdited = Signal()  # émis après une modification (pour l'annulation et le tableau)
    aboutToEdit = Signal()  # émis avant une modification (sauvegarde pour Ctrl+Z)
    playRequested = Signal(int)
    viewChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.track: Track | None = None
        self.template = DEFAULT_TEMPLATE
        self.peaks: np.ndarray | None = None  # (blocs, 2) min/max sur toutes les voies
        self.view_start = 0
        self.view_end = 1
        self.selected = -1
        self.playhead: int | None = None
        self._drag = None  # ("edge", idx, "start"/"end") | ("create", origin) | ("pan", x, view_start)
        self._draft: tuple[int, int] | None = None
        self._edited = False

    # --- Données -------------------------------------------------------------

    def set_track(self, track: Track | None):
        self.track = track
        self.selected = -1
        self.playhead = None
        if track is not None and len(track.data):
            mono = track.data.max(axis=1), track.data.min(axis=1)
            n = int(np.ceil(len(track.data) / PEAK_BLOCK))
            hi = np.full(n * PEAK_BLOCK, -1.0, dtype=np.float32)
            lo = np.full(n * PEAK_BLOCK, 1.0, dtype=np.float32)
            hi[: len(mono[0])] = mono[0]
            lo[: len(mono[1])] = mono[1]
            self.peaks = np.stack([lo.reshape(n, PEAK_BLOCK).min(axis=1), hi.reshape(n, PEAK_BLOCK).max(axis=1)], axis=1)
            self.view_start, self.view_end = 0, len(track.data)
        else:
            self.peaks = None
            self.view_start, self.view_end = 0, 1
        self.viewChanged.emit()
        self.update()

    @property
    def total(self) -> int:
        return len(self.track.data) if self.track is not None else 1

    @property
    def segments(self) -> list[Segment]:
        return self.track.segments if self.track is not None else []

    def select(self, idx: int, ensure_visible: bool = True):
        self.selected = idx if 0 <= idx < len(self.segments) else -1
        if ensure_visible and self.selected >= 0:
            seg = self.segments[self.selected]
            if seg.end < self.view_start or seg.start > self.view_end:
                span = self.view_end - self.view_start
                self.set_view(seg.start - span // 4, seg.start - span // 4 + span)
        self.update()

    # --- Vue / zoom ----------------------------------------------------------

    def set_view(self, start: int, end: int):
        span = max(64, min(self.total, end - start))
        start = int(max(0, min(start, self.total - span)))
        self.view_start, self.view_end = start, start + span
        self.viewChanged.emit()
        self.update()

    def zoom(self, factor: float, anchor_x: float | None = None):
        if anchor_x is None:
            anchor_x = self.width() / 2
        anchor = self.x_to_frame(anchor_x)
        span = (self.view_end - self.view_start) * factor
        ratio = anchor_x / max(1, self.width())
        start = anchor - span * ratio
        self.set_view(int(start), int(start + span))

    def zoom_fit(self):
        self.set_view(0, self.total)

    def frame_to_x(self, frame: float) -> float:
        return (frame - self.view_start) / max(1, self.view_end - self.view_start) * self.width()

    def x_to_frame(self, x: float) -> int:
        f = self.view_start + x / max(1, self.width()) * (self.view_end - self.view_start)
        return int(max(0, min(self.total, f)))

    # --- Dessin --------------------------------------------------------------

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), COLORS["bg"])
        w, h = self.width(), self.height()
        mid = h / 2
        p.setPen(QPen(COLORS["axis"], 1))
        p.drawLine(0, int(mid), w, int(mid))
        if self.track is None or self.peaks is None:
            p.setPen(COLORS["text"])
            p.drawText(self.rect(), Qt.AlignCenter, "Glisse des fichiers audio ici ou clique sur « Ouvrir des pistes »")
            return

        # Zones des sons
        for i, seg in enumerate(self.segments):
            x0, x1 = self.frame_to_x(seg.start), self.frame_to_x(seg.end)
            if x1 < 0 or x0 > w:
                continue
            color = COLORS["seg_sel"] if i == self.selected else (COLORS["seg_a"] if i % 2 == 0 else COLORS["seg_b"])
            p.fillRect(QRectF(x0, 0, x1 - x0, h), color)

        # Forme d'onde : min/max par colonne de pixels
        lo, hi = self._column_peaks(w)
        if lo is not None:
            top = mid - hi * (mid - 4)
            bottom = mid - lo * (mid - 4)
            xs = np.arange(len(lo), dtype=np.float64)
            poly = QPolygonF([QPointF(x, y) for x, y in zip(xs, top)] + [QPointF(x, y) for x, y in zip(xs[::-1], bottom[::-1])])
            p.setPen(Qt.NoPen)
            p.setBrush(COLORS["wave_in"])
            p.drawPolygon(poly)

        # Bords et étiquettes
        font = QFont(self.font())
        font.setPointSize(8)
        p.setFont(font)
        names = self.track.resolved_names(self.template)
        for i, seg in enumerate(self.segments):
            x0, x1 = self.frame_to_x(seg.start), self.frame_to_x(seg.end)
            if x1 < 0 or x0 > w:
                continue
            pen = QPen(COLORS["edge_sel"] if i == self.selected else COLORS["edge"], 2 if i == self.selected else 1)
            p.setPen(pen)
            p.drawLine(QPointF(x0, 0), QPointF(x0, h))
            p.drawLine(QPointF(x1, 0), QPointF(x1, h))
            if x1 - x0 > 30:
                label = p.fontMetrics().elidedText(f"{i + 1}. {names[i]}{' ⟳' if seg.loop else ''}", Qt.ElideRight, int(x1 - x0 - 12))
                # Pastille sombre derrière le nom pour qu'il reste lisible sur la forme d'onde
                box = QRectF(x0 + 3, 3, p.fontMetrics().horizontalAdvance(label) + 8, 16)
                p.setPen(Qt.NoPen)
                p.setBrush(COLORS["label_bg"])
                p.drawRoundedRect(box, 4, 4)
                p.setPen(COLORS["text"])
                p.drawText(box, Qt.AlignCenter, label)

        if self._draft is not None:
            a, b = sorted(self._draft)
            p.fillRect(QRectF(self.frame_to_x(a), 0, self.frame_to_x(b) - self.frame_to_x(a), h), COLORS["draft"])

        if self.playhead is not None:
            x = self.frame_to_x(self.playhead)
            p.setPen(QPen(COLORS["playhead"], 2))
            p.drawLine(QPointF(x, 0), QPointF(x, h))

        p.setPen(COLORS["text"])
        sr = self.track.samplerate
        p.drawText(QRectF(4, h - 18, 200, 16), Qt.AlignLeft, format_time(self.view_start / sr))
        p.drawText(QRectF(w - 204, h - 18, 200, 16), Qt.AlignRight, format_time(self.view_end / sr))

    def _column_peaks(self, w: int):
        if w <= 0:
            return None, None
        span = self.view_end - self.view_start
        per_px = span / w
        if per_px >= PEAK_BLOCK:
            src = self.peaks
            a, b = self.view_start / PEAK_BLOCK, self.view_end / PEAK_BLOCK
            lo_src, hi_src = src[:, 0], src[:, 1]
        else:
            raw = self.track.data[self.view_start : self.view_end]
            lo_src, hi_src = raw.min(axis=1), raw.max(axis=1)
            a, b = 0.0, float(len(raw))
        if len(lo_src) == 0:
            return None, None
        edges = np.linspace(a, b, w + 1)
        idx = np.clip(edges.astype(np.int64), 0, len(lo_src) - 1)
        if per_px >= 1:
            # Indices croissants : min/max de chaque tranche [idx[i], idx[i+1])
            stop = min(len(lo_src), max(int(np.ceil(b)), int(idx[-1]) + 1))
            lo = np.minimum.reduceat(lo_src[:stop], idx[:-1])
            hi = np.maximum.reduceat(hi_src[:stop], idx[:-1])
        else:
            lo, hi = lo_src[idx[:-1]], hi_src[idx[:-1]]
        return lo, hi

    # --- Souris --------------------------------------------------------------

    def _hit(self, x: float):
        """Renvoie ("edge", idx, côté) ou ("body", idx) ou None."""
        best = None
        for i, seg in enumerate(self.segments):
            for side, frame in (("start", seg.start), ("end", seg.end)):
                d = abs(self.frame_to_x(frame) - x)
                if d <= EDGE_GRAB_PX and (best is None or d < best[0]):
                    best = (d, i, side)
        if best is not None:
            return ("edge", best[1], best[2])
        f = self.x_to_frame(x)
        for i, seg in enumerate(self.segments):
            if seg.start <= f < seg.end:
                return ("body", i)
        return None

    def mousePressEvent(self, e):
        if self.track is None:
            return
        x = e.position().x()
        if e.button() == Qt.MiddleButton:
            self._drag = ("pan", x, self.view_start)
            return
        if e.button() != Qt.LeftButton:
            return
        hit = self._hit(x)
        if hit and hit[0] == "edge":
            self.aboutToEdit.emit()
            self._drag = ("edge", hit[1], hit[2])
            self._edited = False
            self.select(hit[1], ensure_visible=False)
            self.selectionChanged.emit(self.selected)
        elif hit and hit[0] == "body":
            self.select(hit[1], ensure_visible=False)
            self.selectionChanged.emit(self.selected)
            self.playRequested.emit(hit[1])
        else:
            self._drag = ("create", self.x_to_frame(x))
            self._draft = None

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag is None:
            hit = self._hit(x) if self.track is not None else None
            self.setCursor(Qt.SizeHorCursor if hit and hit[0] == "edge" else Qt.ArrowCursor)
            return
        kind = self._drag[0]
        if kind == "pan":
            span = self.view_end - self.view_start
            dx = (x - self._drag[1]) / max(1, self.width()) * span
            self.set_view(int(self._drag[2] - dx), int(self._drag[2] - dx) + span)
        elif kind == "edge":
            _, i, side = self._drag
            segs = self.segments
            seg = segs[i]
            f = self.x_to_frame(x)
            min_len = max(1, self.track.samplerate // 100)
            if side == "start":
                lower = segs[i - 1].end if i > 0 else 0
                seg.start = int(min(max(f, lower), seg.end - min_len))
            else:
                upper = segs[i + 1].start if i + 1 < len(segs) else self.total
                seg.end = int(max(min(f, upper), seg.start + min_len))
            self._edited = True
            self.update()
        elif kind == "create":
            self._draft = (self._drag[1], self.x_to_frame(x))
            self.update()

    def mouseReleaseEvent(self, e):
        drag, self._drag = self._drag, None
        if drag is None:
            return
        if drag[0] == "edge" and self._edited:
            self.segmentsEdited.emit()
        elif drag[0] == "create":
            draft, self._draft = self._draft, None
            if draft is not None and abs(self.frame_to_x(draft[1]) - self.frame_to_x(draft[0])) > 4:
                self.add_segment(*sorted(draft))
            else:
                self.playhead = self.x_to_frame(e.position().x())
                self.select(-1)
                self.selectionChanged.emit(-1)
            self.update()

    def wheelEvent(self, e):
        if self.track is None:
            return
        dy = e.angleDelta().y()
        dx = e.angleDelta().x()
        if e.modifiers() & Qt.ShiftModifier or abs(dx) > abs(dy):
            span = self.view_end - self.view_start
            step = -(dx or dy) / 120 * span * 0.15
            self.set_view(int(self.view_start + step), int(self.view_start + step) + span)
        elif dy:
            self.zoom(0.8 if dy > 0 else 1.25, e.position().x())

    def mouseDoubleClickEvent(self, e):
        hit = self._hit(e.position().x()) if self.track is not None else None
        if hit and hit[0] == "body":
            seg = self.segments[hit[1]]
            pad = (seg.end - seg.start) // 10
            self.set_view(seg.start - pad, seg.end + pad)

    def contextMenuEvent(self, e):
        if self.track is None:
            return
        x = e.pos().x()
        f = self.x_to_frame(x)
        hit = self._hit(x)
        menu = QMenu(self)
        if hit:
            i = hit[1]
            self.select(i, ensure_visible=False)
            self.selectionChanged.emit(i)
            menu.addAction("Écouter", lambda: self.playRequested.emit(i))
            if self.segments[i].start < f < self.segments[i].end:
                menu.addAction("Couper ici en deux sons", lambda: self.split_at(i, f))
            if i + 1 < len(self.segments):
                menu.addAction("Fusionner avec le son suivant", lambda: self.merge_next(i))
            menu.addSeparator()
            menu.addAction("Supprimer ce son", lambda: self.delete(i))
        else:
            menu.addAction("Ajouter un son ici", lambda: self.add_around(f))
        menu.addSeparator()
        menu.addAction("Tout afficher", self.zoom_fit)
        menu.exec(e.globalPos())

    # --- Opérations sur les sons ---------------------------------------------

    def _commit(self, select: int | None = None):
        if select is not None:
            self.select(select, ensure_visible=False)
            self.selectionChanged.emit(self.selected)
        self.segmentsEdited.emit()
        self.update()

    def add_segment(self, start: int, end: int):
        self.aboutToEdit.emit()
        segs = self.segments
        # Rogne la nouvelle zone sur ses voisins pour éviter les chevauchements
        for s in segs:
            if s.start <= start < s.end:
                start = s.end
            if s.start < end <= s.end:
                end = s.start
        if end - start < 16:
            return
        segs[:] = [s for s in segs if not (start <= s.start and s.end <= end)]
        segs.append(Segment(int(start), int(end)))
        segs.sort(key=lambda s: s.start)
        self._commit(next(i for i, s in enumerate(segs) if s.start == start))

    def add_around(self, frame: int):
        half = self.track.samplerate // 4
        self.add_segment(max(0, frame - half), min(self.total, frame + half))

    def split_at(self, i: int, frame: int):
        self.aboutToEdit.emit()
        seg = self.segments[i]
        new = Segment(frame, seg.end)
        seg.end = frame
        self.segments.insert(i + 1, new)
        self._commit(i + 1)

    def merge_next(self, i: int):
        self.aboutToEdit.emit()
        segs = self.segments
        segs[i].end = segs[i + 1].end
        del segs[i + 1]
        self._commit(i)

    def delete(self, i: int):
        if not (0 <= i < len(self.segments)):
            return
        self.aboutToEdit.emit()
        del self.segments[i]
        self._commit(min(i, len(self.segments) - 1))
