from pathlib import Path

from bluray_fidelity.bluray import BlurayTitleAsset, BlurayTrack
from bluray_fidelity.m2ts import M2tsTitleBuilder


def test_builder_writes_selected_title_as_original_m2ts_bytes(tmp_path: Path, monkeypatch):
    asset = BlurayTitleAsset(
        root=tmp_path / "source.iso",
        entry_path="HDATHOME/MAIN_TITLE.m2ts",
        title_index=2,
        playlist=800,
        duration_90k=180_000,
        size=6,
        clip_count=2,
        source_kind="iso",
        audio_tracks=(BlurayTrack(0, 4352, 129, "zho"),),
        subtitle_tracks=(BlurayTrack(0, 4608, 144, "zho"),),
    )
    monkeypatch.setattr(
        "bluray_fidelity.m2ts.iter_bluray_title",
        lambda _asset, chunk_size: iter((b"abc", b"def")),
    )

    output = tmp_path / "main-title.m2ts.partial"
    metadata = M2tsTitleBuilder().build(asset, output, title="ignored")

    assert output.read_bytes() == b"abcdef"
    assert metadata["duration_ns"] == 2_000_000_000
    # DIST-009 起轨道 dict 附带 video_format/frame_rate 元数据键；
    # 字节合同只关心既有关键字段（超集兼容比较）。
    def _project(tracks):
        return tuple({k: v for k, v in track.items() if k in ("index", "pid", "coding_type", "language")} for track in tracks)
    assert _project(metadata["audio_tracks"]) == ({"index": 0, "pid": 4352, "coding_type": 129, "language": "zho"},)
    assert _project(metadata["subtitle_tracks"]) == ({"index": 0, "pid": 4608, "coding_type": 144, "language": "zho"},)
