import importlib
from pathlib import Path

import pytest


def _module():
    try:
        return importlib.import_module("bluray_fidelity.bluray")
    except ModuleNotFoundError:
        pytest.fail("bluray_title_reader module is not implemented")


def _disc_root(tmp_path: Path, name: str = "disc") -> Path:
    root = tmp_path / name
    (root / "BDMV").mkdir(parents=True)
    (root / "BDMV/index.bdmv").write_bytes(b"index")
    return root


def test_probe_main_title_parses_selected_record(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: "SELECTED\t2\t802\t90000\t4096\t51\n",
    )

    asset = bluray.probe_main_title(root)

    assert asset.root == root.resolve()
    assert asset.entry_path == "HDATHOME/MAIN_TITLE.m2ts"
    assert asset.title_index == 2
    assert asset.playlist == 802
    assert asset.duration_90k == 90000
    assert asset.size == 4096
    assert asset.clip_count == 51


def test_native_probe_emits_stream_metadata_for_automatic_titles():
    """The native --probe path must carry streams, not only SELECTED."""
    source = Path(__file__).parents[1] / "tools/bluray-title-reader/bluray_title_reader.cpp"
    text = source.read_text()
    probe_body = text.split("int probe(", 1)[1].split("int list_titles(", 1)[0]

    assert "print_streams(disc, selected.index);" in probe_body


def test_probe_main_title_parses_audio_and_subtitle_tracks(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: (
            "SELECTED\t2\t802\t90000\t4096\t51\n"
            "STREAM\tAUDIO\t0\t4352\t134\teng\n"
            "STREAM\tAUDIO\t1\t4354\t129\tzho\n"
            "STREAM\tSUBTITLE\t0\t4768\t144\teng\n"
            "STREAM\tSUBTITLE\t1\t4773\t144\tzho\n"
        ),
    )

    asset = bluray.probe_main_title(root)

    assert [(track.index, track.pid, track.language) for track in asset.audio_tracks] == [
        (0, 4352, "eng"),
        (1, 4354, "zho"),
    ]
    assert [(track.index, track.pid, track.language) for track in asset.subtitle_tracks] == [
        (0, 4768, "eng"),
        (1, 4773, "zho"),
    ]
    assert asset.default_audio_index == 1
    assert asset.default_subtitle_index == 1


def test_probe_main_title_parses_video_tracks_and_flags_dolby_vision(
    tmp_path: Path, monkeypatch
):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: (
            "SELECTED\t2\t802\t90000\t4096\t51\n"
            "STREAM\tVIDEO\t0\t4113\t36\teng\n"
            "STREAM\tVIDEO\t1\t4117\t36\teng\n"
            "STREAM\tAUDIO\t0\t4352\t134\teng\n"
        ),
    )

    asset = bluray.probe_main_title(root)

    assert [(track.index, track.pid) for track in asset.video_tracks] == [
        (0, 4113),
        (1, 4117),
    ]
    assert asset.dolby_vision is True
    assert asset.audio_tracks == (
        bluray.BlurayTrack(index=0, pid=4352, coding_type=134, language="eng"),
    )


def test_probe_main_title_single_video_is_not_dolby_vision(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: (
            "SELECTED\t2\t802\t90000\t4096\t51\n"
            "STREAM\tVIDEO\t0\t4113\t36\teng\n"
        ),
    )

    asset = bluray.probe_main_title(root)

    assert len(asset.video_tracks) == 1
    assert asset.dolby_vision is False


def test_probe_main_title_keeps_no_default_when_chinese_track_is_absent(
    tmp_path: Path, monkeypatch
):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: (
            "SELECTED\t2\t802\t90000\t4096\t51\n"
            "STREAM\tAUDIO\t0\t4352\t134\teng\n"
            "STREAM\tSUBTITLE\t0\t4768\t144\teng\n"
        ),
    )

    asset = bluray.probe_main_title(root)

    assert asset.default_audio_index is None
    assert asset.default_subtitle_index is None


