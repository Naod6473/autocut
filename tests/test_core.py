from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from autocut.core import (
    DetectionSettings,
    ExportSettings,
    Segment,
    Track,
    detect_segments,
    export_track,
    load_session,
    load_track,
    names_from_text,
    numbered_names,
    render_names,
    save_session,
    sanitize_filename,
    unique_names,
    write_listing,
)

SR = 44100


def burst(seconds, freq=440.0, amp=0.5):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * freq * t) * np.hanning(len(t)) ** 0.2).astype(np.float32)


def silence(seconds, noise=0.0):
    rng = np.random.default_rng(0)
    return (rng.standard_normal(int(seconds * SR)) * noise).astype(np.float32)


def make_track(parts, channels=2):
    mono = np.concatenate(parts)
    data = np.repeat(mono[:, None], channels, axis=1)
    return data


def test_detects_three_sounds_with_noise_floor():
    data = make_track([silence(0.5, 0.001), burst(0.3), silence(0.6, 0.001), burst(0.5, 880), silence(0.4, 0.001), burst(0.2, 220), silence(0.5, 0.001)])
    segs = detect_segments(data, SR, DetectionSettings())
    assert len(segs) == 3
    expected_starts = [0.5, 1.4, 2.3]
    for seg, exp in zip(segs, expected_starts):
        assert abs(seg.start / SR - exp) < 0.05


def test_short_gap_is_merged():
    data = make_track([silence(0.3), burst(0.2), silence(0.1), burst(0.2), silence(0.3)])
    segs = detect_segments(data, SR, DetectionSettings(min_silence_ms=250))
    assert len(segs) == 1


def test_tiny_click_is_ignored():
    data = make_track([silence(0.3), burst(0.01), silence(0.5), burst(0.3), silence(0.3)])
    segs = detect_segments(data, SR, DetectionSettings(min_sound_ms=60))
    assert len(segs) == 1


def test_silent_track_gives_nothing():
    assert detect_segments(make_track([silence(1.0)]), SR, DetectionSettings()) == []


def test_padding_never_overlaps():
    data = make_track([burst(0.2), silence(0.26), burst(0.2)])
    segs = detect_segments(data, SR, DetectionSettings(min_silence_ms=250, padding_ms=200))
    assert len(segs) == 2
    assert segs[0].end <= segs[1].start


def test_naming_helpers():
    assert numbered_names("Porte", 3) == ["Porte_01", "Porte_02", "Porte_03"]
    assert numbered_names("Pas", 2, start=9, digits=1) == ["Pas_09", "Pas_10"]
    assert names_from_text("coup\n\n  épée  \n") == ["coup", "épée"]
    assert sanitize_filename('a:b/c?') == "a_b_c_"
    assert sanitize_filename("CON") == "_CON"
    assert unique_names(["a", "A", "a", "b"]) == ["a", "A_2", "a_3", "b"]


@pytest.mark.parametrize("fmt", ["wav", "mp3", "ogg"])
def test_export_formats(tmp_path, fmt):
    data = make_track([silence(0.2), burst(0.3), silence(0.4), burst(0.3), silence(0.2)])
    src = tmp_path / "pack.wav"
    sf.write(src, data, SR)
    track = load_track(src)
    track.segments = detect_segments(track.data, SR, DetectionSettings())
    track.naming_mode = "multiple"
    track.segments[1].name = "Explosion"
    files = export_track(track, tmp_path / "out", ExportSettings(fmt=fmt, mono=True, samplerate=48000, normalize=True))
    assert [f.name for f in files] == [f"pack_01.{fmt}", f"Explosion.{fmt}"]
    clip, sr = sf.read(files[0], always_2d=True)
    assert sr == 48000 and clip.shape[1] == 1
    assert abs(np.abs(clip).max() - 10 ** (-1 / 20)) < 0.05


def test_export_does_not_overwrite(tmp_path):
    track = Track(path=tmp_path / "x.wav", data=make_track([burst(0.2)]), samplerate=SR, base_title="x")
    track.segments = [Segment(0, 1000)]
    export_track(track, tmp_path, ExportSettings())
    second = export_track(track, tmp_path, ExportSettings())
    assert second[0].name == "x_01_2.wav"


def test_unique_mode_ignores_custom_names():
    track = Track(path=Path("Porte.wav"), data=make_track([burst(0.2)]), samplerate=SR, base_title="Porte")
    track.segments = [Segment(0, 10, "x"), Segment(10, 20, "y")]
    track.start_index = 5
    assert track.resolved_names() == ["Porte_05", "Porte_06"]
    track.naming_mode = "multiple"
    assert track.resolved_names() == ["x", "y"]


def test_name_template():
    assert render_names("SFX_{titre}_{n}", "Porte", "pack", 2) == ["SFX_Porte_01", "SFX_Porte_02"]
    assert render_names("{piste}-{n}", "x", "pack 3", 1, start=7, digits=3) == ["pack 3-007"]
    assert render_names("", "Porte", "pack", 1) == ["Porte_01"]
    assert render_names("a/{titre}", "b", "p", 1) == ["a_b"]
    track = Track(path=Path("pack.wav"), data=make_track([burst(0.2)]), samplerate=SR, base_title="Pas")
    track.segments = [Segment(0, 10), Segment(10, 20)]
    assert track.resolved_names("{titre}") == ["Pas", "Pas_2"]


def test_listing_and_session_roundtrip(tmp_path):
    src = tmp_path / "pack.wav"
    sf.write(src, make_track([silence(0.2), burst(0.3), silence(0.4), burst(0.3), silence(0.2)]), SR)
    track = load_track(src)
    track.segments = detect_segments(track.data, SR, DetectionSettings())
    track.naming_mode, track.start_index = "multiple", 3
    track.segments[0].name = "Coup"
    files = export_track(track, tmp_path / "out", ExportSettings())
    csv_text = write_listing(tmp_path / "out" / "liste.csv", [(track, files)]).read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0] == "fichier,piste_source,debut_s,fin_s,duree_s"
    assert csv_text.splitlines()[1].startswith("Coup.wav,pack.wav,")
    assert csv_text.splitlines()[2].startswith("pack_04.wav,")

    save_session(tmp_path / "s.autocut", [track], {"template": "X_{n}"})
    tracks, settings, errors = load_session(tmp_path / "s.autocut")
    assert errors == [] and settings == {"template": "X_{n}"}
    t = tracks[0]
    assert [(s.start, s.end, s.name) for s in t.segments] == [(s.start, s.end, s.name) for s in track.segments]
    assert (t.naming_mode, t.start_index) == ("multiple", 3)

    src.unlink()
    _, _, errors = load_session(tmp_path / "s.autocut")
    assert len(errors) == 1
