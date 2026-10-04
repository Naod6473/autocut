"""Moteur audio d'Autocut : chargement, détection des sons, nommage et export.

Ce module ne dépend pas de l'interface graphique, il peut être testé seul.
Il tourne aussi dans le navigateur (Pyodide) pour la version web : soundfile et
soxr n'y existent pas, ils ne sont donc importés que par les fonctions qui lisent
ou écrivent des fichiers et par le rééchantillonnage.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

SUPPORTED_INPUT = (".wav", ".mp3", ".ogg", ".flac", ".aif", ".aiff")
EXPORT_FORMATS = ("wav", "mp3", "ogg")
DEFAULT_TEMPLATE = "{titre}_{n}"
SESSION_VERSION = 1


@dataclass
class Segment:
    start: int  # en échantillons
    end: int  # exclusif, en échantillons
    name: str = ""
    loop: bool = False  # exporté comme boucle sans coupure (ambiances)

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
    normalize_mode: str = "peak"  # peak = crête en dBFS, lufs = volume perçu (ITU-R BS.1770)
    normalize_lufs: float = -16.0
    highpass_hz: float = 0.0  # filtre coupe-bas (0 = aucun)
    trim: bool = False  # rogne le silence restant au début et à la fin
    trim_db: float = -50.0  # en dessous = silence, pour le rognage
    fade_curve: str = "linear"  # linear | smooth (en S) | sharp (rapide)
    loop: bool = False  # boucle sans coupure : fondu enchaîné de la fin sur le début, à la place des fondus
    loop_crossfade_ms: float = 200.0
    pitch_semitones: float = 0.0  # variation : hauteur décalée (la durée change aussi, comme dans un moteur de jeu)
    gain_db: float = 0.0  # variation : volume, appliqué après la normalisation
    variants: int = 0  # à l'export, N variations en plus de chaque son : nom_v1, nom_v2…
    variant_pitch: float = 1.0  # écart de hauteur maximal des variations (demi-tons)
    variant_volume: float = 2.0  # baisse de volume maximale des variations (dB)


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
    number_repeats: bool = False  # en mode « un nom par son » : pas, pas → pas_01, pas_02

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
            typed = [sanitize_filename(s.name) if s.name.strip() else "" for s in self.segments]
            if self.number_repeats:
                filled = [i for i, n in enumerate(typed) if n]
                for i, n in zip(filled, number_repeats([typed[i] for i in filled], self.digits)):
                    typed[i] = n
            numbered = [typed[i] or numbered[i] for i in range(len(typed))]
        return unique_names(numbered)


def load_track(path: str | os.PathLike) -> Track:
    import soundfile as sf

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


def estimate_threshold(data: np.ndarray, sr: int) -> float:
    """Propose un seuil de silence d'après le bruit de fond de la piste (niveau des passages calmes + 12 dB)."""
    if len(data) == 0:
        return DetectionSettings.threshold_db
    levels, _ = envelope_db(data, sr)
    floor = float(np.percentile(levels, 10))
    loud = float(np.percentile(levels, 99))
    threshold = min(max(floor + 12.0, -65.0), -15.0)
    # Toujours nettement sous les sons les plus forts, sinon rien ne serait détecté
    threshold = min(threshold, loud - 10.0)
    return float(round(threshold))


