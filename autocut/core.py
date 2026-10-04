"""Moteur audio d'Autocut : chargement, détection des sons, nommage et export.

Ce module ne dépend pas de l'interface graphique, il peut être testé seul.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

SUPPORTED_INPUT = (".wav", ".mp3", ".ogg", ".flac", ".aif", ".aiff")
EXPORT_FORMATS = ("wav", "mp3", "ogg")
DEFAULT_TEMPLATE = "{titre}_{n}"
SESSION_VERSION = 1


@dataclass
class Segment:
    start: int  # en échantillons
    end: int  # exclusif, en échantillons
    name: str = ""

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class DetectionSettings:
    threshold_db: float = -45.0  # en dessous = silence
    min_silence_ms: float = 250.0  # silence minimal qui sépare deux sons
    min_sound_ms: float = 60.0  # sons plus courts ignorés (clics, bruit)
    padding_ms: float = 20.0  # marge gardée avant/après chaque son


@dataclass
class ExportSettings:
    fmt: str = "wav"  # wav | mp3 | ogg
    samplerate: int | None = None  # None = fréquence d'origine
    mono: bool = False
    normalize: bool = False
    normalize_db: float = -1.0  # crête visée en dBFS
    fade_in_ms: float = 0.0
    fade_out_ms: float = 5.0
    wav_subtype: str = "PCM_16"  # PCM_16 | PCM_24 | FLOAT
    mp3_quality: float = 0.2  # 0 = meilleure qualité, 1 = fichier le plus léger


@dataclass
class Track:
    path: Path
    data: np.ndarray  # float32, forme (frames, canaux)
    samplerate: int
    segments: list[Segment] = field(default_factory=list)
    base_title: str = ""
    naming_mode: str = "unique"  # unique = titre numéroté, multiple = un nom par son
    start_index: int = 1
    digits: int = 2

    @property
    def duration(self) -> float:
        return len(self.data) / self.samplerate

    @property
    def channels(self) -> int:
        return self.data.shape[1]

    def resolved_names(self, template: str = DEFAULT_TEMPLATE) -> list[str]:
        """Noms de fichier finaux (sans extension), uniques, dans l'ordre des sons."""
        numbered = render_names(template, self.base_title or self.path.stem, self.path.stem, len(self.segments), self.start_index, self.digits)
        if self.naming_mode == "multiple":
            numbered = [sanitize_filename(s.name) if s.name.strip() else numbered[i] for i, s in enumerate(self.segments)]
        return unique_names(numbered)


def load_track(path: str | os.PathLike) -> Track:
    path = Path(path)
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return Track(path=path, data=data, samplerate=sr, base_title=sanitize_filename(path.stem))


def _ms_to_frames(ms: float, sr: int) -> int:
    return max(0, int(round(ms * sr / 1000.0)))


def envelope_db(data: np.ndarray, sr: int, window_ms: float = 10.0) -> tuple[np.ndarray, int]:
    """Niveau RMS en dB par fenêtre. Renvoie (niveaux, taille de fenêtre en échantillons)."""
    hop = max(1, _ms_to_frames(window_ms, sr))
    mono = np.abs(data).max(axis=1) if data.ndim == 2 else np.abs(data)
    n = int(np.ceil(len(mono) / hop))
    padded = np.zeros(n * hop, dtype=np.float32)
    padded[: len(mono)] = mono
    rms = np.sqrt(np.mean(padded.reshape(n, hop) ** 2, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1e-10)), hop