def test_probe_main_title_prefers_near_equal_playlist_with_chinese_tracks(
    tmp_path: Path, monkeypatch
):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: (
            "SELECTED\t51\t801\t607604452\t49304739840\t51\n"
            "STREAM\tAUDIO\t0\t4352\t134\teng\n"
            "STREAM\tSUBTITLE\t0\t4768\t144\teng\n"
        ),
    )
    monkeypatch.setattr(
        bluray,
        "_run_list",
        lambda _root: (
            "TITLE\t50\t800\t607604406\t49179224064\t51\n"
            "TITLE\t51\t801\t607604452\t49304739840\t51\n"
        ),
    )
    monkeypatch.setattr(
        bluray,
        "_run_streams",
        lambda _root, title_index: (
            "STREAM\tAUDIO\t0\t4352\t134\teng\n"
            "STREAM\tAUDIO\t1\t4354\t129\tzho\n"
            "STREAM\tSUBTITLE\t0\t4768\t144\teng\n"
            "STREAM\tSUBTITLE\t1\t4773\t144\tzho\n"
            if title_index == 50
            else
            "STREAM\tAUDIO\t0\t4352\t134\teng\n"
            "STREAM\tSUBTITLE\t0\t4768\t144\teng\n"
        ),
    )

    asset = bluray.probe_main_title(root)

    assert asset.playlist == 800
    assert asset.default_audio_index == 1
    assert asset.default_subtitle_index == 1


def test_native_stream_probe_scans_every_mpls_clip():
    source = Path(__file__).parents[1] / "tools/bluray-title-reader/bluray_title_reader.cpp"
    text = source.read_text()
    stream_body = text.split("void print_streams(", 1)[1].split("bool read_exact(", 1)[0]

    assert "for (std::uint32_t clip_index = 0; clip_index < info->clip_count; ++clip_index)" in stream_body
    assert "info->clips[0]" not in stream_body


def test_native_stream_probe_emits_ordered_clip_fingerprint():
    """The native helper must expose the ordered MPLS clip sequence
    (clip_id + 90 kHz in/out points) so the edition graph can compare
    playlists strictly and never split a multi-clip seamless title."""
    source = Path(__file__).parents[1] / "tools/bluray-title-reader/bluray_title_reader.cpp"
    text = source.read_text()
    stream_body = text.split("void print_streams(", 1)[1].split("bool read_exact(", 1)[0]

    assert "CLIP\\t" in stream_body
    assert "clip.clip_id" in stream_body
    assert "clip.in_time" in stream_body
    assert "clip.out_time" in stream_body


def test_probe_main_title_parses_ordered_clips(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: (
            "SELECTED\t2\t802\t90000\t4096\t2\n"
            "CLIP\t0\t00000\t0\t45000000\n"
            "CLIP\t1\t00001\t45000000\t90000000\n"
        ),
    )

    asset = bluray.probe_main_title(root)

    assert asset.clip_count == 2
    assert [(clip.index, clip.clip_id, clip.in_time, clip.out_time) for clip in asset.clips] == [
        (0, "00000", 0, 45000000),
        (1, "00001", 45000000, 90000000),
    ]


def test_probe_main_title_parses_vinfo_video_attributes(tmp_path: Path, monkeypatch):
    """VINFO lines carry the real HDMV video format / frame rate onto the
    matching VIDEO track (used by the strict alternate-source signature)."""
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: (
            "SELECTED\t2\t802\t90000\t4096\t1\n"
            "VINFO\t0\t164\t24\n"
            "STREAM\tVIDEO\t0\t4113\t36\tzho\n"
        ),
    )

    asset = bluray.probe_main_title(root)

    assert len(asset.video_tracks) == 1
    track = asset.video_tracks[0]
    assert track.video_format == 164  # 2160p (real HDMV attribute)
    assert track.frame_rate == 24


def test_native_stream_probe_emits_vinfo_video_attributes():
    """The native helper must emit authoritative video format/rate (VINFO),
    never a guessed resolution."""
    source = Path(__file__).parents[1] / "tools/bluray-title-reader/bluray_title_reader.cpp"
    text = source.read_text()
    stream_body = text.split("void print_streams(", 1)[1].split("bool read_exact(", 1)[0]

    assert "VINFO\\t" in stream_body
    assert "stream.format" in stream_body


def test_probe_main_title_parses_vinfo_zero_rate_as_none(tmp_path: Path, monkeypatch):
    """BATCH-006: when the local libbluray header does not expose the precise
    frame-rate field, the helper emits 0 and the track keeps frame_rate=None
    so the strict signature stays incomplete (fail-closed)."""
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: (
            "SELECTED\t2\t802\t90000\t4096\t1\n"
            "VINFO\t0\t164\t0\n"
            "STREAM\tVIDEO\t0\t4113\t36\tzho\n"
        ),
    )

    asset = bluray.probe_main_title(root)

    track = asset.video_tracks[0]
    assert track.video_format == 164
    assert track.frame_rate is None


