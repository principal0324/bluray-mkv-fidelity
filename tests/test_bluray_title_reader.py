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
    source = Path(__file__).parents[1] / "native/bluray-title-reader/bluray_title_reader.cpp"
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
    source = Path(__file__).parents[1] / "native/bluray-title-reader/bluray_title_reader.cpp"
    text = source.read_text()
    stream_body = text.split("void print_streams(", 1)[1].split("bool read_exact(", 1)[0]

    assert "for (std::uint32_t clip_index = 0; clip_index < info->clip_count; ++clip_index)" in stream_body
    assert "info->clips[0]" not in stream_body


def test_native_stream_probe_emits_ordered_clip_fingerprint():
    """The native helper must expose the ordered MPLS clip sequence
    (clip_id + 90 kHz in/out points) so the edition graph can compare
    playlists strictly and never split a multi-clip seamless title."""
    source = Path(__file__).parents[1] / "native/bluray-title-reader/bluray_title_reader.cpp"
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
    source = Path(__file__).parents[1] / "native/bluray-title-reader/bluray_title_reader.cpp"
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


# ---------------------------------------------------------------------------
# play item 边界（每段 MPLS play item 一个独立字节区间）
# ---------------------------------------------------------------------------

def _title_asset(
    root: Path,
    *,
    size: int,
    clip_count: int,
    title_index: int = 3,
    playlist: int = 800,
):
    bluray = _module()
    return bluray.BlurayTitleAsset(
        root=root.resolve(),
        entry_path="HDATHOME/MAIN_TITLE.m2ts",
        title_index=title_index,
        playlist=playlist,
        duration_90k=90000,
        size=size,
        clip_count=clip_count,
    )


def test_parse_item_bounds_keeps_mpls_order_and_contiguous_ranges():
    bluray = _module()

    bounds = bluray._parse_item_bounds(
        "ITEM\t0\t0\t192\n"
        "ITEM\t1\t192\t384\n"
        "ITEM\t2\t576\t192\n"
    )

    assert bounds == ((0, 192), (192, 384), (576, 192))


@pytest.mark.parametrize(
    "output, message",
    [
        ("ITEM\t0\t0\t192\nITEM\t2\t192\t384\n", "顺序或范围"),
        ("ITEM\t0\t0\t0\n", "顺序或范围"),
        ("ITEM\t0\t0\t192\nITEM\t1\t384\t192\n", "不连续"),
        ("ITEM\t0\t192\t192\n", "起点不为 0"),
        ("ITEM\t0\t0\n", "ITEM 字段错误"),
        ("ITEM\t0\t0\tx\n", "ITEM 字段不是整数"),
        ("STREAM\tVIDEO\t0\t4113\t36\tzho\n", "没有 play item"),
    ],
)
def test_parse_item_bounds_rejects_broken_tables(output: str, message: str):
    bluray = _module()

    with pytest.raises(bluray.BlurayTitleError, match=message):
        bluray._parse_item_bounds(output)


def test_bluray_item_bounds_parses_and_caches_reader_output(tmp_path: Path, monkeypatch):
    bluray = _module()
    root = _disc_root(tmp_path)
    asset = _title_asset(root, size=768, clip_count=3)
    calls = []

    def fake_run(_root, title_index):
        calls.append(title_index)
        return "ITEM\t0\t0\t192\nITEM\t1\t192\t384\nITEM\t2\t576\t192\n"

    monkeypatch.setattr(bluray, "_run_item_bounds", fake_run)
    bluray.clear_bluray_reader_pool()

    assert bluray.bluray_item_bounds(asset) == ((0, 192), (192, 384), (576, 192))
    assert bluray.bluray_item_bounds(asset) == ((0, 192), (192, 384), (576, 192))
    assert calls == [asset.title_index]

    bluray.clear_bluray_reader_pool()
    assert bluray.bluray_item_bounds(asset) == ((0, 192), (192, 384), (576, 192))
    assert calls == [asset.title_index, asset.title_index]