def detect_segments(data: np.ndarray, sr: int, settings: DetectionSettings) -> list[Segment]:
    """Découpe la piste en sons séparés par des silences."""
    if len(data) == 0:
        return []
    levels, hop = envelope_db(data, sr)
    loud = levels > settings.threshold_db
    if not loud.any():
        return []

    # Zones bruyantes consécutives : (début, fin) en fenêtres
    edges = np.diff(np.concatenate(([0], loud.astype(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)

    # Fusionne les zones séparées par un silence trop court
    min_gap = max(1, int(np.ceil(settings.min_silence_ms / 1000.0 * sr / hop)))
    regions: list[list[int]] = [[int(starts[0]), int(ends[0])]]
    for s, e in zip(starts[1:], ends[1:]):
        if s - regions[-1][1] < min_gap:
            regions[-1][1] = int(e)
        else:
            regions.append([int(s), int(e)])

    total = len(data)
    pad = _ms_to_frames(settings.padding_ms, sr)
    min_len = _ms_to_frames(settings.min_sound_ms, sr)
    segments = []
    for s, e in regions:
        if (e - s) * hop < min_len:
            continue
        segments.append(Segment(max(0, s * hop - pad), min(total, e * hop + pad)))

    # Évite que la marge fasse se chevaucher deux sons voisins
    for a, b in zip(segments, segments[1:]):
        if a.end > b.start:
            mid = (a.end + b.start) // 2
            a.end, b.start = mid, mid
    return segments


# --- Nommage -----------------------------------------------------------------

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str) -> str:
    """Rend un nom utilisable comme fichier Windows."""
    name = _INVALID.sub("_", name).strip().rstrip(". ")
    if name.upper() in _RESERVED:
        name = f"_{name}"
    return name or "son"


def numbered_names(title: str, count: int, start: int = 1, digits: int = 2, sep: str = "_") -> list[str]:
    title = sanitize_filename(title)
    digits = max(digits, len(str(start + count - 1)))
    return [f"{title}{sep}{i:0{digits}d}" for i in range(start, start + count)]


def render_names(template: str, title: str, track: str, count: int, start: int = 1, digits: int = 2) -> list[str]:
    """Applique un modèle de nom. Étiquettes : {titre}, {n} (numéro), {piste} (nom du fichier source)."""
    template = template.strip() or DEFAULT_TEMPLATE
    digits = max(digits, len(str(start + count - 1)))
    out = []
    for i in range(start, start + count):
        name = template.replace("{titre}", title).replace("{piste}", track).replace("{n}", f"{i:0{digits}d}")
        out.append(sanitize_filename(name))
    return out


def names_from_text(text: str) -> list[str]:
    """Une ligne = un nom ; les lignes vides sont ignorées."""
    return [sanitize_filename(line) for line in text.splitlines() if line.strip()]


def unique_names(names: list[str]) -> list[str]:
    """Ajoute _2, _3… aux doublons (sans tenir compte de la casse, comme Windows)."""
    seen: dict[str, int] = {}
    taken = {n.lower() for n in names}
    out = []
    for n in names:
        key = n.lower()
        if key not in seen:
            seen[key] = 1
            out.append(n)
            continue
        i = seen[key]
        while True:
            i += 1
            candidate = f"{n}_{i}"
            if candidate.lower() not in taken:
                break
        seen[key] = i
        taken.add(candidate.lower())
        out.append(candidate)
    return out


# --- Traitement et export ----------------------------------------------------


def process_segment(data: np.ndarray, sr: int, seg: Segment, settings: ExportSettings) -> tuple[np.ndarray, int]:
    clip = data[seg.start : seg.end].astype(np.float32, copy=True)
    if settings.mono and clip.shape[1] > 1:
        clip = clip.mean(axis=1, keepdims=True)

    out_sr = sr
    if settings.samplerate and settings.samplerate != sr and len(clip):
        import soxr

        clip = soxr.resample(clip, sr, settings.samplerate, quality="HQ").astype(np.float32)
        out_sr = settings.samplerate

    if settings.normalize and len(clip):
        peak = float(np.abs(clip).max())
        if peak > 0:
            clip *= (10 ** (settings.normalize_db / 20.0)) / peak

    n = len(clip)
    fin = min(n, _ms_to_frames(settings.fade_in_ms, out_sr))
    fout = min(n, _ms_to_frames(settings.fade_out_ms, out_sr))
    if fin > 0:
        clip[:fin] *= np.linspace(0.0, 1.0, fin, dtype=np.float32)[:, None]
    if fout > 0:
        clip[n - fout :] *= np.linspace(1.0, 0.0, fout, dtype=np.float32)[:, None]

    np.clip(clip, -1.0, 1.0, out=clip)
    return clip, out_sr


def write_clip(path: Path, clip: np.ndarray, sr: int, settings: ExportSettings) -> None:
    fmt = settings.fmt.lower()
    if fmt == "wav":
        sf.write(str(path), clip, sr, format="WAV", subtype=settings.wav_subtype)
    elif fmt == "mp3":
        if sr not in (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000):
            raise ValueError(f"Le MP3 ne supporte pas {sr} Hz : choisis 44100 ou 48000 Hz à l'export.")
        sf.write(str(path), clip, sr, format="MP3", compression_level=settings.mp3_quality)
    elif fmt == "ogg":
        sf.write(str(path), clip, sr, format="OGG", subtype="VORBIS")
    else:
        raise ValueError(f"Format inconnu : {settings.fmt}")


def export_track(
    track: Track, out_dir: str | os.PathLike, settings: ExportSettings, overwrite: bool = False, template: str = DEFAULT_TEMPLATE
) -> list[Path]:
    """Exporte tous les segments d'une piste. Renvoie les fichiers créés, dans l'ordre des sons."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = track.resolved_names(template)

    written = []
    ext = settings.fmt.lower()
    for seg, name in zip(track.segments, names):
        path = out_dir / f"{name}.{ext}"
        if not overwrite:
            i = 2
            while path.exists():
                path = out_dir / f"{name}_{i}.{ext}"
                i += 1
        clip, sr = process_segment(track.data, track.samplerate, seg, settings)
        write_clip(path, clip, sr, settings)
        written.append(path)
    return written


def write_listing(path: str | os.PathLike, exported: list[tuple[Track, list[Path]]]) -> Path:
    """Écrit un CSV des sons exportés : fichier (relatif au CSV), piste source, début, fin, durée en secondes."""
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["fichier", "piste_source", "debut_s", "fin_s", "duree_s"])
        for track, files in exported:
            sr = track.samplerate
            for seg, file in zip(track.segments, files):
                rel = Path(os.path.relpath(file, path.parent)).as_posix()
                w.writerow([rel, track.path.name, f"{seg.start / sr:.3f}", f"{seg.end / sr:.3f}", f"{seg.length / sr:.3f}"])
    return path


# --- Sessions ----------------------------------------------------------------


def save_session(path: str | os.PathLike, tracks: list[Track], settings: dict | None = None) -> None:
    """Enregistre le découpage (pas l'audio) dans un fichier .autocut (JSON)."""
    path = Path(path)
    doc = {
        "version": SESSION_VERSION,
        "settings": settings or {},
        "tracks": [
            {
                "path": str(t.path.resolve()),
                "base_title": t.base_title,
                "naming_mode": t.naming_mode,
                "start_index": t.start_index,
                "digits": t.digits,
                "segments": [[s.start, s.end, s.name] for s in t.segments],
            }
            for t in tracks
        ],
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def load_session(path: str | os.PathLike) -> tuple[list[Track], dict, list[str]]:
    """Recharge une session. Renvoie (pistes, réglages, erreurs pour les fichiers introuvables)."""
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    tracks, errors = [], []
    for item in doc.get("tracks", []):
        src = Path(item["path"])
        if not src.exists() and (path.parent / src.name).exists():
            src = path.parent / src.name  # session déplacée avec ses fichiers audio
        try:
            t = load_track(src)
        except Exception as exc:
            errors.append(f"{src.name} : {exc}")
            continue
        t.base_title = item.get("base_title", t.base_title)
        t.naming_mode = item.get("naming_mode", "unique")
        t.start_index = int(item.get("start_index", 1))
        t.digits = int(item.get("digits", 2))
        total = len(t.data)
        t.segments = [Segment(max(0, int(a)), min(total, int(b)), n) for a, b, n in item.get("segments", []) if int(a) < min(total, int(b))]
        tracks.append(t)
    return tracks, doc.get("settings", {}), errors


def format_time(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m)}:{s:06.3f}"