def test_probe_main_title_accepts_iso_source(tmp_path: Path, monkeypatch):
    bluray = _module()
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    monkeypatch.setattr(
        bluray,
        "_run_probe",
        lambda _root: "SELECTED\t4\t801\t607604452\t49334833384\t51\n",
    )

    asset = bluray.probe_main_title(iso)

    assert asset.root == iso.resolve()
    assert asset.source_kind == "iso"
    assert asset.entry_path == "HDATHOME/MAIN_TITLE.m2ts"


def test_list_main_titles_parses_candidates(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_list",
        lambda _root: (
            "TITLE\t4\t801\t607604452\t49334833384\t51\n"
            "TITLE\t7\t800\t120000\t1000000\t1\n"
        ),
    )

    candidates = bluray.list_main_titles(root)

    assert [(item.title_index, item.playlist, item.clip_count) for item in candidates] == [
        (4, 801, 51),
        (7, 800, 1),
    ]
    assert all(item.source_kind == "bdmv" for item in candidates)


def test_probe_main_title_accepts_manual_playlist(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_list",
        lambda _root: (
            "TITLE\t4\t801\t607604452\t49334833384\t51\n"
            "TITLE\t7\t800\t120000\t1000000\t1\n"
        ),
    )

    asset = bluray.probe_main_title(root, playlist=800)

    assert asset.title_index == 7
    assert asset.playlist == 800


def test_probe_main_title_rejects_missing_bdmv_index(tmp_path: Path):
    bluray = _module()

    with pytest.raises(bluray.BlurayTitleError, match="index.bdmv"):
        bluray.probe_main_title(tmp_path / "missing")


def test_probe_main_title_rejects_malformed_native_output(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(bluray, "_run_probe", lambda _root: "SELECTED\tbroken\n")

    with pytest.raises(bluray.BlurayTitleError, match="输出无效"):
        bluray.probe_main_title(root)


class _FakeReader:
    def __init__(self, asset, data: bytes):
        self.asset = asset
        self.data = data
        self.closed = False

    def is_alive(self) -> bool:
        return not self.closed

    def read(self, offset: int, length: int) -> bytes:
        return self.data[offset:offset + length]

    def close(self) -> None:
        self.closed = True


def test_iter_bluray_title_reuses_one_reader(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    asset = bluray.BlurayTitleAsset(
        root=root.resolve(),
        entry_path="HDATHOME/MAIN_TITLE.m2ts",
        title_index=0,
        playlist=1,
        duration_90k=90000,
        size=8,
        clip_count=2,
    )
    created = []

    def create_reader(_executable, value):
        reader = _FakeReader(value, b"01234567")
        created.append(reader)
        return reader

    monkeypatch.setattr(bluray, "_ReaderClient", create_reader)
    monkeypatch.setattr(bluray, "_find_executable", lambda: "/fake/reader")
    bluray.clear_bluray_reader_pool()

    assert b"".join(bluray.iter_bluray_title(asset, 0, 4, 3)) == b"0123"
    assert b"".join(bluray.iter_bluray_title(asset, 4, 4, 3)) == b"4567"
    assert len(created) == 1

    bluray.clear_bluray_reader_pool()
    assert created[0].closed


def test_reader_pool_evicts_oldest_disc(tmp_path: Path, monkeypatch):
    bluray = _module()
    created = []

    def create_reader(_executable, asset):
        reader = _FakeReader(asset, b"x")
        created.append(reader)
        return reader

    monkeypatch.setattr(bluray, "_ReaderClient", create_reader)
    monkeypatch.setattr(bluray, "_find_executable", lambda: "/fake/reader")
    bluray.clear_bluray_reader_pool()

    for index in range(5):
        root = _disc_root(tmp_path, f"disc-{index}")
        asset = bluray.BlurayTitleAsset(
            root=root.resolve(),
            entry_path="HDATHOME/MAIN_TITLE.m2ts",
            title_index=0,
            playlist=index,
            duration_90k=90000,
            size=1,
            clip_count=1,
        )
        assert b"".join(bluray.iter_bluray_title(asset, 0, 1, 1)) == b"x"

    assert len(created) == 5
    assert created[0].closed
    assert sum(not reader.closed for reader in created) == 4
    bluray.clear_bluray_reader_pool()