def test_bluray_item_bounds_fails_closed_when_ranges_do_not_match_asset(
    tmp_path: Path, monkeypatch
):
    bluray = _module()
    root = _disc_root(tmp_path)
    monkeypatch.setattr(
        bluray,
        "_run_item_bounds",
        lambda _root, _title: "ITEM\t0\t0\t192\nITEM\t1\t192\t384\n",
    )
    bluray.clear_bluray_reader_pool()

    with pytest.raises(bluray.BlurayTitleError, match="数量不符"):
        bluray.bluray_item_bounds(_title_asset(root, size=576, clip_count=3))

    with pytest.raises(bluray.BlurayTitleError, match="总长与标题大小不符"):
        bluray.bluray_item_bounds(_title_asset(root, size=1_000, clip_count=2))


class _FakeItemReader:
    """假 reader：按冻结边界在整条标题字节流上切片并记录每次 READITEM。"""

    def __init__(self, asset, data: bytes, bounds):
        self.asset = asset
        self.data = data
        self.bounds = bounds
        self.calls: list[tuple[int, int, int]] = []
        self.closed = False

    def is_alive(self) -> bool:
        return not self.closed

    def read_item(self, item_index: int, offset_in_item: int, length: int) -> bytes:
        start, item_length = self.bounds[item_index]
        if offset_in_item > item_length or length > item_length - offset_in_item:
            raise AssertionError("iter_bluray_item 越界读取")
        self.calls.append((item_index, offset_in_item, length))
        return self.data[start + offset_in_item:start + offset_in_item + length]

    def close(self) -> None:
        self.closed = True


def _with_fake_item_reader(tmp_path: Path, monkeypatch, *, size: int, clip_count: int,
                           bounds):
    bluray = _module()
    root = _disc_root(tmp_path, f"disc-{size}-{clip_count}")
    asset = _title_asset(root, size=size, clip_count=clip_count)
    data = bytes(range(size))
    reader = _FakeItemReader(asset, data, tuple(bounds))
    monkeypatch.setattr(bluray, "_ReaderClient", lambda _exe, _asset: reader)
    monkeypatch.setattr(bluray, "_find_executable", lambda: "/fake/reader")
    monkeypatch.setattr(bluray, "bluray_item_bounds", lambda _asset: tuple(bounds))
    bluray.clear_bluray_reader_pool()
    return bluray, asset, reader


def test_iter_bluray_item_reads_each_reference_of_a_repeated_clip(
    tmp_path: Path, monkeypatch
):
    """同一 clip 在 MPLS 中出现 K 次就有 K 段独立区间：第 0/2 段必须
    各自产出自己那一段字节，而不是复用同一区间或跳过重复段。"""
    bluray, asset, reader = _with_fake_item_reader(
        tmp_path, monkeypatch, size=12, clip_count=3,
        bounds=((0, 4), (4, 4), (8, 4)),
    )

    assert b"".join(bluray.iter_bluray_item(asset, 0, 4)) == bytes(range(0, 4))
    assert b"".join(bluray.iter_bluray_item(asset, 1, 4)) == bytes(range(4, 8))
    assert b"".join(bluray.iter_bluray_item(asset, 2, 4)) == bytes(range(8, 12))
    # 读取前先采样一个窗口做空包 PID 加固判定（霸王别姬读路径改写防护），
    # 因此每个 item 的首个请求是该 item 的头窗口采样，随后才是顺序读。
    assert reader.calls == [
        (0, 0, 4), (0, 0, 4),
        (1, 0, 4), (1, 0, 4),
        (2, 0, 4), (2, 0, 4),
    ]
    bluray.clear_bluray_reader_pool()


def test_iter_bluray_item_chunks_and_rejects_out_of_range(tmp_path: Path, monkeypatch):
    bluray, asset, reader = _with_fake_item_reader(
        tmp_path, monkeypatch, size=12, clip_count=3,
        bounds=((0, 4), (4, 6), (10, 2)),
    )

    assert b"".join(bluray.iter_bluray_item(asset, 1, 4)) == bytes(range(4, 10))
    assert reader.calls == [(1, 0, 6), (1, 0, 4), (1, 4, 2)]

    with pytest.raises(bluray.BlurayTitleError, match="序号越界"):
        list(bluray.iter_bluray_item(asset, 3))
    with pytest.raises(bluray.BlurayTitleError, match="序号越界"):
        list(bluray.iter_bluray_item(asset, -1))
    with pytest.raises(bluray.BlurayTitleError, match="块大小无效"):
        list(bluray.iter_bluray_item(asset, 0, 0))
    bluray.clear_bluray_reader_pool()