def _timbre(clip: np.ndarray, sr: int, bands: int = 24) -> np.ndarray:
    """Empreinte du timbre d'un son : énergie par bande de fréquence (dB, échelle log), crête ramenée à 0."""
    mono = clip.mean(axis=1) if clip.ndim == 2 else clip
    frame = 2048
    if len(mono) < frame:
        mono = np.pad(mono, (0, frame - len(mono)))
    starts = np.arange(0, len(mono) - frame + 1, frame // 2)[:64]
    frames = np.stack([mono[s : s + frame] for s in starts]) * np.hanning(frame)
    power = (np.abs(np.fft.rfft(frames, axis=1)) ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(frame, 1 / sr)
    edges = np.geomspace(60, min(16000, sr / 2), bands + 1)
    idx = np.searchsorted(freqs, edges)
    energy = np.array([power[a:max(b, a + 1)].sum() for a, b in zip(idx[:-1], idx[1:])])
    db = 10 * np.log10(np.maximum(energy, 1e-12))
    return np.maximum(db - db.max(), -60.0)


def group_variations(data: np.ndarray, sr: int, segments: list[Segment], tolerance: float = 1.0) -> list[int]:
    """Repère les sons qui se ressemblent (timbre et durée). Renvoie un numéro de groupe par son,
    -1 pour un son sans variante. Les groupes sont numérotés dans l'ordre d'apparition."""
    prints = [(_timbre(data[s.start : s.end], sr), np.log2(max(s.length, 1) / sr)) for s in segments]
    groups: list[list[int]] = []
    centers: list[tuple[np.ndarray, float]] = []
    for i, (tim, dur) in enumerate(prints):
        best, best_d = -1, tolerance
        for g, (ctim, cdur) in enumerate(centers):
            # 6 dB d'écart moyen sur le spectre, ou une durée doublée, comptent chacun pour 1
            d = float(np.abs(tim - ctim).mean()) / 6.0 + abs(dur - cdur)
            if d < best_d:
                best, best_d = g, d
        if best < 0:
            groups.append([i])
            centers.append((tim, dur))
        else:
            groups[best].append(i)
            members = groups[best]
            centers[best] = (np.mean([prints[k][0] for k in members], axis=0), float(np.mean([prints[k][1] for k in members])))
    out = [-1] * len(segments)
    label = 0
    for members in groups:
        if len(members) > 1:
            for k in members:
                out[k] = label
            label += 1
    return out


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
    """Applique un modèle de nom. Étiquettes : {titre}, {n} (numéro), {piste} (nom du fichier source) ; {title} et {track} en anglais."""
    template = template.strip() or DEFAULT_TEMPLATE
    digits = max(digits, len(str(start + count - 1)))
    out = []
    for i in range(start, start + count):
        name = (template.replace("{titre}", title).replace("{title}", title)
                .replace("{piste}", track).replace("{track}", track).replace("{n}", f"{i:0{digits}d}"))
        out.append(sanitize_filename(name))
    return out


def names_from_text(text: str) -> list[str]:
    """Une ligne = un nom ; les lignes vides sont ignorées."""
    return [sanitize_filename(line) for line in text.splitlines() if line.strip()]


def number_repeats(names: list[str], digits: int = 2) -> list[str]:
    """Numérote les noms répétés comme des variantes : pas, porte, pas → pas_01, porte, pas_02."""
    counts: dict[str, int] = {}
    for n in names:
        counts[n.lower()] = counts.get(n.lower(), 0) + 1
    seen: dict[str, int] = {}
    out = []
    for n in names:
        key = n.lower()
        if counts[key] < 2:
            out.append(n)
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(f"{n}_{seen[key]:0{max(digits, len(str(counts[key])))}d}")
    return out


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


def _biquad_filter(clip: np.ndarray, sr: int, stages: list[tuple[tuple, tuple]]) -> np.ndarray:
    """Applique des filtres biquad en cascade, par FFT (rapide sans scipy, donc aussi dans Pyodide)."""
    n = len(clip)
    if n == 0:
        return clip
    nfft = 1 << (n + sr // 2 - 1).bit_length()  # marge pour la traîne du filtre
    z = np.exp(-2j * np.pi * np.fft.rfftfreq(nfft))
    response = np.ones_like(z)
    for b, a in stages:
        response *= (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    spectrum = np.fft.rfft(clip, nfft, axis=0) * response[:, None]
    return np.fft.irfft(spectrum, nfft, axis=0)[:n]


def _highpass(f0: float, sr: int, q: float = 0.7071) -> tuple[tuple, tuple]:
    k = np.tan(np.pi * min(f0, sr * 0.45) / sr)
    norm = 1 + k / q + k * k
    return (1 / norm, -2 / norm, 1 / norm), (1.0, 2 * (k * k - 1) / norm, (1 - k / q + k * k) / norm)


def _k_weighting(sr: int) -> list[tuple[tuple, tuple]]:
    """Filtres de pondération K de la norme ITU-R BS.1770 (coefficients recalculés pour toute fréquence)."""
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = np.tan(np.pi * f0 / sr)
    vh = 10 ** (gain / 20)
    vb = vh ** 0.4996667741545416
    norm = 1 + k / q + k * k
    shelf = (
        ((vh + vb * k / q + k * k) / norm, 2 * (k * k - vh) / norm, (vh - vb * k / q + k * k) / norm),
        (1.0, 2 * (k * k - 1) / norm, (1 - k / q + k * k) / norm),
    )
    # Le passe-haut de la norme garde b = (1, -2, 1), sans normalisation du gain
    _, hp_a = _highpass(38.13547087602444, sr, 0.5003270373238773)
    return [shelf, ((1.0, -2.0, 1.0), hp_a)]


def integrated_loudness(clip: np.ndarray, sr: int) -> float:
    """Volume perçu intégré en LUFS (BS.1770 : blocs de 400 ms, portes absolue et relative).

    Un son plus court qu'un bloc est mesuré d'un seul tenant. Renvoie -inf pour du silence.
    """
    if clip.ndim == 1:
        clip = clip[:, None]
    if len(clip) == 0:
        return float("-inf")
    power = _biquad_filter(clip.astype(np.float64), sr, _k_weighting(sr)) ** 2
    block, step = int(0.4 * sr), int(0.1 * sr)
    if len(power) <= block:
        energies = np.array([power.mean(axis=0).sum()])
    else:
        sums = np.concatenate([np.zeros((1, power.shape[1])), np.cumsum(power, axis=0)])
        starts = np.arange(0, len(power) - block + 1, step)
        energies = ((sums[starts + block] - sums[starts]) / block).sum(axis=1)
    lufs = -0.691 + 10 * np.log10(np.maximum(energies, 1e-20))
    keep = lufs > -70.0
    if not keep.any():
        return float("-inf")
    relative = -0.691 + 10 * np.log10(energies[keep].mean()) - 10.0
    keep &= lufs > relative
    return float(-0.691 + 10 * np.log10(energies[keep].mean()))


def trim_silence(clip: np.ndarray, sr: int, threshold_db: float, margin_ms: float = 5.0) -> np.ndarray:
    """Retire le silence au début et à la fin, en gardant une petite marge."""
    if len(clip) == 0:
        return clip
    loud = np.flatnonzero(np.abs(clip).max(axis=1) > 10 ** (threshold_db / 20.0))
    if not len(loud):
        return clip
    margin = _ms_to_frames(margin_ms, sr)
    return clip[max(0, loud[0] - margin) : min(len(clip), loud[-1] + 1 + margin)]


FADE_CURVES = ("linear", "smooth", "sharp")


def fade_ramp(n: int, curve: str = "linear") -> np.ndarray:
    """Montée de 0 à 1 sur n échantillons. Le fondu de sortie est la même courbe à l'envers.

    linear : droite ; smooth : en S, sans attaque ni coupure audibles ; sharp : démarre doucement
    puis monte vite, donc en sortie le son baisse vite puis s'éteint en douceur.
    """
    x = np.linspace(0.0, 1.0, n, dtype=np.float32)
    if curve == "smooth":
        return (0.5 - 0.5 * np.cos(np.pi * x)).astype(np.float32)
    if curve == "sharp":
        return x * x
    return x


def _resample_fft(clip: np.ndarray, n: int) -> np.ndarray:
    """Rééchantillonne (frames, canaux) vers n frames, par FFT (limité en bande, sans scipy)."""
    if n == len(clip) or len(clip) == 0:
        return clip
    spec = np.fft.rfft(clip, axis=0)
    out = np.zeros((n // 2 + 1, clip.shape[1]), dtype=complex)
    k = min(len(out), len(spec))
    out[:k] = spec[:k]
    return (np.fft.irfft(out, n, axis=0) * (n / len(clip))).astype(np.float32)


def shift_pitch(clip: np.ndarray, semitones: float) -> np.ndarray:
    """Change la hauteur comme une lecture plus rapide ou plus lente : +12 = une octave plus aigu, deux fois plus court."""
    if not semitones or len(clip) < 2:
        return clip
    n = max(1, int(round(len(clip) / 2 ** (semitones / 12.0))))
    return _resample_fft(clip, n)


def variation_params(count: int, pitch: float, volume_db: float, seed: int = 0) -> list[tuple[float, float]]:
    """Réglages de `count` variations : hauteurs réparties de -pitch à +pitch (demi-tons) avec un peu de hasard,
    volumes tirés entre -volume_db et 0 dB. Toujours les mêmes pour une même graine."""
    if count <= 0:
        return []
    rng = np.random.default_rng(seed)
    if count == 1:
        pitches = np.array([pitch if rng.random() < 0.5 else -pitch])
    else:
        pitches = np.linspace(-pitch, pitch, count)
        pitches += rng.uniform(-0.25, 0.25, count) * (2 * pitch / (count - 1))
        rng.shuffle(pitches)
    volumes = rng.uniform(-abs(volume_db), 0.0, count)
    return [(round(float(p), 2), round(float(v), 2)) for p, v in zip(pitches, volumes)]


def make_loop(clip: np.ndarray, sr: int, crossfade_ms: float = 200.0) -> np.ndarray:
    """Boucle sans coupure : la fin du son est fondue sur son début (puissance constante).

    Le point de bouclage est cherché près de la fin, là où le son ressemble le plus à son début,
    pour éviter l'effet de phase. Le résultat est un peu plus court que l'original.
    """
    n = len(clip)
    xf = min(_ms_to_frames(crossfade_ms, sr), n // 3)
    if xf < 16:
        return clip
    mono = clip.mean(axis=1)
    head = mono[:xf]
    # Fin de boucle e : on compare clip[e - xf : e] au début, pour e dans les derniers 100 ms
    search = min(int(0.1 * sr), n // 4)
    window = mono[n - search - xf : n]
    size = 1 << (len(window) + xf).bit_length()
    corr = np.fft.irfft(np.fft.rfft(window, size) * np.conj(np.fft.rfft(head, size)), size)[: search + 1]
    energy = np.convolve(window ** 2, np.ones(xf), mode="valid")[: search + 1]
    score = corr / np.sqrt(np.maximum(energy * float(head @ head), 1e-20))
    e = n - search + int(np.argmax(score))
    t = np.linspace(0.0, np.pi / 2, xf, dtype=np.float32)[:, None]
    out = clip[: e - xf].copy()
    out[:xf] = clip[:xf] * np.sin(t) + clip[e - xf : e] * np.cos(t)
    return out


def process_segment(data: np.ndarray, sr: int, seg: Segment, settings: ExportSettings) -> tuple[np.ndarray, int]:
    clip = data[seg.start : seg.end].astype(np.float32, copy=True)
    if settings.mono and clip.shape[1] > 1:
        clip = clip.mean(axis=1, keepdims=True)
    clip = shift_pitch(clip, settings.pitch_semitones)

    out_sr = sr
    if settings.samplerate and settings.samplerate != sr and len(clip):
        import soxr

        clip = soxr.resample(clip, sr, settings.samplerate, quality="HQ").astype(np.float32)
        out_sr = settings.samplerate

    if settings.highpass_hz > 0 and len(clip):
        clip = _biquad_filter(clip, out_sr, [_highpass(settings.highpass_hz, out_sr)]).astype(np.float32)

    loop = settings.loop or seg.loop
    if settings.trim and not loop:
        clip = trim_silence(clip, out_sr, settings.trim_db)

    if settings.normalize and len(clip):
        peak = float(np.abs(clip).max())
        if peak > 0 and settings.normalize_mode == "lufs":
            loudness = integrated_loudness(clip, out_sr)
            if np.isfinite(loudness):
                # Volume visé, sans laisser la crête dépasser -1 dBFS
                gain = min(10 ** ((settings.normalize_lufs - loudness) / 20.0), 10 ** (-1 / 20.0) / peak)
                clip *= gain
        elif peak > 0:
            clip *= (10 ** (settings.normalize_db / 20.0)) / peak

    if settings.gain_db:
        clip *= 10 ** (settings.gain_db / 20.0)

    if loop:
        clip = make_loop(clip, out_sr, settings.loop_crossfade_ms)
    else:
        n = len(clip)
        fin = min(n, _ms_to_frames(settings.fade_in_ms, out_sr))
        fout = min(n, _ms_to_frames(settings.fade_out_ms, out_sr))
        if fin > 0:
            clip[:fin] *= fade_ramp(fin, settings.fade_curve)[:, None]
        if fout > 0:
            clip[n - fout :] *= fade_ramp(fout, settings.fade_curve)[::-1, None]

    np.clip(clip, -1.0, 1.0, out=clip)
    return clip, out_sr


def write_clip(path: Path, clip: np.ndarray, sr: int, settings: ExportSettings) -> None:
    import soundfile as sf

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
    """Exporte tous les segments d'une piste. Renvoie les fichiers créés, dans l'ordre des sons ;
    avec des variations, chaque son est suivi des siennes (nom_v1, nom_v2…)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = track.resolved_names(template)

    written = []
    ext = settings.fmt.lower()
    for seg, name in zip(track.segments, names):
        versions = [(name, settings)]
        for k, (pitch, gain) in enumerate(variation_params(settings.variants, settings.variant_pitch, settings.variant_volume, seg.start % 2147483647)):
            versions.append((f"{name}_v{k + 1}", replace(settings, pitch_semitones=pitch, gain_db=gain)))
        for version_name, version_settings in versions:
            path = out_dir / f"{version_name}.{ext}"
            if not overwrite:
                i = 2
                while path.exists():
                    path = out_dir / f"{version_name}_{i}.{ext}"
                    i += 1
            clip, sr = process_segment(track.data, track.samplerate, seg, version_settings)
            write_clip(path, clip, sr, version_settings)
            written.append(path)
    return written


def write_listing(path: str | os.PathLike, exported: list[tuple[Track, list[Path]]], variants: int = 0) -> Path:
    """Écrit un CSV des sons exportés : fichier (relatif au CSV), piste source, début, fin, durée en secondes.
    `variants` : nombre de variations exportées après chaque son (elles reprennent ses positions)."""
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["fichier", "piste_source", "debut_s", "fin_s", "duree_s"])
        for track, files in exported:
            sr = track.samplerate
            per = 1 + max(0, variants)
            for k, file in enumerate(files):
                seg = track.segments[k // per]
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
                "loops": [i for i, s in enumerate(t.segments) if s.loop],
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
        loops = set(item.get("loops", []))
        t.segments = [
            Segment(max(0, int(a)), min(total, int(b)), n, i in loops)
            for i, (a, b, n) in enumerate(item.get("segments", []))
            if int(a) < min(total, int(b))
        ]
        tracks.append(t)
    return tracks, doc.get("settings", {}), errors


def format_time(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m)}:{s:06.3f}"
