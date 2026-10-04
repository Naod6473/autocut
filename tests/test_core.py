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
    estimate_threshold,
    export_track,
    fade_ramp,
    group_variations,
    integrated_loudness,
    load_session,
    load_track,
    make_loop,
    names_from_text,
    number_repeats,
    numbered_names,
    process_segment,
    render_names,
    save_session,
    sanitize_filename,
    shift_pitch,
    trim_silence,
    unique_names,
    variation_params,
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
    assert render_names("{track}_{title}_{n}", "Door", "pack", 1) == ["pack_Door_01"]
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


def test_auto_threshold_follows_noise_floor():
    parts = [silence(1.0, noise=0.001), burst(0.4), silence(1.0, noise=0.001), burst(0.4), silence(1.0, noise=0.001)]
    data = make_track(parts)
    threshold = estimate_threshold(data, SR)
    assert -55 < threshold < -40  # bruit de fond vers -60 dB, plus 12 dB
    assert len(detect_segments(data, SR, DetectionSettings(threshold_db=threshold))) == 2
    assert estimate_threshold(make_track([silence(1.0), burst(0.4)]), SR) == -65


def test_loudness_matches_bs1770_reference():
    # Un sinus 997 Hz à 0 dBFS sur un canal vaut -3,01 LUFS par définition
    t = np.arange(5 * SR) / SR
    sine = np.sin(2 * np.pi * 997 * t)[:, None]
    assert integrated_loudness(sine, SR) == pytest.approx(-3.01, abs=0.05)
    assert integrated_loudness(sine * 0.1, SR) == pytest.approx(-23.01, abs=0.05)
    assert integrated_loudness(np.zeros((SR, 2)), SR) == float("-inf")


def test_lufs_normalization_and_peak_cap():
    t = np.arange(SR) / SR
    data = np.repeat((0.05 * np.sin(2 * np.pi * 1000 * t))[:, None], 2, axis=1).astype(np.float32)
    seg = Segment(0, len(data))
    clip, _ = process_segment(data, SR, seg, ExportSettings(normalize=True, normalize_mode="lufs", normalize_lufs=-20, fade_out_ms=0))
    assert integrated_loudness(clip, SR) == pytest.approx(-20, abs=0.1)
    # -1 LUFS demanderait une crête au-dessus de 0 dBFS : on s'arrête à -1 dBFS
    clip, _ = process_segment(data, SR, seg, ExportSettings(normalize=True, normalize_mode="lufs", normalize_lufs=-1, fade_out_ms=0))
    assert np.abs(clip).max() == pytest.approx(10 ** (-1 / 20), abs=1e-3)