# ---------------------------------------------------------------------------
# 真实管道上的 READ/READITEM framing（假子进程，不 monkeypatch reader 客户端）
# ---------------------------------------------------------------------------

_FAKE_READER_SOURCE = '''#!/usr/bin/env python3
"""Fake reader child: real pipes, canned replies, request log."""
import os
import sys

MODE = {mode!r}
LOG = {log!r}
ITEM_BOUNDS = {bounds!r}
PAYLOAD = {payload!r}


def log(line):
    with open(LOG, "a", encoding="ascii") as stream:
        stream.write(line + "\\n")


def reply(body):
    os.write(1, body)


def main():
    if "--item-bounds" in sys.argv:
        for index, (start, length) in enumerate(ITEM_BOUNDS):
            os.write(1, ("ITEM\\t%d\\t%d\\t%d\\n" % (index, start, length)).encode("ascii"))
        return 0
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.rstrip("\\n")
        if line == "QUIT":
            return 0
        log(line)
        if MODE == "error":
            reply(b"ERR\\titem bounds unavailable\\n")
            continue
        parts = line.split("\\t")
        if parts[0] == "READITEM":
            item = int(parts[1])
            start = ITEM_BOUNDS[item][0] + int(parts[2])
            length = int(parts[3])
        else:
            start = int(parts[1])
            length = int(parts[2])
        payload = PAYLOAD[start:start + length]
        if MODE == "oversize":
            reply(b"OK\\t%d\\n" % (length * 2) + payload + payload)
        elif MODE == "short":
            reply(b"OK\\t%d\\n" % length + payload[: max(0, length - 1)])
            return 0
        else:
            reply(b"OK\\t%d\\n" % length + payload)
    return 0


sys.exit(main())
'''


def _fake_reader_child(tmp_path: Path, *, mode: str, bounds, payload: bytes = bytes(range(64))):
    log = tmp_path / "fake-reader-requests.log"
    script = tmp_path / "fake-bluray-reader.py"
    script.write_text(_FAKE_READER_SOURCE.format(
        mode=mode,
        log=str(log),
        bounds=tuple(bounds),
        payload=payload,
    ))
    script.chmod(0o755)
    return script, log


class _RealReader:
    """真实管道上的 reader：假子进程按冻结边界应答并记录收到的请求行。"""

    def __init__(self, module, script: Path, log: Path, asset):
        self.module = module
        self.script = script
        self.log = log
        self.asset = asset

    def client(self):
        return self.module._ReaderClient(str(self.script), self.asset)

    def requests(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()

    def close(self) -> None:
        self.module.clear_bluray_reader_pool()


def _real_reader(tmp_path: Path, monkeypatch, *, mode: str, bounds, size: int,
                 clip_count: int, payload: bytes = bytes(range(64))) -> _RealReader:
    bluray = _module()
    script, log = _fake_reader_child(tmp_path, mode=mode, bounds=bounds, payload=payload)
    root = _disc_root(tmp_path, f"disc-{mode}-{size}")
    asset = _title_asset(root, size=size, clip_count=clip_count)
    monkeypatch.setattr(bluray, "_find_executable", lambda: str(script))
    bluray.clear_bluray_reader_pool()
    return _RealReader(bluray, script, log, asset)


def test_reader_client_frames_read_and_read_item_over_real_pipes(tmp_path: Path, monkeypatch):
    reader = _real_reader(
        tmp_path, monkeypatch, mode="ok", bounds=((0, 4), (4, 4)), size=8, clip_count=2
    )
    client = reader.client()
    try:
        assert client.read(2, 4) == bytes(range(2, 6))
        assert client.read_item(1, 0, 4) == bytes(range(4, 8))
        assert client.read_item(0, 2, 2) == bytes(range(2, 4))
        assert client.read(8, 0) == b""
    finally:
        client.close()
        reader.close()
    assert reader.requests() == [
        "READ\t2\t4",
        "READITEM\t1\t0\t4",
        "READITEM\t0\t2\t2",
        "READ\t8\t0",
    ]


def test_reader_client_rejects_declared_length_mismatch(tmp_path: Path, monkeypatch):
    reader = _real_reader(
        tmp_path, monkeypatch, mode="oversize", bounds=((0, 4), (4, 4)), size=8, clip_count=2
    )
    client = reader.client()
    try:
        with pytest.raises(reader.module.BlurayTitleError, match="返回长度不符"):
            client.read(0, 4)
    finally:
        client.close()
        reader.close()


def test_reader_client_surfaces_short_payload_and_error_reply(tmp_path: Path, monkeypatch):
    reader = _real_reader(
        tmp_path, monkeypatch, mode="short", bounds=((0, 4), (4, 4)), size=8, clip_count=2
    )
    client = reader.client()
    try:
        with pytest.raises(reader.module.BlurayTitleError, match="输出不足"):
            client.read(0, 4)
    finally:
        client.close()
        reader.close()

    reader2 = _real_reader(
        tmp_path, monkeypatch, mode="error", bounds=((0, 4), (4, 4)), size=8, clip_count=2
    )
    client2 = reader2.client()
    try:
        with pytest.raises(reader2.module.BlurayTitleError, match="item bounds unavailable"):
            client2.read_item(0, 0, 4)
    finally:
        client2.close()
        reader2.close()


def test_reader_client_rejects_item_range_before_sending(tmp_path: Path, monkeypatch):
    """越出本段 item 的读取在客户端就被拒绝，不落到 wire 上。"""
    reader = _real_reader(
        tmp_path, monkeypatch, mode="ok", bounds=((0, 4), (4, 4)), size=8, clip_count=2
    )
    client = reader.client()
    try:
        with pytest.raises(reader.module.BlurayTitleError, match="超出结尾"):
            client.read_item(0, 2, 3)
        with pytest.raises(reader.module.BlurayTitleError, match="序号越界"):
            client.read_item(2, 0, 1)
    finally:
        client.close()
        reader.close()
    assert reader.requests() == []


def test_iter_bluray_item_over_real_pipes(tmp_path: Path, monkeypatch):
    reader = _real_reader(
        tmp_path, monkeypatch, mode="ok", bounds=((0, 4), (4, 6), (10, 2)),
        size=12, clip_count=3,
    )
    try:
        assert b"".join(reader.module.iter_bluray_item(reader.asset, 1, 4)) == bytes(range(4, 10))
        assert b"".join(reader.module.iter_bluray_item(reader.asset, 2, 2)) == bytes(range(10, 12))
    finally:
        reader.close()
    assert reader.requests() == [
        # 每个 item 的首个请求是空包 PID 加固判定的头窗口采样
        "READITEM\t1\t0\t6",
        "READITEM\t1\t0\t4",
        "READITEM\t1\t4\t2",
        "READITEM\t2\t0\t2",
        "READITEM\t2\t0\t2",
    ]


# ---------------------------------------------------------------------------
# native 源码断言：READITEM/--item-bounds 路径回归时立刻失败
# ---------------------------------------------------------------------------

def _native_source() -> str:
    source = Path(__file__).parents[1] / "native/bluray-title-reader/bluray_title_reader.cpp"
    return source.read_text()


def test_native_reader_exposes_play_item_bounds_cli():
    text = _native_source()
    bounds_body = text.split("int print_item_bounds(", 1)[1].split("int serve(", 1)[0]

    assert "--item-bounds" in text
    assert "ITEM\\t" in bounds_body
    assert "collect_item_bounds(" in bounds_body


def test_native_play_item_bounds_cross_check_mpls_packet_partition():
    """每个 play item 起点来自 bd_seek_playitem，且必须与 MPLS 包划分
    完全一致；libbluray 该返回值不报错，只有交叉校验能挡住错位。"""
    text = _native_source()
    init_body = text.split("bool ItemBounds::init(", 1)[1].split("bool ItemBounds::start_of(", 1)[0]
    start_body = text.split("bool ItemBounds::start_of(", 1)[1].split("bool ItemBounds::item(", 1)[0]
    item_body = text.split("bool ItemBounds::item(", 1)[1].split("bool collect_item_bounds(", 1)[0]

    assert "info->clips[index].pkt_count" in init_body
    assert "total != title_size" in init_body
    assert "kPacketSize" in init_body
    assert "bd_seek_playitem(disc_, index)" in start_body
    assert "!= expected_[index]" in start_body
    assert "title_size_" in item_body


def test_native_serve_adds_readitem_and_validates_item_range():
    text = _native_source()
    serve_body = text.split("int serve(", 1)[1].split("}  // namespace", 1)[0]
    read_body = serve_body.split('if (command == "READITEM")', 1)[1]

    assert '"READ"' in serve_body
    assert '"READITEM"' in serve_body
    assert "read_range(disc, selected.size, offset, length," in serve_body
    assert "item_bounds.init(disc, title_index, selected.size)" in serve_body
    # 2026-09-15：READITEM 改用 item_cached（首次校验式 seek，之后复用）——
    # 每个 chunk 都 item()+invalidate 会让 22.9 GB 的 item 变成 O(n^2)。
    assert "item_bounds.item_cached(" in read_body
    assert "length > item_length - offset" in read_body
    assert "item_start + offset" in read_body
    assert "send_payload(output)" in read_body
    assert "if (seeked) {\n            cursor.invalidate();" in read_body


# ---------------------------------------------------------------------------
# native 源码断言：字节偏移定位经 item 表 + bd_tell 校准，不再用 bd_seek
# （真盘只读实测：seek 之后第一次读取会回退 768 字节 / 4 个包）
# ---------------------------------------------------------------------------

def _native_position_at_body() -> str:
    text = _native_source()
    return text.split("bool position_at(", 1)[1].split("bool read_range(", 1)[0]


def _native_read_range_body() -> str:
    text = _native_source()
    return text.split("bool read_range(", 1)[1].split("void send_error(", 1)[0]


def _native_take_exact_body() -> str:
    text = _native_source()
    return text.split("bool take_exact(", 1)[1].split("bool position_at(", 1)[0]


def _native_item_bounds_body(name: str) -> str:
    """取 ItemBounds::<name>() 的函数体（到下一个同级定义为止）。"""
    next_definition = {
        "init": "bool ItemBounds::start_of(",
        "start_of": "bool ItemBounds::item(",
        "item": "bool ItemBounds::item_cached(",
        "item_cached": "bool ItemBounds::reposition(",
        "reposition": "bool collect_item_bounds(",
    }[name]
    text = _native_source()
    return text.split(f"bool ItemBounds::{name}(", 1)[1].split(next_definition, 1)[0]


def test_native_read_range_repositions_mid_item_through_item_table():
    """中段字节偏移必须经 item 表重定位：bd_seek() 只对齐到最近的接入单元、
    bd_seek_time() 的单位是 90 kHz tick，两者都不能按字节定位。"""
    text = _native_source()
    read_body = _native_read_range_body()
    position_body = _native_position_at_body()
    start_body = _native_item_bounds_body("start_of")
    reposition_body = _native_item_bounds_body("reposition")

    assert "bd_seek(disc" not in text
    assert "bd_seek_playitem(disc_, index)" in start_body
    assert "expected_[index]" in start_body
    assert "expected_[index + 1] <= offset" in reposition_body
    assert "!item_bounds->reposition(offset, &item_start)" in position_body
    assert "needs_reposition(*cursor, offset)" in read_body
    assert "!position_at(disc, item_bounds, title_size, offset, cursor)" in read_body


def test_native_position_at_validates_seek_result_and_calibrates_rewind():
    """bd_seek_playitem 返回值与 MPLS 包划分不一致时必须失败；seek 之后第一次
    读取会把起点之前的 PAT/PMT/PCR 包一起返回，位置只能用 bd_tell() 校准。"""
    position_body = _native_position_at_body()
    start_body = _native_item_bounds_body("start_of")

    assert "!= expected_[index]" in start_body
    assert "title_size <= item_start" in position_body
    assert "bd_tell(disc)" in position_body
    assert "probe.empty()" in position_body
    assert "actual < probe.size()" in position_body
    assert "offset < probe_start" in position_body
    assert "bd_tell(disc) != offset" in position_body
    # 定位失败直接返回 false，绝不落到按猜出的位置供字节
    assert position_body.index("return false;") < position_body.index("cursor->known = true;")


def test_native_position_at_start_offset_keeps_probe_bytes_without_discard():
    """offset 0 与 item 起点：探测读出的字节全部留给下一次读取，丢弃量为 0；
    只有当目标越过已读出的字节时才丢弃，且丢弃量以 bd_tell() 的实际位置为准。"""
    text = _native_source()
    cursor_body = text.split("struct ReadCursor {", 1)[1].split("};", 1)[0]
    position_body = _native_position_at_body()
    take_body = _native_take_exact_body()
    discard_body = text.split("bool discard_exact(", 1)[1].split("struct ReadCursor {", 1)[0]

    assert "std::uint64_t position = 0;" in cursor_body
    assert "bool known = true;" in cursor_body
    # 探测只读一个包，丢弃量因此至多一段 play item 加一个包
    assert "std::min<std::uint64_t>(kPacketSize, title_size - item_start)" in position_body
    assert "cursor->lookahead.assign(" in position_body
    assert position_body.index("if (offset <= actual) {") < position_body.index(
        "discard_exact(disc, offset - actual)"
    )
    # 已读出的字节先交付，不足的部分才 bd_read
    assert take_body.index("cursor->lookahead") < take_body.index(
        "read_exact(disc, remaining, output)"
    )
    assert "while (remaining > 0)" in discard_body


def test_native_jumping_read_fails_closed_without_validated_item_table():
    """item 表无法与标题包划分交叉校验时，跳跃读取必须报错；顺序读取不需要
    重定位、因此不依赖 item 表，保持原快路径。"""
    text = _native_source()
    serve_body = text.split("int serve(", 1)[1].split("}  // namespace", 1)[0]
    read_body = _native_read_range_body()
    init_body = _native_item_bounds_body("init")

    assert "total != title_size" in init_body
    assert "needs_reposition(cursor, offset)" in serve_body
    assert 'send_error("item bounds unavailable")' in serve_body
    # item() 的 seek 之后位置不可确认（seek 回退），游标连同 lookahead 作废
    assert "cursor.invalidate();" in serve_body
    assert "!position_at(disc, item_bounds, title_size, offset, cursor)" in read_body


def test_native_item_cached_reuses_verified_range_without_reseek():
    """顺序流式读不得每个 chunk 重新 seek：区间验证只发生一次。

    2026-09-15 生产实证：READITEM 每个请求都 item()（bd_seek_playitem）+ 作废
    游标，read_range 于是重新定位并用 discard_exact 走过段内前缀——22.9 GB 的
    item 在 1 MB/请求下累计要丢弃约 260 TB，实测吞吐跌到 0.8 MB/s（同一台机器
    上的直读路径为 98 MB/s）。判据：item_cached 命中缓存时不得调用 start_of，
    且调用方只在 seeked 为真时作废游标。
    """
    body = _native_item_bounds_body("item_cached")
    assert "verified_[index]" in body
    assert "return true;" in body
    # 命中缓存的分支必须早于 item() 调用（否则仍是每请求一次 seek）
    cache_hit = body.index("verified_[index]")
    seek_path = body.index("item(index, &item_start, &item_length)")
    assert cache_hit < seek_path
    # 只有真正 seek 过的路径才报告 seeked=true
    assert "*seeked = true;" in body

    init_body = _native_item_bounds_body("init")
    assert "verified_.assign(count, 0);" in init_body


def test_native_serve_only_invalidates_cursor_after_verified_seek():
    text = _native_source()
    serve_body = text.split("int serve(", 1)[1].split("}  // namespace", 1)[0]
    read_body = serve_body.split('if (command == "READITEM")', 1)[1]
    assert "bool seeked = false;" in read_body
    assert read_body.count("cursor.invalidate();") == 1
    guarded = read_body.split("if (seeked) {", 1)[1].split("}", 1)[0]
    assert "cursor.invalidate();" in guarded