def test_highpass_and_trim():
    t = np.arange(SR) / SR
    rumble = np.repeat((0.5 * np.sin(2 * np.pi * 30 * t))[:, None], 2, axis=1).astype(np.float32)
    clip, _ = process_segment(rumble, SR, Segment(0, SR), ExportSettings(highpass_hz=120, fade_out_ms=0))
    assert np.abs(clip[SR // 2 :]).max() < 0.05  # 30 Hz fortement atténué
    voice = np.repeat((0.5 * np.sin(2 * np.pi * 1000 * t))[:, None], 2, axis=1).astype(np.float32)
    clip, _ = process_segment(voice, SR, Segment(0, SR), ExportSettings(highpass_hz=120, fade_out_ms=0))
    assert np.abs(clip[SR // 2 :]).max() > 0.48  # 1 kHz intact

    data = make_track([silence(0.3), burst(0.2), silence(0.3)])
    trimmed = trim_silence(data, SR, -50, margin_ms=5)
    assert abs(len(trimmed) - int(0.21 * SR)) < int(0.02 * SR)
    clip, _ = process_segment(data, SR, Segment(0, len(data)), ExportSettings(trim=True, trim_db=-50))
    assert len(clip) == len(trimmed)


def test_repeated_names_become_variations():
    assert number_repeats(["pas", "porte", "Pas", "pas"]) == ["pas_01", "porte", "Pas_02", "pas_03"]
    assert number_repeats(["a"] * 12, digits=1)[-1] == "a_12"
    track = Track(path=Path("pack.wav"), data=make_track([burst(0.2)]), samplerate=SR, base_title="Pack", naming_mode="multiple")
    track.segments = [Segment(0, 10, "pas"), Segment(10, 20, ""), Segment(20, 30, "pas")]
    assert track.resolved_names() == ["pas", "Pack_02", "pas_2"]
    track.number_repeats = True
    assert track.resolved_names() == ["pas_01", "Pack_02", "pas_02"]


def test_group_variations_by_timbre_and_length():
    rng = np.random.default_rng(3)
    noise = lambda: (rng.standard_normal(int(0.2 * SR)) * 0.4).astype(np.float32)
    parts = [silence(0.3)]
    for p in [burst(0.5, 300), burst(0.5, 310), noise(), noise(), burst(0.5, 305), burst(1.5, 3000)]:
        parts += [p, silence(0.3)]
    data = make_track(parts)
    segs = detect_segments(data, SR, DetectionSettings())
    assert len(segs) == 6
    assert group_variations(data, SR, segs) == [0, 0, 1, 1, 0, -1]


def test_fade_curves():
    for curve in ("linear", "smooth", "sharp"):
        r = fade_ramp(101, curve)
        assert r[0] == 0 and r[-1] == pytest.approx(1) and np.all(np.diff(r) >= 0)
    assert fade_ramp(101, "smooth")[50] == pytest.approx(0.5, abs=1e-6)
    assert fade_ramp(101, "sharp")[50] == pytest.approx(0.25, abs=1e-6)
    data = np.ones((SR, 1), dtype=np.float32) * 0.5
    clip, _ = process_segment(data, SR, Segment(0, SR), ExportSettings(fade_out_ms=1000, fade_curve="sharp"))
    # En sortie, la courbe rapide est déjà au quart du volume à mi-chemin
    assert clip[SR // 2, 0] == pytest.approx(0.125, abs=1e-3)


def test_pitch_variations():
    t = np.arange(SR) / SR
    tone = np.sin(2 * np.pi * 440 * t).astype(np.float32)[:, None]
    up = shift_pitch(tone, 12)
    assert len(up) == SR // 2
    spectrum = np.abs(np.fft.rfft(up[:, 0]))
    assert np.argmax(spectrum) * SR / len(up) == pytest.approx(880, abs=3)
    params = variation_params(4, 2.0, 3.0, seed=7)
    assert params == variation_params(4, 2.0, 3.0, seed=7)
    assert len({p for p, _ in params}) == 4
    assert all(-2.5 <= p <= 2.5 and -3 <= v <= 0 for p, v in params)
    assert variation_params(0, 2, 3) == []
    clip, _ = process_segment(tone * 0.5, SR, Segment(0, SR), ExportSettings(gain_db=-6, fade_out_ms=0, pitch_semitones=-12))
    assert len(clip) == 2 * SR
    assert np.abs(clip).max() == pytest.approx(0.25, abs=0.01)


def test_seamless_loop():
    rng = np.random.default_rng(1)
    noise = (rng.standard_normal((2 * SR, 2)) * 0.3).astype(np.float32)
    loop = make_loop(noise, SR, 200)
    xf = int(0.2 * SR)
    assert 2 * SR - xf - int(0.1 * SR) <= len(loop) <= 2 * SR - xf
    # Le passage fin → début est continu : pas de saut plus grand qu'un pas ordinaire du bruit
    seam = np.abs(loop[0] - loop[-1]).max()
    assert seam < np.abs(np.diff(noise, axis=0)).max()
    # Sans fondus : la boucle passe à la place des fondus d'entrée et de sortie
    clip, _ = process_segment(noise, SR, Segment(0, 2 * SR, loop=True), ExportSettings(fade_in_ms=500, fade_out_ms=500))
    assert len(clip) == len(loop) and np.abs(clip[:10]).max() > 0.01


def test_session_keeps_loops(tmp_path):
    wav = tmp_path / "amb.wav"
    sf.write(wav, make_track([burst(0.5), silence(0.2), burst(0.5)]), SR)
    t = load_track(wav)
    t.segments = [Segment(0, 1000, "a"), Segment(2000, 4000, "b", loop=True)]
    save_session(tmp_path / "s.autocut", [t])
    tracks, _, errors = load_session(tmp_path / "s.autocut")
    assert not errors and [s.loop for s in tracks[0].segments] == [False, True]


def test_export_with_variations_and_listing(tmp_path):
    wav = tmp_path / "pack.wav"
    sf.write(wav, make_track([silence(0.3), burst(0.3), silence(0.3), burst(0.3), silence(0.3)]), SR)
    t = load_track(wav)
    t.segments = detect_segments(t.data, t.samplerate, DetectionSettings())
    files = export_track(t, tmp_path / "out", ExportSettings(variants=2, variant_pitch=2), template="{titre}_{n}")
    assert [f.name for f in files] == ["pack_01.wav", "pack_01_v1.wav", "pack_01_v2.wav", "pack_02.wav", "pack_02_v1.wav", "pack_02_v2.wav"]
    lengths = [len(sf.read(f)[0]) for f in files[:3]]
    assert len(set(lengths)) == 3
    listing = write_listing(tmp_path / "out" / "liste.csv", [(t, files)], variants=2)
    rows = listing.read_text(encoding="utf-8-sig").splitlines()
    assert len(rows) == 7 and rows[4].startswith("pack_02.wav") and rows[2].split(",")[2] == rows[1].split(",")[2]
