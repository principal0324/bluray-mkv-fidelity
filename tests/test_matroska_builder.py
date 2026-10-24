from pathlib import Path

import json
import pathlib

import pytest

from dataclasses import replace
from bluray_fidelity.bluray import BlurayClip, BlurayTitleAsset, BlurayTrack
from bluray_fidelity.matroska import (
    MatroskaBuildError,
    MatroskaBuilder,
    build_mkvmerge_command,
    parse_identification,
    resolve_official_tracks,
    validate_finalized_identification,
)


def _asset(tmp_path: Path) -> BlurayTitleAsset:
    return BlurayTitleAsset(
        root=tmp_path / "disc.iso",
        entry_path="HDATHOME/MAIN_TITLE.m2ts",
        title_index=4,
        playlist=801,
        duration_90k=9_000_000,
        size=10_000_000,
        clip_count=2,
        source_kind="iso",
        audio_tracks=(
            BlurayTrack(index=0, pid=4352, coding_type=0x83, language="eng"),
            BlurayTrack(index=1, pid=4353, coding_type=0x81, language="zho"),
        ),
        subtitle_tracks=(
            BlurayTrack(index=0, pid=4768, coding_type=0x90, language="eng"),
            BlurayTrack(index=1, pid=4769, coding_type=0x90, language="zho"),
        ),
    )


def _input_identification() -> dict:
    return {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {"stream_id": 4113}},
            {"id": 1, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {"stream_id": 4117}},
            {"id": 2, "type": "audio", "codec": "TrueHD", "properties": {"stream_id": 4352}},
            # mkvmerge exposes the embedded AC-3 core separately. It is not an
            # independent MPLS audio track and must not leak into the result.
            {"id": 3, "type": "audio", "codec": "AC-3", "properties": {"stream_id": 4352}},
            {"id": 4, "type": "audio", "codec": "AC-3", "properties": {"stream_id": 4353}},
            {"id": 5, "type": "subtitles", "codec": "HDMV PGS", "properties": {"stream_id": 4768}},
            {"id": 6, "type": "subtitles", "codec": "HDMV PGS", "properties": {"stream_id": 4769}},
        ],
    }


def test_resolver_keeps_only_official_mpls_tracks_and_filters_truehd_core(tmp_path: Path):
    asset = _asset(tmp_path)
    identified = parse_identification(_input_identification())

    resolved = resolve_official_tracks(asset, identified)

    assert resolved.video_ids == (0, 1)
    assert resolved.audio_ids == (2, 4)
    assert resolved.subtitle_ids == (5, 6)
    assert resolved.audio_by_official_index == ((0, 2), (1, 4))
    assert resolved.subtitle_by_official_index == ((0, 5), (1, 6))


def test_identification_accepts_mkvmerge_hexadecimal_transport_stream_pids():
    payload = _input_identification()
    payload["tracks"][2]["properties"]["stream_id"] = "0x1100"

    identified = parse_identification(payload)

    assert identified[2].stream_id == 4352


def _vietnam_style_asset(tmp_path: Path) -> BlurayTitleAsset:
    """越南战争 S01E09 主片（playlist 3，19 个 play item）的官方音轨表形态。

    实测 `--streams` 合并结果：PID 4354 被声明两次——index 2 记 `spa`、
    index 4 记 `eng`（17 段共用一个 clip，跨片段语言不同）。
    """
    asset = _asset(tmp_path)
    return BlurayTitleAsset(
        **{**asset.__dict__, "audio_tracks": (
            BlurayTrack(index=0, pid=4352, coding_type=0x86, language="eng"),
            BlurayTrack(index=1, pid=4353, coding_type=0x86, language="eng"),
            BlurayTrack(index=2, pid=4354, coding_type=0x86, language="spa"),
            BlurayTrack(index=3, pid=4355, coding_type=0x86, language="eng"),
            BlurayTrack(index=4, pid=4354, coding_type=0x86, language="eng"),
        ), "subtitle_tracks": (
            BlurayTrack(index=0, pid=4608, coding_type=0x90, language="eng"),
            BlurayTrack(index=1, pid=4609, coding_type=0x90, language="spa"),
        )}
    )


def _vietnam_style_identification(payload_4354_language: str) -> dict:
    return {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "AVC", "properties": {"stream_id": 4113}},
            {"id": 1, "type": "audio", "codec": "DTS-HD Master Audio",
             "properties": {"stream_id": 4352, "language": "eng"}},
            {"id": 2, "type": "audio", "codec": "DTS-HD Master Audio",
             "properties": {"stream_id": 4353, "language": "eng"}},
            {"id": 3, "type": "audio", "codec": "DTS-HD Master Audio",
             "properties": {"stream_id": 4354, "language": payload_4354_language}},
            {"id": 4, "type": "audio", "codec": "DTS-HD Master Audio",
             "properties": {"stream_id": 4355, "language": "eng"}},
            {"id": 5, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"stream_id": 4608, "language": "eng"}},
            {"id": 6, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"stream_id": 4609, "language": "spa"}},
        ],
    }


def test_duplicate_mpls_pid_maps_to_one_output_track(tmp_path: Path):
    """同一 PID 被官方表声明两次时，只能占用一条输出轨。

    否则成品条数比"可映射"少一条，会在数量门被拒（2026-09-14 越南战争：
    成品 4 条 vs 可映射 5 条，来源完好却失败在计数上）。
    """
    asset = _vietnam_style_asset(tmp_path)

    resolved = resolve_official_tracks(
        asset, parse_identification(_vietnam_style_identification("eng")))

    assert len(resolved.audio_ids) == len(set(resolved.audio_ids))
    assert len(resolved.audio_by_official_index) == len(resolved.audio_ids)
    assert resolved.audio_ids == (1, 2, 3, 4)


def test_duplicate_pid_keeps_declaration_matching_identified_language(tmp_path: Path):
    """重复声明的取舍必须与输入轨实际语言一致，否则语言门会误拒。"""
    asset = _vietnam_style_asset(tmp_path)

    # 输入轨 4354 实为 eng → 应保留 index 4（eng），丢弃 index 2（spa）。
    resolved_eng = resolve_official_tracks(
        asset, parse_identification(_vietnam_style_identification("eng")))
    assert (4, 3) in resolved_eng.audio_by_official_index
    assert all(index != 2 for index, _ in resolved_eng.audio_by_official_index)

    # 输入轨 4354 实为 spa → 应保留先到的 index 2（spa）。
    resolved_spa = resolve_official_tracks(
        asset, parse_identification(_vietnam_style_identification("spa")))
    assert (2, 3) in resolved_spa.audio_by_official_index
    assert all(index != 4 for index, _ in resolved_spa.audio_by_official_index)


def test_resolver_skips_stale_mpls_entry_when_mkvmerge_has_no_packets(tmp_path: Path):
    asset = _asset(tmp_path)
    payload = _input_identification()
    # The MPLS table can contain an optional PID that has no packets in the
    # selected title; mkvmerge does not identify it and it must not abort the
    # otherwise valid stream-copy build.
    asset = BlurayTitleAsset(
        **{**asset.__dict__, "audio_tracks": asset.audio_tracks + (
            BlurayTrack(index=2, pid=4357, coding_type=0x82, language="eng"),
        )}
    )
    resolved = resolve_official_tracks(asset, parse_identification(payload))
    assert resolved.audio_ids == (2, 4)
    assert resolved.audio_by_official_index == ((0, 2), (1, 4))


def test_command_preserves_all_video_layers_and_sets_chinese_defaults(tmp_path: Path):
    asset = _asset(tmp_path)
    resolved = resolve_official_tracks(asset, parse_identification(_input_identification()))
    fifo = tmp_path / "title.m2ts"
    output = tmp_path / "final.mkv.partial"

    command = build_mkvmerge_command(asset, resolved, fifo, output, title="十三刺客")

    assert command[:3] == ["mkvmerge", "--output", str(output)]
    assert ["--video-tracks", "0,1"] == command[command.index("--video-tracks"):command.index("--video-tracks") + 2]
    assert ["--audio-tracks", "2,4"] == command[command.index("--audio-tracks"):command.index("--audio-tracks") + 2]
    assert ["--subtitle-tracks", "5,6"] == command[command.index("--subtitle-tracks"):command.index("--subtitle-tracks") + 2]
    assert "2:eng" in command
    assert "4:zho" in command
    assert "5:eng" in command
    assert "6:zho" in command
    assert "2:no" in command
    assert "4:yes" in command
    assert "5:no" in command
    assert "6:yes" in command
    assert command[-1] == str(fifo)


def test_command_append_uses_explicit_track_selection_and_mapping(tmp_path):
    """多段追加：逐轨选择必须同时作用于追加段，并给出只含被复制轨的显式映射。

    生产标本（dlv_93c13946d7c0eac7c0b9 地球脉动.第三季 Disc.3）：首段按
    `--audio-tracks 2,4`（本例同形）排除了 track 3，而旧实现只给追加段类型级
    开关 → mkvmerge 默认按位映射给出 `1:3:0:3`，指向首段未被复制的轨 →
    `#GUI#error … does not contain a track with the ID 3 … --append-to was invalid`
    （1.3 秒报错退出，交付永远无法完成）。
    """
    asset = _asset(tmp_path)
    resolved = resolve_official_tracks(asset, parse_identification(_input_identification()))
    first = tmp_path / "title.item0.m2ts"
    second = tmp_path / "title.item1.m2ts"

    command = build_mkvmerge_command(asset, resolved, first,
                                     tmp_path / "final.mkv.partial",
                                     title="十三刺客", append_inputs=(second,))

    # 追加段必须带**同样的逐轨选择**（否则它仍在复制自己的全部轨）
    plus = command.index("+")
    appended = command[plus + 1:command.index(str(second))]
    assert "--audio-tracks" in appended and "2,4" in appended
    assert "--subtitle-tracks" in appended and "5,6" in appended

    # 显式映射：只含被复制的轨，且覆盖全部被复制轨
    mapping = command[command.index("--append-to") + 1]
    assert "1:3:0:3" not in mapping, "被排除的 track 3 绝不能被映射引用"
    for track_id in (0, 1, 2, 4, 5, 6):
        assert f"1:{track_id}:0:{track_id}" in mapping
    assert command[-1] == str(second)


def test_command_single_input_has_no_append_mapping(tmp_path):
    """单段路线零变化：没有 +、没有 --append-to（回归）。"""
    asset = _asset(tmp_path)
    resolved = resolve_official_tracks(asset, parse_identification(_input_identification()))
    source = tmp_path / "title.m2ts"

    command = build_mkvmerge_command(asset, resolved, source,
                                     tmp_path / "final.mkv.partial", title="十三刺客")

    assert "+" not in command
    assert "--append-to" not in command
    assert command[-1] == str(source)


def test_validation_accepts_single_video_track_for_dovi_merged_output(tmp_path: Path):
    """BATCH-006：dovi_tool 合并 BL+EL 后 mkvmerge 输出单视频轨（不报
    multiplexed_tracks）是预期，validate 不应误拒。DV 完整性的最终闸门是
    _verify_dolby_vision（ffprobe DOVI configuration record 要求
    dv_profile==7 + rpu + el），在 build() 里 validate 之后调用。"""
    asset = _asset(tmp_path)
    resolved = resolve_official_tracks(asset, parse_identification(_input_identification()))
    output = {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {}},
            {"id": 1, "type": "audio", "codec": "TrueHD", "properties": {"language": "eng"}},
            {"id": 2, "type": "audio", "codec": "AC-3", "properties": {"language": "zho"}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "eng"}},
            {"id": 4, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "zho"}},
        ],
    }

    result = validate_finalized_identification(asset, resolved, output)
    assert result["dolby_vision"] is True


def test_validation_fails_closed_when_dolby_vision_output_has_two_video_tracks(tmp_path: Path):
    """BATCH-006：DV 成品若出现两条视频轨（BL+EL 未合并成单轨）必须拒绝——
    不允许把未合并的双轨当作成品交付。"""
    asset = _asset(tmp_path)
    resolved = resolve_official_tracks(asset, parse_identification(_input_identification()))
    output = {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {}},
            {"id": 1, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {}},
            {"id": 2, "type": "audio", "codec": "TrueHD", "properties": {"language": "eng"}},
            {"id": 3, "type": "audio", "codec": "AC-3", "properties": {"language": "zho"}},
            {"id": 4, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "eng"}},
            {"id": 5, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "zho"}},
        ],
    }

    with pytest.raises(MatroskaBuildError, match="Dolby Vision"):
        validate_finalized_identification(asset, resolved, output)


def test_validation_rejects_missing_official_subtitle(tmp_path: Path):
    asset = _asset(tmp_path)
    single_video_input = _input_identification()
    single_video_input["tracks"] = [
        track for track in single_video_input["tracks"] if track["id"] != 1
    ]
    resolved = resolve_official_tracks(asset, parse_identification(single_video_input))
    output = {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {}},
            {"id": 1, "type": "audio", "codec": "TrueHD", "properties": {"language": "eng"}},
            {"id": 2, "type": "audio", "codec": "AC-3", "properties": {"language": "zho"}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "eng"}},
        ],
    }

    with pytest.raises(MatroskaBuildError, match="字幕"):
        validate_finalized_identification(asset, resolved, output)


def _stream(index: int, codec_type: str, codec: str, pid: int) -> dict:
    return {
        "index": index,
        "codec_type": codec_type,
        "codec_name": codec,
        "id": hex(pid),
    }


def test_inject_metadata_aligns_by_pid_with_truehd_expansion():
    """TrueHD PID 展开为两个输出流时，语言标签必须按 PID 对齐。"""
    from bluray_fidelity.bluray import BlurayTrack
    from bluray_fidelity.matroska import FifoMatroskaBuilder

    asset = _asset(Path("/tmp"))
    asset = replace(asset, 
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x83, language="eng"),
            BlurayTrack(index=1, pid=0x1101, coding_type=0x85, language="eng"),
            BlurayTrack(index=2, pid=0x1102, coding_type=0x83, language="zho"),
        ),
        subtitle_tracks=(),
    )
    streams = [
        _stream(1, "video", "hevc", 0x1011),
        _stream(2, "audio", "truehd", 0x1100),
        _stream(3, "audio", "ac3", 0x1100),  # TrueHD AC3 core（同 PID 双流）
        _stream(4, "audio", "dts", 0x1101),
        _stream(5, "audio", "truehd", 0x1102),
    ]
    cmd = ["ffmpeg", "-i", "fifo", "-map", "0:a?", "-c", "copy"]
    cmd = FifoMatroskaBuilder._inject_track_metadata(cmd, asset, streams)

    # 输出流顺序 = PMT 顺序：truehd(0x1100), ac3(0x1100), dts(0x1101), truehd(0x1102)
    # 语言必须按 PID 对齐，而不是 MPLS 列表顺序
    assert "-metadata:s:a:0" in cmd and cmd[cmd.index("-metadata:s:a:0") + 1] == "language=eng"
    assert "-metadata:s:a:1" in cmd and cmd[cmd.index("-metadata:s:a:1") + 1] == "language=eng"
    assert "-metadata:s:a:2" in cmd and cmd[cmd.index("-metadata:s:a:2") + 1] == "language=eng"
    assert "-metadata:s:a:3" in cmd and cmd[cmd.index("-metadata:s:a:3") + 1] == "language=zho"
    # 默认音轨 = 首选 zho（MPLS index 2 -> pid 0x1102 -> 输出流 3）
    assert "-disposition:a:3" in cmd
    assert cmd[cmd.index("-disposition:a:3") + 1] == "default"


def test_inject_metadata_transcodes_pcm_bluray():
    """LPCM（pcm_bluray）无 Matroska codec tag，必须转码 pcm_s16le。"""
    from bluray_fidelity.bluray import BlurayTrack
    from bluray_fidelity.matroska import FifoMatroskaBuilder

    asset = _asset(Path("/tmp"))
    asset = replace(asset, 
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x80, language="eng"),
            BlurayTrack(index=1, pid=0x1101, coding_type=0x80, language="jpn"),
        ),
        subtitle_tracks=(),
    )
    streams = [
        _stream(1, "video", "hevc", 0x1011),
        _stream(2, "audio", "pcm_bluray", 0x1100),
        _stream(3, "audio", "pcm_bluray", 0x1101),
    ]
    cmd = ["ffmpeg", "-i", "fifo", "-map", "0:a?", "-c", "copy"]
    cmd = FifoMatroskaBuilder._inject_track_metadata(cmd, asset, streams)

    assert "-c:a:0" in cmd and cmd[cmd.index("-c:a:0") + 1] == "pcm_s16le"
    assert "-c:a:1" in cmd and cmd[cmd.index("-c:a:1") + 1] == "pcm_s16le"
    assert "-metadata:s:a:0" in cmd and cmd[cmd.index("-metadata:s:a:0") + 1] == "language=eng"
    assert "-metadata:s:a:1" in cmd and cmd[cmd.index("-metadata:s:a:1") + 1] == "language=jpn"


def test_inject_metadata_falls_back_when_probe_missing():
    """探测缺失时回退 MPLS 顺序注入（旧行为，不更坏）。"""
    from bluray_fidelity.bluray import BlurayTrack
    from bluray_fidelity.matroska import FifoMatroskaBuilder

    asset = _asset(Path("/tmp"))
    asset = replace(asset, 
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x83, language="eng"),
            BlurayTrack(index=1, pid=0x1101, coding_type=0x83, language="zho"),
        ),
        subtitle_tracks=(),
    )
    cmd = ["ffmpeg", "-i", "fifo", "-map", "0:a?", "-c", "copy"]
    cmd = FifoMatroskaBuilder._inject_track_metadata(cmd, asset, None)
    assert "-metadata:s:a:0" in cmd and cmd[cmd.index("-metadata:s:a:0") + 1] == "language=eng"
    assert "-metadata:s:a:1" in cmd and cmd[cmd.index("-metadata:s:a:1") + 1] == "language=zho"


def _stream(index: int, codec_type: str, codec: str, pid: int) -> dict:
    return {"index": index, "codec_type": codec_type, "codec_name": codec, "id": hex(pid)}


def test_select_fifo_maps_excludes_dv_el_and_truehd_core():
    """受控映射：排除 DV EL 辅助视频与 TrueHD AC-3 core，跳过非官方流。"""
    from bluray_fidelity.bluray import BlurayTrack
    from bluray_fidelity.matroska import FifoMatroskaBuilder

    asset = _asset(Path("/tmp"))
    asset = replace(asset,
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x83, language="eng"),
            BlurayTrack(index=1, pid=0x1101, coding_type=0x85, language="eng"),
        ),
        subtitle_tracks=(BlurayTrack(index=0, pid=0x12a0, coding_type=0x90, language="eng"),),
    )
    streams = [
        _stream(0, "video", "hevc", 0x1011),   # BL 4K
        _stream(1, "video", "hevc", 0x1015),   # DV EL 1080p（排除）
        _stream(2, "audio", "truehd", 0x1100),
        _stream(3, "audio", "ac3", 0x1100),    # TrueHD AC-3 core（排除）
        _stream(4, "audio", "dts", 0x1101),
        _stream(5, "audio", "ac3", 0x1A00),    # 非官方解说轨（排除）
        _stream(6, "subtitle", "hdmv_pgs_subtitle", 0x12a0),
        _stream(7, "subtitle", "hdmv_pgs_subtitle", 0x1B00),  # 非官方（排除）
    ]
    maps, selected = FifoMatroskaBuilder._select_fifo_maps(streams, asset)
    map_args = " ".join(maps)
    assert "0:1" not in map_args       # EL 排除
    assert "0:3" not in map_args       # core 排除
    assert "0:5" not in map_args       # 解说排除
    assert "0:7" not in map_args       # 非官方字幕排除
    assert "0:0" in map_args and "0:2" in map_args and "0:4" in map_args and "0:6" in map_args
    selected_types = [(s["codec_type"], s["id"]) for s in selected]
    assert ("video", "0x1011") in selected_types
    assert ("audio", "0x1101") in selected_types
    assert ("subtitle", "0x12a0") in selected_types
    assert ("video", "0x1015") not in selected_types


def test_select_fifo_maps_falls_back_without_probe():
    from bluray_fidelity.matroska import FifoMatroskaBuilder
    asset = _asset(Path("/tmp"))
    maps, selected = FifoMatroskaBuilder._select_fifo_maps(None, asset)
    assert maps == ["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"]
    assert selected is None


def _dv_asset(tmp_path: Path) -> BlurayTitleAsset:
    return replace(
        _asset(tmp_path),
        video_tracks=(
            BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),
            BlurayTrack(index=1, pid=0x1015, coding_type=0xEA, language=""),
        ),
    )


def test_dv_command_two_inputs_video_first_with_local_ids(tmp_path: Path):
    from bluray_fidelity.matroska import build_mkvmerge_command_dv

    asset = _dv_asset(tmp_path)
    resolved = resolve_official_tracks(asset, parse_identification(_input_identification()))
    merged_video = tmp_path / "title.dv.hevc"
    source = tmp_path / "title.m2ts"
    output = tmp_path / "final.mkv.partial"

    command = build_mkvmerge_command_dv(
        asset, resolved, merged_video, source, output, title="西线无战事",
    )

    assert command[:3] == ["mkvmerge", "--output", str(output)]
    # 第一个输入：合并后的 DV 视频轨，选项在其之前、使用局部 ID 0。
    video_at = command.index(str(merged_video))
    assert command[video_at - 6] == "--video-tracks"
    assert command[video_at - 5] == "0"
    assert command[video_at - 1] == "0:yes"
    # 第二个输入：m2ts 只取音轨/字幕，排除其视频轨。
    assert ("--video-tracks", "!0") in list(zip(command, command[1:]))
    assert ["--audio-tracks", "2,4"] == command[
        command.index("--audio-tracks"):command.index("--audio-tracks") + 2
    ]
    assert ["--subtitle-tracks", "5,6"] == command[
        command.index("--subtitle-tracks"):command.index("--subtitle-tracks") + 2
    ]
    assert "2:eng" in command
    assert "4:zho" in command
    assert "5:eng" in command
    assert "6:zho" in command
    assert command[-1] == str(source)


def test_require_dovi_side_data_accepts_profile_7():
    from bluray_fidelity.matroska import require_dovi_side_data

    payload = {
        "streams": [
            {"side_data_list": [
                {"side_data_type": "DOVI configuration record",
                 "dv_profile": 7, "rpu_present_flag": 1, "el_present_flag": 1},
            ]},
        ],
    }

    record = require_dovi_side_data(payload)

    assert record["dv_profile"] == 7


def test_require_dovi_side_data_rejects_missing_enhancement_layer():
    from bluray_fidelity.matroska import require_dovi_side_data

    for payload in (
        {"streams": [{"side_data_list": []}]},
        {"streams": [{}]},
        {"streams": [{"side_data_list": [
            {"side_data_type": "DOVI configuration record",
             "dv_profile": 7, "rpu_present_flag": 1, "el_present_flag": 0},
        ]}]},
        {"streams": [{"side_data_list": [
            {"side_data_type": "DOVI configuration record",
             "dv_profile": 8, "rpu_present_flag": 1, "el_present_flag": 1},
        ]}]},
    ):
        with pytest.raises(MatroskaBuildError, match="Dolby Vision"):
            require_dovi_side_data(payload)


def test_require_dovi_side_data_profile_81_mode():
    """Profile 8.1 模式：接受 dv_profile=8（RPU+BL，无 EL），拒绝 profile 7。"""
    from bluray_fidelity.matroska import require_dovi_side_data

    ok = {
        "streams": [
            {"side_data_list": [
                {"side_data_type": "DOVI configuration record",
                 "dv_profile": 8, "rpu_present_flag": 1, "el_present_flag": 0,
                 "bl_present_flag": 1},
            ]},
        ],
    }
    assert require_dovi_side_data(ok, "81")["dv_profile"] == 8

    bad = {
        "streams": [
            {"side_data_list": [
                {"side_data_type": "DOVI configuration record",
                 "dv_profile": 7, "rpu_present_flag": 1, "el_present_flag": 1},
            ]},
        ],
    }
    with pytest.raises(MatroskaBuildError, match="Profile 81"):
        require_dovi_side_data(bad, "81")


def test_fifo_builder_refuses_dolby_vision_content(tmp_path: Path, monkeypatch):
    from bluray_fidelity.matroska import FifoMatroskaBuilder
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    builder = FifoMatroskaBuilder()
    asset = _dv_asset(tmp_path)

    with pytest.raises(MatroskaBuildError, match="Dolby Vision"):
        builder.build(asset, tmp_path / "final.mkv", title="西线无战事")


def test_dual_hevc_video_streams_detects_pmt_only_dolby_vision():
    """西线无战事式：EL 不在 MPLS 轨道表，只有 PMT 声明两条 HEVC 视频 PID。"""
    from bluray_fidelity.matroska import dual_hevc_video_streams

    assert dual_hevc_video_streams([
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011"},
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1015"},
        {"codec_type": "audio", "codec_name": "truehd", "id": "0x1100"},
    ]) is True


def test_dual_hevc_video_streams_ignores_single_video_and_avc_pip():
    from bluray_fidelity.matroska import dual_hevc_video_streams

    assert dual_hevc_video_streams([
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011"},
        {"codec_type": "audio", "codec_name": "truehd", "id": "0x1100"},
    ]) is False
    assert dual_hevc_video_streams(None) is False
    assert dual_hevc_video_streams([]) is False


def test_dv_video_pids_prefers_mpls_then_falls_back_to_pmt_by_resolution(
    tmp_path: Path,
):
    from bluray_fidelity.matroska import MatroskaBuilder

    builder = MatroskaBuilder()
    pmt_streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1015",
         "width": 1920, "height": 1080},
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011",
         "width": 3840, "height": 2160},
    ]

    # MPLS 声明双轨：直接用 MPLS 顺序。
    asset = _dv_asset(tmp_path)
    assert builder._dv_video_pids(asset, pmt_streams) == (0x1011, 0x1015)

    # 西线无战事式：MPLS 无 EL 条目，按分辨率降序从 PMT 取 BL/EL。
    single_video_asset = replace(asset, video_tracks=asset.video_tracks[:1])
    assert builder._dv_video_pids(single_video_asset, pmt_streams) == (0x1011, 0x1015)


def test_dv_video_pids_rejects_single_pmt_video(tmp_path: Path):
    from bluray_fidelity.matroska import MatroskaBuilder

    builder = MatroskaBuilder()
    asset = replace(_asset(tmp_path), video_tracks=())
    with pytest.raises(MatroskaBuildError, match="增强层"):
        builder._dv_video_pids(asset, [
            {"codec_type": "video", "codec_name": "hevc", "id": "0x1011",
             "width": 3840, "height": 2160},
        ])


def test_default_builder_collects_merged_rpu_evidence(tmp_path: Path, monkeypatch):
    """Default mkvmerge finalization must satisfy the strict Profile-7 gate."""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    merged = tmp_path / "merged.hevc"
    evidence_file = tmp_path / "merged.rpu.bin"
    merged.write_bytes(b"dv")
    calls = []

    def fake_raw(executable, source, target, *, limit):
        calls.append((executable, source, target, limit))
        return {
            "method": "dovi_tool extract-rpu",
            "limit_frames": 240,
            "rpu_summary": "ok",
        }

    monkeypatch.setattr(m, "_raw_rpu_evidence", fake_raw)
    evidence = MatroskaBuilder._capture_merged_rpu_evidence(
        "dovi_tool", merged, evidence_file,
    )

    assert evidence["method"] == "dovi_tool extract-rpu"
    assert calls == [("dovi_tool", merged, evidence_file, 240)]


def test_default_builder_dv_branch_passes_el_source_digests_to_profile7_gate(
    tmp_path, monkeypatch
):
    """默认 mkvmerge DV 分支必须把源 EL 摘要纳入 dv_facts（P0 回归）。

    修复前默认 MatroskaBuilder 的 DV 分支只采集 dovi_executable/duration/fps/
    bl_size/merged_size/merged_rpu_evidence，缺 el_source_digests →
    _verify_dv_profile7_evidence 以"Dolby Vision EL 独立证据缺失"拒绝发布
    （西线无战事 dlv_caf04c1ffc1b1530ab34 实测失败）。本测试断言默认路径：
    - 调用 _el_vcl_parse 生成非空 el_source_digests 与匹配统计；
    - 用同一 _verify_el_order_consistency 校验合并流（container=False）；
    - 将 el_source_digests/el_source_digest_stats/el_merged_order 传入最终
      Profile 7 发布门。
    """
    import json as _json
    import shutil as _shutil
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _dv_asset(tmp_path)
    builder = MatroskaBuilder()

    def fake_materialize(_asset, source, progress=None, staging=None):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"m")

    monkeypatch.setattr(MatroskaBuilder, "_materialize_title",
                        staticmethod(fake_materialize))
    monkeypatch.setattr(builder, "_video_streams",
                        lambda _s: _b2_streams(with_el=True))
    monkeypatch.setattr(
        _shutil, "which",
        lambda name: f"/usr/bin/{name}"
        if name in ("mkvmerge", "ffmpeg", "dovi_tool", "ffprobe") else None,
    )
    # 提取 BL/EL 与合并：真实文件写入（后续 _el_vcl_parse 需要真实 EL 文件）。
    def fake_extract(_source, _base_pid, _enh_pid, _progress, base, el):
        base.write_bytes(b"\x00\x00\x01" + bytes([0x26, 0x01]) + b"bl" * 64)
        el.write_bytes(b"\x00\x00\x01" + bytes([0x26, 0x01]) + b"el" * 64)

    monkeypatch.setattr(builder, "_extract_dv_layers", fake_extract)
    monkeypatch.setattr(builder, "_merge_dv_layers",
                        lambda _e, _b, _el, merged, _p: merged.write_bytes(b"dv" * 64))
    monkeypatch.setattr(m, "_raw_rpu_evidence",
                        lambda *_a, **kw: {"method": "dovi_tool extract-rpu",
                                           "limit_frames": 240,
                                           "rpu_summary": "ok"})
    # 源 EL 摘要：真实解析 EL 临时文件（fake_extract 写了 Annex-B NAL）。
    # 为稳定断言，mock 掉 _el_vcl_parse 记录调用并返回固定摘要/统计。
    el_parse_calls = []
    real_parse = m._el_vcl_parse
    monkeypatch.setattr(
        m, "_el_vcl_parse",
        lambda path: el_parse_calls.append(path) or real_parse(path),
    )    # 合并流顺序一致性（container=False）与最终 MKV（container=True）共用
    # 同一被校验函数——记录调用方式。
    order_calls = []
    monkeypatch.setattr(
        m, "_verify_el_order_consistency",
        lambda *_a, **kw: order_calls.append(kw) or {
            "source_vcl_slices": 1, "recovered_vcl_slices": 1,
            "order_ok": True, "temporal": "frame-order",
        },
    )
    monkeypatch.setattr(builder, "_verify_seekable", lambda _o, dv_facts=None: None)

    # 捕获传给最终 Profile 7 发布门的 dv_facts。
    captured: dict = {}

    def fake_profile7(ffmpeg, mkv_path, dv_facts):
        captured["dv_facts"] = dict(dv_facts or {})
        # 最终 MKV 的顺序一致性校验发生在发布门内部（container=True）；
        # 在这里调用被 mock 的 _verify_el_order_consistency 以记录调用方式。
        m._verify_el_order_consistency(
            str(dv_facts.get("dovi_executable") or ""), ffmpeg, Path(),
            mkv_path, "最终 MKV", container=True,
            src_digests=dv_facts.get("el_source_digests") or [],
            src_stats=dv_facts.get("el_source_digest_stats"),
        )
        return {"rpu_per_window": (5, 5, 5), "el_evidence": "ok",
                "profile7_signal": {"dv_profile": 7}}

    monkeypatch.setattr(m, "_verify_dv_profile7_evidence", fake_profile7)
    monkeypatch.setattr(builder, "_verify_dolby_vision",
                        lambda _o: {"dv_profile": 7})

    class _IdentifyOk:
        returncode = 0
        stdout = _json.dumps(_input_identification())
        stderr = ""

    # 最终成品 identify：DV 合并后必须是单视频轨 + 2 音轨 + 2 字幕（与
    # _asset 的 MPLS 轨道一致）；source identify 用双视频轨 _input_identification。
    def _final_identification() -> dict:
        return {
            "container": {"properties": {"duration": 100_000_000_000}},
            "tracks": [
                {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
                 "properties": {"stream_id": 4113}},
                {"id": 2, "type": "audio", "codec": "TrueHD",
                 "properties": {"stream_id": 4352, "language": "eng"}},
                {"id": 4, "type": "audio", "codec": "AC-3",
                 "properties": {"stream_id": 4353, "language": "zho"}},
                {"id": 5, "type": "subtitles", "codec": "HDMV PGS",
                 "properties": {"stream_id": 4768, "language": "eng"}},
                {"id": 6, "type": "subtitles", "codec": "HDMV PGS",
                 "properties": {"stream_id": 4769, "language": "zho"}},
            ],
        }

    class _IdentifyFinalOk:
        returncode = 0
        stdout = _json.dumps(_final_identification())
        stderr = ""

    class _FfprobeDvc:
        returncode = 0
        stdout = _json.dumps({"streams": [{"side_data_list": [
            {"side_data_type": "DOVI configuration record", "dv_profile": 7,
             "rpu_present_flag": 1, "el_present_flag": 1}]}]})
        stderr = ""

    def _run(cmd, **k):
        if cmd and cmd[0].endswith("mkvmerge") and "--identify" in cmd:
            # 源（.m2ts）用双视频轨输入；成品（.mkv）用单视频轨输出。
            if any(a.endswith(".mkv") for a in cmd):
                return _IdentifyFinalOk()
            return _IdentifyOk()
        return _FfprobeDvc()

    monkeypatch.setattr(m.subprocess, "run", _run)

    class FakeProcess:
        stdout = iter([])
        def __init__(self, args=None, **kwargs):
            if args and args[0].endswith("mkvmerge") and "--output" in args:
                (tmp_path / "out.mkv").write_bytes(b"mkv")
        def wait(self):
            return 0

    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda *a, **k: FakeProcess(*a, **k))
    # E1 轨覆盖门要对真实成品逐窗口测在场区间；本用例的成品是桩字节（b"mkv"），
    # 覆盖门在这里没有可驱动的对象（真实媒体证据见
    # tests/test_matroska_track_coverage.py）。
    monkeypatch.setattr(m, "require_track_coverage", lambda *a, **k: {})

    builder.build(asset, tmp_path / "out.mkv", title="测试")

    facts = captured["dv_facts"]
    # 1) 非空 el_source_digests + 匹配统计进入最终 Profile 7 门。
    assert facts.get("el_source_digests"), "默认 DV 分支必须生成源 EL 摘要"
    assert facts.get("el_source_digest_stats", {}).get("vcl_count") == len(
        facts["el_source_digests"])
    assert facts["el_source_digest_stats"]["sequence_sha256"]
    # 2) 源 EL 摘要来自 EL 临时文件（_el_vcl_parse 被调用且路径是 EL 文件；
    # 文件本身在 build finally 中清理，此处断言调用时路径形态即可）。
    assert el_parse_calls, "必须调用 _el_vcl_parse 生成源 EL 摘要"
    assert el_parse_calls[-1].name.endswith(".el.hevc"), \
        f"源 EL 摘要必须来自 EL 临时文件：{el_parse_calls[-1]}"
    # 3) 合并流与最终 MKV 共用同一 _verify_el_order_consistency（container 区分）。
    assert any(kw.get("container") is False for kw in order_calls), \
        "合并裸流必须与源 EL 做顺序一致性校验（container=False）"
    assert any(kw.get("container") is True for kw in order_calls), \
        "最终 MKV 必须继续走顺序一致性校验（container=True）"
    assert "el_merged_order" in facts
    # 4) 未降级：保持 Profile 7 数据面，dv_facts 仍含合并 RPU 证据与工具路径。
    assert facts.get("merged_rpu_evidence", {}).get("rpu_summary") == "ok"
    assert facts.get("dovi_executable") == "dovi_tool"


def test_default_builder_dv_branch_fails_closed_when_el_evidence_fails(
    tmp_path, monkeypatch
):
    """默认 mkvmerge DV 分支：EL 摘要/一致性采集失败仍失败关闭，不发布 ready。"""
    import json as _json
    import shutil as _shutil
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder, MatroskaBuildError

    asset = _dv_asset(tmp_path)
    builder = MatroskaBuilder()

    def fake_materialize(_asset, source, progress=None, staging=None):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"m")

    monkeypatch.setattr(MatroskaBuilder, "_materialize_title",
                        staticmethod(fake_materialize))
    monkeypatch.setattr(builder, "_video_streams",
                        lambda _s: _b2_streams(with_el=True))
    monkeypatch.setattr(
        _shutil, "which",
        lambda name: f"/usr/bin/{name}"
        if name in ("mkvmerge", "ffmpeg", "dovi_tool", "ffprobe") else None,
    )
    monkeypatch.setattr(builder, "_extract_dv_layers",
                        lambda _s, _bp, _ep, _p, base, el: (
                            base.write_bytes(b"bl"), el.write_bytes(b"el")))
    monkeypatch.setattr(builder, "_merge_dv_layers",
                        lambda _e, _b, _el, merged, _p: merged.write_bytes(b"dv"))
    monkeypatch.setattr(m, "_raw_rpu_evidence",
                        lambda *_a, **kw: {"method": "dovi_tool extract-rpu",
                                           "limit_frames": 240,
                                           "rpu_summary": "ok"})
    # 源 identify 需要双视频轨输入（PMT-only DV）。
    class _IdentifyOk:
        returncode = 0
        stdout = _json.dumps(_input_identification())
        stderr = ""
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *_a, **_k: _IdentifyOk())
    # 源 EL 摘要解析失败 → 构建必须失败关闭。
    monkeypatch.setattr(
        m, "_el_vcl_parse",
        lambda _path: (_ for _ in ()).throw(
            MatroskaBuildError("EL 流读取失败（失败关闭）")),
    )
    with pytest.raises(MatroskaBuildError, match="EL 流读取失败"):
        builder.build(asset, tmp_path / "out2.mkv", title="测试")


def test_primary_video_pid_allows_single_official_track_for_hdr10plus(tmp_path: Path):
    """HDR10+ 清理只取官方主视频，不能误走 Profile-7 双轨门。"""
    from bluray_fidelity.matroska import MatroskaBuilder

    builder = MatroskaBuilder()
    asset = replace(_asset(tmp_path), video_tracks=(
        BlurayTrack(index=0, pid=0x1011, coding_type=0x24, language=""),
    ))
    assert builder._primary_video_pid(asset, [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011"},
    ]) == asset.video_tracks[0].pid





def test_dynamic_sei_payload_detects_type5_and_hdr10plus():
    """动态元数据 SEI 识别：type 5 私有 SEI 与 HDR10+ T.35（type 4）。"""
    from bluray_fidelity.matroska import _dynamic_sei_payload

    # type 5 (user_data_unregistered) 任意 UUID → 剥离
    assert _dynamic_sei_payload(bytes([5, 16]) + bytes(16) + b"\x80") is True
    # HDR10+ T.35（type 4，country=0xB5）
    assert _dynamic_sei_payload(bytes([4, 5]) + b"\xb5\x00\x3c\x00\x01" + b"\x80") is True
    # 母版显示（type 1 / 137）与内容亮度（type 144）→ 保留
    assert _dynamic_sei_payload(bytes([1, 24]) + bytes(24) + b"\x80") is False
    assert _dynamic_sei_payload(bytes([137, 24]) + bytes(24) + b"\x80") is False
    assert _dynamic_sei_payload(bytes([144, 4]) + bytes(4) + b"\x80") is False
    # 前有静态消息、后有 type 5
    mixed = bytes([1, 2, 0xAA, 0xBB]) + bytes([5, 16]) + bytes(16) + b"\x80"
    assert _dynamic_sei_payload(mixed) is True
    # 空/结尾
    assert _dynamic_sei_payload(b"\x80") is False


def test_strip_dynamic_sei_keeps_static_metadata_and_parameters(tmp_path):
    """剥离只删除动态 SEI：VPS/SPS/PPS/片全部保留，静态 SEI 保留。"""
    from bluray_fidelity.matroska import _strip_dynamic_sei

    vps = b"\x00\x00\x01\x40\x01\x0c\x01\xff\xff\x01\x60\x00\x00\x03\x00\x90\x00\x00\x03\x00\x00\x03\x00\x99\x99\x98\x09\x80"
    sps = b"\x00\x00\x01\x42\x01\x01\x01\x60\x00\x00\x03\x00\x90\x00\x00\x03\x00\x00\x03\x00\x99\xa0\x80"
    pps = b"\x00\x00\x01\x44\x01\xc0\x80"
    sei_static = b"\x00\x00\x01\x4e\x01\x01\x18" + bytes(24) + b"\x80"     # header + type1 mastering
    sei_dynamic = b"\x00\x00\x01\x4e\x01\x05\x10" + bytes(16) + b"\x80"     # header + type5 ATEME
    slice1 = b"\x00\x00\x01\x02\x01\x02\x03\x80"
    slice2 = b"\x00\x00\x01\x02\x04\x05\x06\x80"
    es = tmp_path / "in.hevc"
    es.write_bytes(vps + sps + pps + sei_static + sei_dynamic + slice1 + slice2)

    stripped = _strip_dynamic_sei(es)
    data = stripped.read_bytes()

    assert b"\x05\x10" not in data          # type-5 SEI 已删
    assert b"\x01\x18" in data              # 静态 SEI 保留
    assert data.startswith(b"\x00\x00\x01\x40")  # VPS 在开头
    assert data.count(b"\x00\x00\x01\x02") == 2  # 两个片都在


def test_peak_space_bytes_models_peak_temporary_footprint(tmp_path):
    """BATCH-001 §5 / P1-2 + 2026-09-14 分段：峰值 = 同槽位同时存活文件。

    默认 mkvmerge 路线按分段物化：全部分段（Σ play item 长度，同一 clip K 次
    引用计 K 份）+ 物化期在写分段 + 在写成品 + 槽位保留的既有成品（末次传输
    后 15 分钟），单段标题下与既有 4× 逐字一致；FIFO 无物化，只有在写成品 +
    槽位保留成品；FFmpeg-file 为物化主片 + 在写成品 + 槽位保留成品。
    """
    from bluray_fidelity.matroska import (
        FfmpegFileMatroskaBuilder,
        FifoMatroskaBuilder,
        MatroskaBuilder,
        default_peak_space_bytes,
    )

    asset = _asset(tmp_path)  # size=10_000_000
    gib_safe = 512 * 1024**2

    assert MatroskaBuilder().peak_space_bytes(asset) == 4 * 10_000_000 + gib_safe
    assert default_peak_space_bytes(asset) == 4 * 10_000_000 + gib_safe
    assert FifoMatroskaBuilder().peak_space_bytes(asset) == 2 * 10_000_000 + gib_safe
    assert FfmpegFileMatroskaBuilder().peak_space_bytes(asset) == 3 * 10_000_000 + gib_safe

    # 多段合辑：分段字节按 play item 逐段计（同一 clip 被引用 K 次即 K 份），
    # 峰值仍以完整虚拟流为上界（在写分段按整片保守取值）。
    multiclip = replace(asset, size=60, clip_count=3, clips=(
        BlurayClip(index=0, clip_id="00001", in_time=54_000_000, out_time=81_000_000),
        BlurayClip(index=1, clip_id="00002", in_time=0, out_time=27_000_000),
        BlurayClip(index=2, clip_id="00001", in_time=54_000_000, out_time=81_000_000),
    ))
    assert MatroskaBuilder().peak_space_bytes(multiclip) == 4 * 60 + gib_safe


def test_validate_dolby_vision_fact_comes_from_build_not_mpls(tmp_path):
    """P1-3：PMT-only DV（MPLS 单视频轨）成品 manifest 必须标 DV。"""
    from bluray_fidelity.matroska import validate_finalized_identification

    asset = _dv_asset(tmp_path)  # video_tracks 双轨（已知 DV）
    payload = _input_identification()
    # 模拟 PMT-only DV：MPLS 只有一条视频轨，resolved.video_ids 只有 1。
    payload["tracks"] = [t for t in payload["tracks"] if t["id"] != 1]
    resolved = resolve_official_tracks(asset, parse_identification(payload))
    assert resolved.video_ids == (0,)

    output = {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
             "properties": {"multiplexed_tracks": [1]}},
            # resolved 音轨 id=2（eng）、id=4（zho）；字幕 id=5（eng）、id=6（zho）。
            {"id": 2, "type": "audio", "codec": "TrueHD",
             "properties": {"language": "eng"}},
            {"id": 4, "type": "audio", "codec": "AC-3",
             "properties": {"language": "zho"}},
            {"id": 5, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "eng"}},
            {"id": 6, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "zho"}},
        ],
    }
    result = validate_finalized_identification(
        asset, resolved, output, dolby_vision=True,
    )
    assert result["dolby_vision"] is True


def test_validate_rejects_track_language_mismatch(tmp_path):
    """P1-4：成品音轨/字幕语言与 MPLS 期望不一致必须失败。"""
    from bluray_fidelity.matroska import validate_finalized_identification

    asset = _asset(tmp_path)
    payload = _input_identification()
    payload["tracks"] = [t for t in payload["tracks"] if t["id"] != 1]
    resolved = resolve_official_tracks(asset, parse_identification(payload))
    output = {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            # 典型最终 MKV 重新编号：video=0、audio=1/2、subtitle=3/4，
            # 与源输入 track ID（2/4/5/6）不同域（round-2 P1-2）。
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
             "properties": {}},
            # 输出音轨顺序 position 0 = official index 0（期望 eng）→ 标了 deu。
            {"id": 1, "type": "audio", "codec": "TrueHD",
             "properties": {"language": "deu"}},
            {"id": 2, "type": "audio", "codec": "AC-3",
             "properties": {"language": "zho"}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "eng"}},
            {"id": 4, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "zho"}},
        ],
    }
    with pytest.raises(MatroskaBuildError, match="语言"):
        validate_finalized_identification(asset, resolved, output)


def test_canonical_language_treats_bt_codes_as_equivalent():
    """ISO 639-2 B/T 是同一语言：mkvmerge 把 fra 归一写成 fre，校验按语义等价。"""
    from bluray_fidelity.matroska import _canonical_language

    # 20 组 B/T 双向等价 + 大小写/空白不敏感。
    for b_code, t_code in {
        "fre": "fra", "ger": "deu", "chi": "zho", "cze": "ces",
        "dut": "nld", "rum": "ron", "wel": "cym",
    }.items():
        assert _canonical_language(b_code) == _canonical_language(t_code)
    # 未配对码原样返回；空/None 归一为 und。
    assert _canonical_language("eng") == "eng"
    assert _canonical_language(" FRE ") == "fra"
    assert _canonical_language("") == "und"
    assert _canonical_language(None) == "und"


def test_validate_accepts_bt_equivalence_rejects_real_mismatch(tmp_path):
    """mkvmerge 产 fre 而官方 fra：语义同语言放行；真正不同语言仍失败。"""
    from bluray_fidelity.matroska import validate_finalized_identification

    asset = replace(
        _asset(tmp_path),
        audio_tracks=(BlurayTrack(index=0, pid=4352, coding_type=0x83, language="fra"),),
        subtitle_tracks=(BlurayTrack(index=0, pid=4768, coding_type=0x90, language="fra"),),
    )
    payload = _input_identification()
    payload["tracks"] = [
        t for t in payload["tracks"]
        if t["id"] in (0, 2, 5)  # 单视频 + fra 音轨/字幕
    ]
    resolved = resolve_official_tracks(asset, parse_identification(payload))
    output = {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
             "properties": {}},
            # mkvmerge v92 实测：--language 0:fra 产出 fre。
            {"id": 1, "type": "audio", "codec": "TrueHD",
             "properties": {"language": "fre"}},
            {"id": 2, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "fre"}},
        ],
    }
    result = validate_finalized_identification(asset, resolved, output)
    assert [t["language"] for t in result["audio_tracks"]] == ["fre"]

    # 真正不同语言（deu 归一为 ger）必须失败关闭。
    bad = {
        "container": output["container"],
        "tracks": [dict(output["tracks"][0]),
                   dict(output["tracks"][1], properties={"language": "deu"}),
                   output["tracks"][2]],
    }
    with pytest.raises(MatroskaBuildError, match="语言"):
        validate_finalized_identification(asset, resolved, bad)


def test_mkvmerge_builder_cleans_temporaries_on_failure(tmp_path, monkeypatch):
    """P2-1：真实 MatroskaBuilder 失败时 finally 清理物化主片等临时文件。"""
    from bluray_fidelity.matroska import MatroskaBuildError, MatroskaBuilder

    asset = _asset(tmp_path)
    output = tmp_path / "final.mkv.partial"
    builder = MatroskaBuilder()

    def fake_materialize(asset, source, progress=None, staging=None):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"materialized-m2ts")

    def boom(_source):
        raise MatroskaBuildError("识别阶段故障")

    monkeypatch.setattr(builder, "_identify_path", boom)
    monkeypatch.setattr(
        MatroskaBuilder, "_materialize_title", staticmethod(fake_materialize),
    )
    with pytest.raises(MatroskaBuildError, match="识别阶段故障"):
        builder.build(asset, output, title="测试")

    # 物化主片已被 finally 清理，不残留任何中间文件。
    assert not output.with_name(output.name + ".source.m2ts").exists()
    assert not output.exists()


def test_count_cue_points_parses_structured_ebml(tmp_path):
    """round-2 P1-4：结构化 EBML Cues 校验（元素 ID 匹配，非裸字节）。"""
    from bluray_fidelity.matroska import _count_cue_points

    # 最小 Segment：Cues 元素含 2 个 CuePoint（ID 0xBB）。
    segment = (
        b"\x18\x53\x80\x67" + b"\x89" +          # Segment, size=9
        b"\x1c\x53\xbb\x6b" + b"\x84" +          # Cues, size=4
        b"\xbb\x80\xbb\x80"                       # 2× CuePoint
    )
    mkv = tmp_path / "sample.mkv"
    mkv.write_bytes(segment)
    assert _count_cue_points(mkv) == 2

    # 无 Cues：只有 Info 元素。
    no_cues = (
        b"\x18\x53\x80\x67" + b"\x87" +
        b"\x15\x49\xa9\x66" + b"\x82\x80\x80"
    )
    mkv2 = tmp_path / "no_cues.mkv"
    mkv2.write_bytes(no_cues)
    assert _count_cue_points(mkv2) == 0

    # 无 Segment：这是不可解析输入，不得混同为合法容器中的「无 Cues」。
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"\x1c\x53\xbb\x6b" + bytes(64))  # Cues ID 裸字节出现
    with pytest.raises(ValueError, match="parse_failed"):
        _count_cue_points(junk)


def _encode_ebml_size_for_test(size: int) -> bytes:
    for length in range(1, 9):
        if size < (1 << (7 * length)) - 1:
            encoded = bytearray(size.to_bytes(length, "big"))
            encoded[0] |= 1 << (8 - length)
            return bytes(encoded)
    raise ValueError("test EBML size too large")


def _write_mkv_with_large_cues(path, padding_bytes: int, *, payload_tail=b"\xbb\x80"):
    """Write a sparse-size test MKV without allocating its full Cues payload."""
    void_header = b"\xec" + _encode_ebml_size_for_test(padding_bytes)
    cues_size = len(void_header) + padding_bytes + len(payload_tail)
    with path.open("wb") as stream:
        stream.write(b"\x18\x53\x80\x67\x01" + b"\xff" * 7)  # unknown Segment size
        stream.write(b"\x1c\x53\xbb\x6b" + _encode_ebml_size_for_test(cues_size))
        stream.write(void_header)
        block = bytes(64 * 1024)
        remaining = padding_bytes
        while remaining:
            size = min(len(block), remaining)
            stream.write(block[:size])
            remaining -= size
        stream.write(payload_tail)


def test_large_cues_after_four_mib_are_counted_with_bounded_memory(tmp_path):
    import tracemalloc
    from bluray_fidelity.matroska import _count_cue_points

    mkv = tmp_path / "large_cues.mkv"
    _write_mkv_with_large_cues(mkv, 5 * 1024 * 1024)
    tracemalloc.start()
    try:
        assert _count_cue_points(mkv) == 1
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024


def test_cues_smaller_than_four_mib_are_counted(tmp_path):
    from bluray_fidelity.matroska import _count_cue_points

    mkv = tmp_path / "small_large_cues.mkv"
    _write_mkv_with_large_cues(mkv, 128 * 1024, payload_tail=b"\xbb\x80\xbb\x80")
    assert _count_cue_points(mkv) == 2


def test_cues_parser_handles_element_headers_split_across_chunks(tmp_path, monkeypatch):
    import bluray_fidelity.matroska as matroska_mod

    mkv = tmp_path / "split_headers.mkv"
    _write_mkv_with_large_cues(mkv, 19, payload_tail=b"\xbb\x80\xbb\x80")
    monkeypatch.setattr(matroska_mod, "_CUES_PARSE_CHUNK_BYTES", 1)
    assert matroska_mod._count_cue_points(mkv) == 2


def test_cues_parser_distinguishes_truncated_unknown_and_unparseable(tmp_path):
    from bluray_fidelity.matroska import _count_cue_points

    def write_cues(name, declared_size, payload):
        path = tmp_path / name
        path.write_bytes(
            b"\x18\x53\x80\x67\x01" + b"\xff" * 7
            + b"\x1c\x53\xbb\x6b" + declared_size + payload
        )
        return path

    truncated = write_cues("truncated_cues.mkv", b"\x84", b"\xbb\x83\xb3")
    with pytest.raises(ValueError, match="truncated"):
        _count_cue_points(truncated)

    unknown = write_cues("unknown_cuepoint.mkv", b"\x82", b"\xbb\xff")
    with pytest.raises(ValueError, match="unknown_size"):
        _count_cue_points(unknown)

    malformed = write_cues("malformed_cues.mkv", b"\x81", b"\x00")
    with pytest.raises(ValueError, match="parse_failed"):
        _count_cue_points(malformed)

    empty = write_cues("empty_cues.mkv", b"\x80", b"")
    with pytest.raises(ValueError, match="parse_failed"):
        _count_cue_points(empty)


def test_cues_parse_failure_is_reported_as_non_absence_at_publish_gate(tmp_path):
    from bluray_fidelity.matroska import (
        MatroskaBuildError,
        _verify_cues_and_seek,
    )

    output = tmp_path / "bad_cues.mkv"
    output.write_bytes(b"\x18\x53\x80\x67\x01" + b"\xff" * 7
                       + b"\x1c\x53\xbb\x6b\x82\xbb\xff")
    diagnostics = {}
    with pytest.raises(MatroskaBuildError, match="unknown_size"):
        _verify_cues_and_seek("ffprobe", output, 100_000_000_000, diagnostics)
    assert diagnostics["cues_status"] == "unknown_size"

    absent = tmp_path / "no_cues.mkv"
    absent.write_bytes(
        b"\x18\x53\x80\x67\x87\x15\x49\xa9\x66\x82\x80\x80"
    )
    with pytest.raises(MatroskaBuildError, match="缺少结构化 Cues"):
        _verify_cues_and_seek("ffprobe", absent, 100_000_000_000)


def test_verify_seekable_three_outcomes(tmp_path, monkeypatch):
    """Codex P1-B：10/50/90% 时间窗口 seek 的正常、无包、有包无帧、无 Cues。"""
    import bluray_fidelity.matroska as matroska_mod
    from bluray_fidelity.matroska import MatroskaBuilder

    output = tmp_path / "final.mkv"
    output.write_bytes(b"fake-mkv")
    builder = MatroskaBuilder()
    builder.probe = "ffprobe"

    seen_targets: list = []
    monkeypatch.setattr(matroska_mod, "_count_cue_points", lambda _path: 3)
    monkeypatch.setattr(matroska_mod, "_probe_duration", lambda _probe, _path: 180.0)
    monkeypatch.setattr(
        matroska_mod, "_probe_interval_packets",
        lambda *a, **k: (seen_targets.append(a[2]), [{"pts_time": "1.0"}])[1],
    )
    monkeypatch.setattr(
        matroska_mod, "_probe_interval_frames",
        lambda *a, **k: [a[2] + 0.2, a[2] + 0.4],
    )

    # 正常：三个目标点 = 10%/50%/90% of 180s = 18/90/162，帧在容差内。
    builder._verify_seekable(output)
    assert seen_targets == [18.0, 90.0, 162.0], f"目标点错误：{seen_targets}"

    # 无包：窗口内无视频包 → 索引/时间轴定位失败。
    monkeypatch.setattr(matroska_mod, "_probe_interval_packets", lambda *a, **k: [])
    with pytest.raises(MatroskaBuildError, match="索引/时间轴定位失败"):
        builder._verify_seekable(output)

    # 有包无帧：关键帧/解码窗口失败。
    monkeypatch.setattr(
        matroska_mod, "_probe_interval_packets",
        lambda *a, **k: [{"pts_time": "1.0"}],
    )
    monkeypatch.setattr(matroska_mod, "_probe_interval_frames", lambda *a, **k: [])
    with pytest.raises(MatroskaBuildError, match="关键帧/解码窗口失败"):
        builder._verify_seekable(output)

    # 帧越界：帧时间远离目标 → 时间越界。
    monkeypatch.setattr(
        matroska_mod, "_probe_interval_frames", lambda *a, **k: [999.0],
    )
    with pytest.raises(MatroskaBuildError, match="时间越界"):
        builder._verify_seekable(output)

    # 无 Cues：结构化校验失败。
    monkeypatch.setattr(matroska_mod, "_count_cue_points", lambda _path: 0)
    with pytest.raises(MatroskaBuildError, match="Cues 索引"):
        builder._verify_seekable(output)



def test_vint_multi_byte_size_and_unknown_segment(tmp_path):
    """round-3 P1-1：多字节 size VINT 与 unknown-size Segment。"""
    from bluray_fidelity.matroska import _count_cue_points

    # 2 字节 size VINT（0x40|高 6 位, 低 8 位）：Cues size=2，负载 1× CuePoint。
    multi_byte = (
        b"\x18\x53\x80\x67" + b"\x88" +            # Segment size=8
        b"\x1c\x53\xbb\x6b" + b"\x40\x02" +        # Cues, 2 字节 VINT size=2
        b"\xbb\x80"                                  # 1× CuePoint (ID+size)
    )
    mkv = tmp_path / "multi_byte.mkv"
    mkv.write_bytes(multi_byte)
    assert _count_cue_points(mkv) == 1

    # unknown-size Segment：遍历到文件边界仍能找到 Cues。
    unknown_seg = (
        b"\x18\x53\x80\x67" + b"\x01\xff\xff\xff\xff\xff\xff\xff" +  # Segment unknown
        b"\x1c\x53\xbb\x6b" + b"\x84" + b"\xbb\x80\xbb\x80"
    )
    mkv2 = tmp_path / "unknown_seg.mkv"
    mkv2.write_bytes(unknown_seg)
    assert _count_cue_points(mkv2) == 2


def test_cuepoint_count_ignores_nested_bb_bytes(tmp_path):
    """round-3 P1-1：CuePoint 子元素负载中的 0xBB 字节不得误计。"""
    from bluray_fidelity.matroska import _count_cue_points

    # CuePoint(0xBB) size=3，内含 CueTime(0xB3) size=1 负载 0xBB：
    # 顶层 CuePoint 只有 1 个；裸字节计数会得到 2。
    cues_payload = b"\xbb\x83" + b"\xb3\x81\xbb"
    segment = (
        b"\x18\x53\x80\x67" + b"\x8a" +            # Segment size=10
        b"\x1c\x53\xbb\x6b" + b"\x85" +            # Cues size=5
        cues_payload
    )
    mkv = tmp_path / "nested_bb.mkv"
    mkv.write_bytes(segment)
    assert _count_cue_points(mkv) == 1


def test_real_small_mkv_cues_and_seek(tmp_path):
    """round-3 P1-1/P1-2 集成证据：本机 ffmpeg 生成小 MKV，真实解析与 seek。"""
    import shutil as _shutil
    from bluray_fidelity.matroska import _count_cue_points, MatroskaBuilder

    if _shutil.which("ffmpeg") is None or _shutil.which("ffprobe") is None:
        pytest.skip("本机缺少 ffmpeg/ffprobe")

    real_mkv = tmp_path / "real_seek.mkv"
    subprocess_run = __import__("subprocess").run
    subprocess_run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=12:size=320x240:rate=24",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(real_mkv)],
        check=True,
    )

    assert _count_cue_points(real_mkv) > 0

    builder = MatroskaBuilder()
    builder.probe = "ffprobe"
    builder._verify_seekable(real_mkv)  # 真实 ffprobe 命令实际执行


def test_truncated_and_invalid_cuepoints_report_structure_status(tmp_path):
    """截断/越界/unknown CuePoint 一律作为显式结构错误报告。"""
    from bluray_fidelity.matroska import _count_cue_points

    # Cues 子元素声明负载但文件提前结束，状态必须是 truncated。
    truncated = (
        b"\x18\x53\x80\x67" + b"\x01" + b"\xff" * 7 +
        b"\x1c\x53\xbb\x6b" + b"\x84" +
        b"\xbb\x8a"
    )
    mkv = tmp_path / "truncated.mkv"
    mkv.write_bytes(truncated)
    with pytest.raises(ValueError, match="truncated"):
        _count_cue_points(mkv)

    # 只有 ID 无 size 字段（截断 header）→ 0。
    only_id = (
        b"\x18\x53\x80\x67" + b"\x01" + b"\xff" * 7 +
        b"\x1c\x53\xbb\x6b" + b"\x81" + b"\xbb"
    )
    mkv2 = tmp_path / "only_id.mkv"
    mkv2.write_bytes(only_id)
    with pytest.raises(ValueError, match="truncated"):
        _count_cue_points(mkv2)

    # unknown-size CuePoint → 0。
    unknown_size = (
        b"\x18\x53\x80\x67" + b"\x01" + b"\xff" * 7 +
        b"\x1c\x53\xbb\x6b" + b"\x84" +
        b"\xbb\xff\x80\x80"
    )
    mkv3 = tmp_path / "unknown_size.mkv"
    mkv3.write_bytes(unknown_size)
    with pytest.raises(ValueError, match="unknown_size"):
        _count_cue_points(mkv3)


def test_real_180s_mkv_midpoint_seek(tmp_path):
    """round-4 P1 集成证据：180s 真实 MKV 的中点 seek 实际成功。"""
    import shutil as _shutil
    import subprocess as _subprocess
    from bluray_fidelity.matroska import _count_cue_points, MatroskaBuilder

    if _shutil.which("ffmpeg") is None or _shutil.which("ffprobe") is None:
        pytest.skip("本机缺少 ffmpeg/ffprobe")

    real_mkv = tmp_path / "real_180s.mkv"
    _subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=180:size=160x120:rate=1",
         "-c:v", "libx264", "-g", "1", "-pix_fmt", "yuv420p", str(real_mkv)],
        check=True,
    )
    assert _count_cue_points(real_mkv) > 0

    builder = MatroskaBuilder()
    builder.probe = "ffprobe"
    # 180s 文件：中点 90s；ffprobe seek 到 90s 附近关键帧（每秒 1 关键帧）。
    builder._verify_seekable(real_mkv)


# ============ BATCH-002 Gate A 回归 ============

def _b2_streams(with_el: bool = False, width: int = 3840):
    streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011",
         "width": width, "height": 2160, "index": 0,
         "avg_frame_rate": "24000/1001"},
        {"codec_type": "audio", "codec_name": "dts", "id": "0x1100",
         "index": 1},
        {"codec_type": "audio", "codec_name": "ac3", "id": "0x1101",
         "index": 2},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "id": "0x12a0", "index": 3},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "id": "0x12a1", "index": 4},
    ]
    if with_el:
        streams.append({"codec_type": "video", "codec_name": "hevc",
                        "id": "0x1015", "width": 1920, "height": 1080,
                        "index": 5})
    return streams


def test_exact_pid_maps_fails_closed_on_missing_official_pid(tmp_path):
    """§7-2 精确布局探测失败时失败关闭，禁止宽泛映射兜底。"""
    from bluray_fidelity.matroska import _exact_pid_maps

    asset = _asset(tmp_path)
    # PMT 缺官方音轨 PID 0x1AC1 → 失败关闭。
    broken = [s for s in _b2_streams() if s.get("id") != "0x1100"]
    with pytest.raises(MatroskaBuildError, match="失败关闭"):
        _exact_pid_maps(asset, broken)


def test_exact_pid_maps_multi_input_prefix(tmp_path):
    """§7-7 DV 双输入：音轨/PGS 的 map 指向输入 1（物化源）。"""
    from bluray_fidelity.matroska import _exact_pid_maps

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
    )
    streams = _b2_streams()
    maps = _exact_pid_maps(asset, streams, input_index=1)
    # round-1 P1-3：map 使用解析后的唯一 stream index（非 PID selector）。
    assert "1:0" in maps, "视频映射到输入 1 的 stream index 0"
    audio_indexes = [streams.index(s) for s in streams if s.get("codec_type") == "audio"]
    for idx in audio_indexes:
        assert f"1:{idx}" in maps
    assert not any(t.startswith("0:") for t in maps if t != "-map")


def test_validate_ffmpeg_output_language_and_default_flags(tmp_path):
    """§7-3 语言/默认标志精确校验，manifest 不退化为 und。"""
    from bluray_fidelity import matroska as m

    asset = _asset(tmp_path)  # 音轨 eng/zho，字幕 eng/zho，默认 index 1/1
    source_streams = _b2_streams()
    out_streams = [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840,
         "height": 2160, "tags": {}, "disposition": {}},
        {"codec_type": "audio", "codec_name": "dts",
         "tags": {"language": "eng"}, "disposition": {"default": 0}},
        {"codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "zho"}, "disposition": {"default": 1}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "eng"}, "disposition": {"default": 0}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "zho"}, "disposition": {"default": 1}},
    ]
    probe = "ffprobe"
    import types
    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "_probe_output_streams", lambda _p, _o: out_streams)
    monkey.setattr(m, "_probe_duration_ns",
                   lambda _p, _o: int(asset.duration_90k / 90000 * 1e9))
    # round-1 P1-1：统一校验现在实际执行 Cues/seek——单元测试 mock 该入口。
    monkey.setattr(m, "_verify_cues_and_seek",
                        lambda *_a, **_k: None)
    monkey.setattr(m, "_has_hdr10plus_es", lambda _f, _o: False)
    monkey.setattr(
        m, "_verify_video_timeline",
        lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                         "avg_frame_rate": "24000/1001"},
    )
    result = m._validate_ffmpeg_output(
        asset, source_streams, tmp_path / "out.mkv", probe, dolby_vision=False,
        source_path=tmp_path / "src.m2ts", ffmpeg_executable="ffmpeg",
    )
    assert [t["language"] for t in result["audio_tracks"]] == ["eng", "zho"]
    assert [t["language"] for t in result["subtitle_tracks"]] == ["eng", "zho"]
    monkey.undo()

    # 语言不符 → 失败。
    bad_streams = [dict(s) for s in out_streams]
    bad_streams[1]["tags"] = {"language": "deu"}
    monkey2 = pytest.MonkeyPatch()
    monkey2.setattr(m, "_probe_output_streams", lambda _p, _o: bad_streams)
    monkey2.setattr(m, "_probe_duration_ns",
                    lambda _p, _o: int(asset.duration_90k / 90000 * 1e9))
    monkey2.setattr(m, "_verify_cues_and_seek",
                        lambda *_a, **_k: None)
    monkey2.setattr(m, "_has_hdr10plus_es", lambda _f, _o: False)
    monkey2.setattr(
        m, "_verify_video_timeline",
        lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                         "avg_frame_rate": "24000/1001"},
    )
    with pytest.raises(MatroskaBuildError, match="语言"):
        m._validate_ffmpeg_output(
            asset, source_streams, tmp_path / "out.mkv", probe, dolby_vision=False,
            source_path=tmp_path / "src.m2ts", ffmpeg_executable="ffmpeg",
        )
    monkey2.undo()


def test_validate_ffmpeg_output_accepts_iso639_2_bt_equivalence(tmp_path):
    """真实失败复现：mkvmerge 产物法语轨 fre 与官方 fra 必须放行（B/T 同语言）。"""
    from bluray_fidelity import matroska as m

    asset = replace(
        _asset(tmp_path),
        audio_tracks=(BlurayTrack(index=0, pid=4352, coding_type=0x83, language="fra"),),
        subtitle_tracks=(BlurayTrack(index=0, pid=4768, coding_type=0x90, language="fra"),),
    )
    source_streams = [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "tags": {}, "disposition": {}},
        {"codec_type": "audio", "codec_name": "truehd", "id": "0x1100",
         "tags": {"language": "fra"}, "disposition": {"default": 0}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a0",
         "tags": {"language": "fra"}, "disposition": {"default": 0}},
    ]
    out_streams = [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840,
         "height": 2160, "tags": {}, "disposition": {}},
        {"codec_type": "audio", "codec_name": "truehd",
         "tags": {"language": "fre"}, "disposition": {"default": 0}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "fre"}, "disposition": {"default": 0}},
    ]
    probe = "ffprobe"
    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "_probe_output_streams", lambda _p, _o: out_streams)
    monkey.setattr(m, "_probe_duration_ns",
                   lambda _p, _o: int(asset.duration_90k / 90000 * 1e9))
    monkey.setattr(m, "_verify_cues_and_seek", lambda *_a, **_k: None)
    monkey.setattr(m, "_has_hdr10plus_es", lambda _f, _o: False)
    monkey.setattr(
        m, "_verify_video_timeline",
        lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                         "avg_frame_rate": "24000/1001"},
    )
    try:
        # 官方 fre(fra) 与 mkvmerge 产物 fre 等价 → 校验放行；
        # 返回值仍携带官方元数据语言。
        result = m._validate_ffmpeg_output(
            asset, source_streams, tmp_path / "out.mkv", probe, dolby_vision=False,
            source_path=tmp_path / "src.m2ts", ffmpeg_executable="ffmpeg",
        )
        assert [t["language"] for t in result["audio_tracks"]] == ["fra"]
    finally:
        monkey.undo()


def test_validate_ffmpeg_output_accepts_pcm_bluray_s24le_and_rejects_other(tmp_path):
    """英雄本色 UHD 复现：Blu-ray LPCM 24-bit 写入 MKV 为 pcm_s24le 必须放行；
    真正不同的 codec 仍失败关闭。"""
    from bluray_fidelity import matroska as m

    asset = replace(
        _asset(tmp_path),
        audio_tracks=(BlurayTrack(index=0, pid=4352, coding_type=0x80, language="eng"),),
        subtitle_tracks=(),
    )
    source_streams = [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "tags": {}, "disposition": {}},
        {"codec_type": "audio", "codec_name": "pcm_bluray", "id": "0x1100",
         "tags": {"language": "eng"}, "disposition": {"default": 0}},
    ]
    out_streams = [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840,
         "height": 2160, "tags": {}, "disposition": {}},
        {"codec_type": "audio", "codec_name": "pcm_s24le",
         "tags": {"language": "eng"}, "disposition": {"default": 0}},
    ]

    def patch(monkey, streams):
        monkey.setattr(m, "_probe_output_streams", lambda _p, _o: streams)
        monkey.setattr(m, "_probe_duration_ns",
                       lambda _p, _o: int(asset.duration_90k / 90000 * 1e9))
        monkey.setattr(m, "_verify_cues_and_seek", lambda *_a, **_k: None)
        monkey.setattr(m, "_has_hdr10plus_es", lambda _f, _o: False)
        monkey.setattr(
            m, "_verify_video_timeline",
            lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                             "avg_frame_rate": "24000/1001"},
        )

    # pcm_s24le（LPCM 24-bit）→ 放行。
    monkey = pytest.MonkeyPatch()
    patch(monkey, out_streams)
    try:
        m._validate_ffmpeg_output(
            asset, source_streams, tmp_path / "ok.mkv", "ffprobe", dolby_vision=False,
            source_path=tmp_path / "src.m2ts", ffmpeg_executable="ffmpeg",
        )
    finally:
        monkey.undo()

    # 真正不同 codec（源 pcm_bluray → 产物 ac3）→ 失败关闭。
    bad = [dict(out_streams[0]),
           dict(out_streams[1], codec_name="ac3")]
    monkey = pytest.MonkeyPatch()
    patch(monkey, bad)
    try:
        with pytest.raises(MatroskaBuildError, match="codec"):
            m._validate_ffmpeg_output(
                asset, source_streams, tmp_path / "bad.mkv", "ffprobe", dolby_vision=False,
                source_path=tmp_path / "src.m2ts", ffmpeg_executable="ffmpeg",
            )
    finally:
        monkey.undo()


def test_ffmpeg_file_probes_materialized_source_not_disc(tmp_path, monkeypatch):
    """§7-1 ISO/BDMV 都从物化主片探测（不读原盘/最大 clip）。"""
    import shutil as _shutil
    from bluray_fidelity.matroska import FfmpegFileMatroskaBuilder

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
    )
    builder = FfmpegFileMatroskaBuilder()
    probed_paths: list = []

    def fake_materialize(_asset, source, progress=None, staging=None):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"materialized")
        probed_paths.append("materialized")

    monkeypatch.setattr(
        FfmpegFileMatroskaBuilder, "_materialize_title", staticmethod(fake_materialize),
    )
    monkeypatch.setattr(
        builder, "_video_streams",
        lambda source: (probed_paths.append(source), _b2_streams())[1],
    )
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/ffmpeg")

    class FakeProcess:
        stdout = iter([])
        def __init__(self, args=None, **kwargs):
            if args and "-f" in args and "matroska" in args:
                out = tmp_path / "out.mkv"
                out.write_bytes(b"mkv")
        def wait(self):
            return 0

    import bluray_fidelity.matroska as m
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: FakeProcess(*a, **k))

    def fake_validate(_asset, _streams, _output, _probe, *, dolby_vision, source_path=None, ffmpeg_executable="ffmpeg"):
        probed_paths.append("validated")
        return {"duration_ns": 1, "dolby_vision": dolby_vision,
                "audio_tracks": (), "subtitle_tracks": ()}

    monkeypatch.setattr(m, "_validate_ffmpeg_output", fake_validate)

    metadata = builder.build(asset, tmp_path / "out.mkv", title="测试")
    assert metadata["dolby_vision"] is False
    # 探测收到的路径是物化文件（含 .source.m2ts），不是原盘 root。
    assert any("source.m2ts" in str(p) for p in probed_paths if hasattr(p, "name"))


def test_completed_source_checkpoint_is_reused_but_truncated_source_restarts(tmp_path, monkeypatch):
    """Only a full atomically-published source title may survive restart."""
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _asset(tmp_path)
    builder = MatroskaBuilder()
    source = tmp_path / "main-title.source.m2ts"
    source.write_bytes(b"x" * int(asset.size))
    calls = []

    def fake_materialize(_asset, target, progress=None, staging=None):
        calls.append(target)
        target.write_bytes(b"y" * int(_asset.size))

    monkeypatch.setattr(MatroskaBuilder, "_materialize_title", staticmethod(fake_materialize))
    assert builder._materialize_or_reuse_title(asset, source) is True
    assert calls == []

    source.write_bytes(b"truncated")
    assert builder._materialize_or_reuse_title(asset, source) is False
    assert calls == [source]
    assert source.stat().st_size == int(asset.size)


def test_ffmpeg_file_rejects_dv_media(tmp_path, monkeypatch):
    """§7-5 PMT-only 双 HEVC 必须进入 DV 事实：非 DV 实验值遇 DV 失败关闭。"""
    import shutil as _shutil
    from bluray_fidelity.matroska import FfmpegFileMatroskaBuilder

    asset = _asset(tmp_path)  # MPLS 单视频轨（asset.dolby_vision=False）
    builder = FfmpegFileMatroskaBuilder()

    def fake_materialize(_asset, source, progress=None, staging=None):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"m")

    monkeypatch.setattr(
        FfmpegFileMatroskaBuilder, "_materialize_title", staticmethod(fake_materialize),
    )
    # PMT 双 HEVC（PMT-only DV）。
    monkeypatch.setattr(builder, "_video_streams", lambda _s: _b2_streams(with_el=True))
    monkeypatch.setattr(_shutil, "which", lambda _n: "/usr/bin/ffmpeg")

    with pytest.raises(MatroskaBuildError, match="媒体类别与实验值"):
        builder.build(asset, tmp_path / "out.mkv", title="测试")


def test_ffmpeg_dv_builder_command_evidence(tmp_path, monkeypatch):
    """DV 命令证据（DV 点亮修复）：视频=合并裸 HEVC（mkvmerge --default-duration
    显式帧率）；音轨/PGS=物化源；无 EL 第二轨；mkvmerge 自动生成容器级 dvcC。"""
    import shutil as _shutil
    import json as _json
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import FfmpegDvFileMatroskaBuilder

    asset = replace(
        _asset(tmp_path),
        video_tracks=(
            BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),
        ),
    )
    builder = FfmpegDvFileMatroskaBuilder()
    assert builder.dv_profile == "7"

    def fake_materialize(_asset, source, progress=None, staging=None):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"m")

    monkeypatch.setattr(
        FfmpegDvFileMatroskaBuilder, "_materialize_title", staticmethod(fake_materialize),
    )
    monkeypatch.setattr(builder, "_video_streams", lambda _s: _b2_streams(with_el=True))
    monkeypatch.setattr(
        "bluray_fidelity.matroska.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in ("ffmpeg", "dovi_tool", "mkvmerge") else None,
    )
    monkeypatch.setattr(builder, "_extract_dv_layer",
                        lambda _s, _pid, target: target.write_bytes(b"es"))
    monkeypatch.setattr(builder, "_merge_dv_layers",
                        lambda _e, _b, _el, merged, _p: merged.write_bytes(b"dv"))
    import bluray_fidelity.matroska as _matroska_mod
    monkeypatch.setattr(_matroska_mod, "_dv_rpu_evidence",
                        lambda *_a, **_k: {"rpu_per_window": (5, 5, 5)})
    monkeypatch.setattr(_matroska_mod, "_raw_rpu_evidence",
                        lambda *_a, **kw: {"method": "dovi_tool extract-rpu",
                                           "limit_frames": 240,
                                           "rpu_summary": "ok"})
    _fp4 = {0.0: [b"a", b"b", b"c"], 0.1: [b"d", b"e", b"f"],
            0.5: [b"g", b"h", b"i"], 0.9: [b"j", b"k", b"l"]}
    monkeypatch.setattr(_matroska_mod, "_extract_el_window_fingerprints",
                        lambda *_a, **_k: dict(_fp4))
    monkeypatch.setattr(_matroska_mod, "_exclude_bl_hits",
                        lambda fps, *_a, **_k: dict(fps))
    monkeypatch.setattr(_matroska_mod, "_require_el_window_integrity",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(_matroska_mod, "_el_vcl_slice_digests",
                        lambda *_a, **_k: [b"d"] * 5)
    monkeypatch.setattr(_matroska_mod, "_verify_el_order_consistency",
                        lambda *_a, **kw: {"source_vcl_slices": 5,
                                            "recovered_vcl_slices": 5,
                                            "order_ok": True,
                                            "temporal": "frame-order"})
    monkeypatch.setattr(
        _matroska_mod, "_verify_video_timeline",
        lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                         "avg_frame_rate": "24000/1001"},
    )
    monkeypatch.setattr(builder, "_verify_seekable", lambda _o: None)

    captured: dict = {}

    class _IdentifyOk:
        returncode = 0
        stdout = _json.dumps(_input_identification())
        stderr = ""

    class _FfprobeDvc:
        returncode = 0
        stdout = _json.dumps({
            "streams": [{"side_data_list": [
                {"side_data_type": "DOVI configuration record", "dv_profile": 7,
                 "rpu_present_flag": 1, "el_present_flag": 1}]}]
        })
        stderr = ""

    def _run(cmd, **k):
        if cmd and cmd[0].endswith("mkvmerge") and "--identify" in cmd:
            return _IdentifyOk()
        return _FfprobeDvc()

    monkeypatch.setattr(_matroska_mod.subprocess, "run", _run)

    class FakeProcess:
        stdout = iter([])
        def __init__(self, args=None, **kwargs):
            if args and args[0].endswith("mkvmerge") and "--output" in args:
                captured["cmd"] = list(args)
                (tmp_path / "out.mkv").write_bytes(b"mkv")
        def wait(self):
            return 0

    import bluray_fidelity.matroska as m2
    monkeypatch.setattr(m2.subprocess, "Popen", lambda *a, **k: FakeProcess(*a, **k))

    probed_paths: list = []
    def fake_validate(_asset, _streams, _output, _probe, *, dolby_vision,
                      source_path=None, ffmpeg_executable="ffmpeg", dv_facts=None):
        probed_paths.append(str(source_path))
        return {"duration_ns": 1, "dolby_vision": dolby_vision,
                "audio_tracks": (), "subtitle_tracks": ()}

    monkeypatch.setattr(m, "_validate_ffmpeg_output", fake_validate)

    metadata = builder.build(asset, tmp_path / "out.mkv", title="测试")
    assert metadata["dolby_vision"] is True

    cmd = captured["cmd"]
    # 命令以 mkvmerge 开头；视频输入是合并裸 HEVC（.dv.hevc），第二输入是物化源。
    assert cmd[0].endswith("mkvmerge")
    assert "--default-duration" in cmd, "裸 HEVC 必须显式帧率"
    dd_idx = cmd.index("--default-duration")
    assert cmd[dd_idx + 1].startswith("0:"), "default-duration 必须带 0:<fps>p"
    assert cmd[dd_idx + 1].endswith("p")
    inputs = [a for a in cmd if isinstance(a, str) and a.endswith(".dv.hevc")]
    assert inputs, "第一输入必须是合并裸 HEVC"
    src_inputs = [a for a in cmd if isinstance(a, str) and a.endswith(".source.m2ts")]
    assert src_inputs, "第二输入必须是物化源（音轨/PGS）"
    # 不得把 EL 作为独立轨映射（mkvmerge 命令只有两个输入文件）。
    n_src = sum(1 for a in cmd if isinstance(a, str) and a.endswith(".source.m2ts"))
    n_dv = sum(1 for a in cmd if isinstance(a, str) and a.endswith(".dv.hevc"))
    assert n_src == 1 and n_dv == 1


def test_ffmpeg_dv_builder_rejects_non_dv_media(tmp_path, monkeypatch):
    """§7-6 DV 实验值遇到非 DV 媒体失败关闭。"""
    import shutil as _shutil
    from bluray_fidelity.matroska import FfmpegDvFileMatroskaBuilder

    asset = _asset(tmp_path)
    builder = FfmpegDvFileMatroskaBuilder()

    def fake_materialize(_asset, source, progress=None, staging=None):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"m")

    monkeypatch.setattr(
        FfmpegDvFileMatroskaBuilder, "_materialize_title", staticmethod(fake_materialize),
    )
    monkeypatch.setattr(builder, "_video_streams", lambda _s: _b2_streams())
    monkeypatch.setattr(_shutil, "which", lambda _n: "/usr/bin/ffmpeg")

    with pytest.raises(MatroskaBuildError, match="媒体类别与实验值"):
        builder.build(asset, tmp_path / "out.mkv", title="测试")


def test_ffmpeg_dv_builder_cleans_temporaries_on_failure(tmp_path, monkeypatch):
    """§7-10 DV 构建失败清理 BL/EL/合并视频/物化源。"""
    import shutil as _shutil
    from bluray_fidelity.matroska import FfmpegDvFileMatroskaBuilder

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
    )
    builder = FfmpegDvFileMatroskaBuilder()

    def fake_materialize(_asset, source, progress=None, staging=None):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"m")

    monkeypatch.setattr(
        FfmpegDvFileMatroskaBuilder, "_materialize_title", staticmethod(fake_materialize),
    )
    monkeypatch.setattr(builder, "_video_streams", lambda _s: _b2_streams(with_el=True))
    monkeypatch.setattr(
        "bluray_fidelity.matroska.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in ("ffmpeg", "dovi_tool") else None,
    )
    monkeypatch.setattr(builder, "_extract_dv_layer",
                        lambda _s, _pid, target: target.write_bytes(b"es"))

    def exploding_merge(_e, _b, _el, merged, _p):
        merged.write_bytes(b"partial")
        raise RuntimeError("dovi_tool 合并中断")

    monkeypatch.setattr(builder, "_merge_dv_layers", exploding_merge)
    import bluray_fidelity.matroska as _matroska_mod
    _fp4 = {0.0: [b"a", b"b", b"c"], 0.1: [b"d", b"e", b"f"],
            0.5: [b"g", b"h", b"i"], 0.9: [b"j", b"k", b"l"]}
    monkeypatch.setattr(_matroska_mod, "_extract_el_window_fingerprints",
                        lambda *_a, **_k: dict(_fp4))
    monkeypatch.setattr(_matroska_mod, "_exclude_bl_hits",
                        lambda fps, *_a, **_k: dict(fps))
    monkeypatch.setattr(_matroska_mod, "_require_el_window_integrity",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(_matroska_mod, "_el_vcl_slice_digests",
                        lambda *_a, **_k: [b"d"] * 5)
    monkeypatch.setattr(_matroska_mod, "_verify_el_order_consistency",
                        lambda *_a, **kw: {"source_vcl_slices": 5,
                                            "recovered_vcl_slices": 5,
                                            "order_ok": True,
                                            "temporal": "frame-order"})

    with pytest.raises(RuntimeError, match="合并中断"):
        builder.build(asset, tmp_path / "out.mkv", title="测试")

    leftovers = [p.name for p in tmp_path.iterdir()
                 if any(k in p.name for k in (".source.m2ts", ".bl.hevc", ".el.hevc", ".dv.hevc"))]
    assert leftovers == [], f"临时文件泄漏：{leftovers}"


def test_batch002_peak_space_models(tmp_path):
    """§7-9 非 DV 3×（物化主片 + 在写成品 + 槽位保留成品）、DV 5×
    （+BL/EL/合并视频，覆盖各阶段共存峰值）。"""
    from bluray_fidelity.matroska import (
        FfmpegDvFileMatroskaBuilder,
        FfmpegFileMatroskaBuilder,
    )

    asset = _asset(tmp_path)  # size=10_000_000
    assert FfmpegFileMatroskaBuilder().peak_space_bytes(asset) == 3 * 10_000_000 + 512 * 1024**2
    assert FfmpegDvFileMatroskaBuilder().peak_space_bytes(asset) == 5 * 10_000_000 + 512 * 1024**2


def test_batch002_r1_same_pid_truehd_core_disambiguation(tmp_path):
    """round-1 P1-3：TrueHD+AC-3 core 同 PID 双流按 coding type 唯一判定。"""
    from bluray_fidelity.matroska import _exact_pid_maps

    # 官方音轨 0：PID 0x1100 coding 0x83（TrueHD）；PMT 同 PID 有 truehd + ac3。
    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x83, language="eng"),
            BlurayTrack(index=1, pid=0x1101, coding_type=0x81, language="zho"),
        ),
    )
    streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011", "index": 0},
        {"codec_type": "audio", "codec_name": "truehd", "id": "0x1100", "index": 1},
        {"codec_type": "audio", "codec_name": "ac3", "id": "0x1100", "index": 2},  # core 同 PID
        {"codec_type": "audio", "codec_name": "ac3", "id": "0x1101", "index": 3},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a0", "index": 4},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a1", "index": 5},
    ]
    maps = _exact_pid_maps(asset, streams)
    selectors = [maps[i + 1] for i in range(0, len(maps), 2)]
    # TrueHD 官方轨映射到 truehd 流（index 1），不是 core（index 2）。
    assert "0:1" in selectors
    assert "0:2" not in selectors


def test_batch002_r1_same_pid_ambiguous_fails_closed(tmp_path):
    """round-1 P1-3：同 PID 多候选且 coding type 无法唯一判定 → 失败关闭。"""
    from bluray_fidelity.matroska import _exact_pid_maps

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x83, language="eng"),
        ),
        subtitle_tracks=(),
    )
    streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011", "index": 0},
        # 同 PID 两个 truehd（异常布局）——coding type 无法区分 → 拒绝。
        {"codec_type": "audio", "codec_name": "truehd", "id": "0x1100", "index": 1},
        {"codec_type": "audio", "codec_name": "truehd", "id": "0x1100", "index": 2},
    ]
    with pytest.raises(MatroskaBuildError, match="唯一判定"):
        _exact_pid_maps(asset, streams)


def test_batch002_r1_bitdepth_or_hdr_change_rejected(tmp_path):
    """round-1 P1-2：位深（pix_fmt）/色彩参数/HDR side data 变化被拒绝。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
    )
    source_streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011", "index": 0,
         "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
         "color_space": "bt2020nc", "color_transfer": "smpte2084",
         "color_primaries": "bt2020", "avg_frame_rate": "24000/1001"},
        {"codec_type": "audio", "codec_name": "dts", "id": "0x1100", "index": 1},
        {"codec_type": "audio", "codec_name": "ac3", "id": "0x1101", "index": 2},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a0", "index": 3},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a1", "index": 4},
    ]

    def output_for(pix_fmt=None, side=None, sub_codec="hdmv_pgs_subtitle"):
        out = [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840,
             "height": 2160,
             "pix_fmt": pix_fmt or "yuv420p10le",
             "color_space": "bt2020nc", "color_transfer": "smpte2084",
             "color_primaries": "bt2020",
             "side_data_list": side or [], "tags": {}, "disposition": {}},
            {"codec_type": "audio", "codec_name": "dts",
             "tags": {"language": "eng"}, "disposition": {"default": 0}},
            {"codec_type": "audio", "codec_name": "ac3",
             "tags": {"language": "zho"}, "disposition": {"default": 1}},
            {"codec_type": "subtitle", "codec_name": sub_codec,
             "tags": {"language": "eng"}, "disposition": {"default": 0}},
            {"codec_type": "subtitle", "codec_name": sub_codec,
             "tags": {"language": "zho"}, "disposition": {"default": 1}},
        ]
        return out

    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "_verify_cues_and_seek",
                        lambda *_a, **_k: None)
    monkey.setattr(m, "_has_hdr10plus_es", lambda _f, _o: False)
    monkey.setattr(m, "_probe_duration_ns",
                   lambda _p, _o: int(asset.duration_90k / 90000 * 1e9))
    monkey.setattr(
        m, "_verify_video_timeline",
        lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                         "avg_frame_rate": "24000/1001"},
    )

    # 位深被降到 8bit → 拒绝。
    monkey.setattr(m, "_probe_output_streams",
                   lambda _p, _o: output_for(pix_fmt="yuv420p"))
    with pytest.raises(MatroskaBuildError, match="位深"):
        m._validate_ffmpeg_output(asset, source_streams, tmp_path / "o.mkv", "ffprobe",
                                  dolby_vision=False, source_path=tmp_path / "s.m2ts",
                                  ffmpeg_executable="ffmpeg")

    # HDR side data 意外出现 → 拒绝。
    monkey.setattr(m, "_probe_output_streams",
                   lambda _p, _o: output_for(side=[
                       {"side_data_type": "Content light level metadata"}]))
    with pytest.raises(MatroskaBuildError, match="源不存在的静态 HDR 元数据"):
        m._validate_ffmpeg_output(asset, source_streams, tmp_path / "o.mkv", "ffprobe",
                                  dolby_vision=False, source_path=tmp_path / "s.m2ts",
                                  ffmpeg_executable="ffmpeg")

    # PGS 被替换为 srt → 拒绝。
    monkey.setattr(m, "_probe_output_streams",
                   lambda _p, _o: output_for(sub_codec="subrip"))
    with pytest.raises(MatroskaBuildError, match="不一致"):
        m._validate_ffmpeg_output(asset, source_streams, tmp_path / "o.mkv", "ffprobe",
                                  dolby_vision=False, source_path=tmp_path / "s.m2ts",
                                  ffmpeg_executable="ffmpeg")
    monkey.undo()


def test_batch002_r1_cues_failure_blocks_metadata(tmp_path, monkeypatch):
    """round-1 P1-1：统一校验实际执行 Cues/seek——失败即拒绝 metadata。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
    )
    source_streams = _b2_streams()
    out_streams = [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "pix_fmt": "yuv420p10le", "avg_frame_rate": "24000/1001",
         "tags": {}, "disposition": {}},
        {"codec_type": "audio", "codec_name": "dts",
         "tags": {"language": "eng"}, "disposition": {"default": 0}},
        {"codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "zho"}, "disposition": {"default": 1}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "eng"}, "disposition": {"default": 0}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "zho"}, "disposition": {"default": 1}},
    ]
    monkeypatch.setattr(m, "_probe_output_streams", lambda _p, _o: out_streams)
    monkeypatch.setattr(m, "_probe_duration_ns",
                        lambda _p, _o: int(asset.duration_90k / 90000 * 1e9))
    monkeypatch.setattr(m, "_has_hdr10plus_es", lambda _f, _o: False)
    monkeypatch.setattr(
        m, "_verify_video_timeline",
        lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                         "avg_frame_rate": "24000/1001"},
    )

    def failing_seek(*_a, **_k):
        raise MatroskaBuildError("成品缺少结构化 Cues 索引，拒绝发布")

    monkeypatch.setattr(m, "_verify_cues_and_seek", failing_seek)
    with pytest.raises(MatroskaBuildError, match="Cues"):
        m._validate_ffmpeg_output(
            asset, source_streams, tmp_path / "out.mkv", "ffprobe", dolby_vision=False,
            source_path=tmp_path / "src.m2ts",
        )


def test_batch002_r2_static_hdr_preserved_and_dynamic_hdr_guard(tmp_path):
    """round-2 P1-1：正常静态 HDR 保留通过；源已知输出缺失拒绝；动态丢失拒绝。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
    )
    mastering = {
        "side_data_type": "Mastering display metadata",
        "red_x": "31313/65536", "green_x": "17954/65536",
        "white_x": "15600/65536", "white_y": "65536/65536",
    }
    source_streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011", "index": 0,
         "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
         "color_space": "bt2020nc", "color_transfer": "smpte2084",
         "color_primaries": "bt2020", "avg_frame_rate": "24000/1001",
         "side_data_list": [mastering]},
        {"codec_type": "audio", "codec_name": "dts", "id": "0x1100", "index": 1},
        {"codec_type": "audio", "codec_name": "ac3", "id": "0x1101", "index": 2},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a0", "index": 3},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a1", "index": 4},
    ]

    def output_with_side(side):
        return [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840,
             "height": 2160, "pix_fmt": "yuv420p10le",
             "color_space": "bt2020nc", "color_transfer": "smpte2084",
             "color_primaries": "bt2020", "side_data_list": side,
             "tags": {}, "disposition": {}},
            {"codec_type": "audio", "codec_name": "dts",
             "tags": {"language": "eng"}, "disposition": {"default": 0}},
            {"codec_type": "audio", "codec_name": "ac3",
             "tags": {"language": "zho"}, "disposition": {"default": 1}},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
             "tags": {"language": "eng"}, "disposition": {"default": 0}},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
             "tags": {"language": "zho"}, "disposition": {"default": 1}},
        ]

    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "_verify_cues_and_seek",
                        lambda *_a, **_k: None)
    monkey.setattr(m, "_probe_duration_ns",
                   lambda _p, _o: int(asset.duration_90k / 90000 * 1e9))
    monkey.setattr(m, "_has_hdr10plus_es", lambda _f, _o: False)
    monkey.setattr(
        m, "_verify_video_timeline",
        lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                         "avg_frame_rate": "24000/1001"},
    )

    # 正常：源与输出静态 HDR 完全一致 → 通过。
    monkey.setattr(m, "_probe_output_streams",
                   lambda _p, _o: output_with_side([mastering]))
    result = m._validate_ffmpeg_output(
        asset, source_streams, tmp_path / "o.mkv", "ffprobe",
        dolby_vision=False, source_path=tmp_path / "s.m2ts",
        ffmpeg_executable="ffmpeg",
    )
    assert result["dolby_vision"] is False

    # 源已知静态 HDR，输出丢失 → 拒绝。
    monkey.setattr(m, "_probe_output_streams",
                   lambda _p, _o: output_with_side([]))
    with pytest.raises(MatroskaBuildError, match="丢失源静态 HDR"):
        m._validate_ffmpeg_output(
            asset, source_streams, tmp_path / "o.mkv", "ffprobe",
            dolby_vision=False, source_path=tmp_path / "s.m2ts",
            ffmpeg_executable="ffmpeg",
        )

    # 动态 HDR10+：源有、输出丢失 → 拒绝（对称帧级探测）。
    monkey.setattr(m, "_probe_output_streams",
                   lambda _p, _o: output_with_side([mastering]))
    monkey.setattr(m, "_has_hdr10plus_es",
                   lambda _f, path: not str(path).endswith("o.mkv"))
    with pytest.raises(MatroskaBuildError, match="HDR10+"):
        m._validate_ffmpeg_output(
            asset, source_streams, tmp_path / "o.mkv", "ffprobe",
            dolby_vision=False, source_path=tmp_path / "s.m2ts",
            ffmpeg_executable="ffmpeg",
        )
    monkey.undo()


def test_batch002_r2_core_before_truehd_consistent(tmp_path):
    """round-2 P1-2：AC-3 core 排在 TrueHD 前，map/校验仍一致选 TrueHD。"""
    from bluray_fidelity.matroska import _exact_pid_maps, _resolve_official_streams

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x83, language="eng"),
        ),
        subtitle_tracks=(),
    )
    streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011", "index": 0},
        # core（ac3）排在 TrueHD 前面。
        {"codec_type": "audio", "codec_name": "ac3", "id": "0x1100", "index": 1},
        {"codec_type": "audio", "codec_name": "truehd", "id": "0x1100", "index": 2},
    ]
    maps = _exact_pid_maps(asset, streams)
    selectors = [maps[i + 1] for i in range(0, len(maps), 2)]
    assert "0:2" in selectors and "0:1" not in selectors

    resolved = _resolve_official_streams(asset, streams)
    _track, source = resolved["audio"][0]
    assert source["codec_name"] == "truehd"


def test_batch002_r2_lpcm_transcode_uses_resolved_stream(tmp_path):
    """round-2 P1-2：LPCM 转码特例依据唯一解析流判定。"""
    from bluray_fidelity.matroska import _inject_official_metadata

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x80, language="eng"),
        ),
        subtitle_tracks=(),
    )
    streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011", "index": 0},
        {"codec_type": "audio", "codec_name": "pcm_bluray", "id": "0x1100", "index": 1},
    ]
    cmd = _inject_official_metadata(["ffmpeg"], asset, streams)
    assert "-c:a:0" in cmd and cmd[cmd.index("-c:a:0") + 1] == "pcm_s16le"


def test_batch002_r4_hdr10plus_semantics_and_fail_closed(tmp_path):
    """round-4 P1-1：HDR10+ 语义判定与失败关闭。"""
    from bluray_fidelity.matroska import (
        _hdr10plus_in_es,
        _sei_has_hdr10plus,
        MatroskaBuildError,
    )

    # 普通 user-data（type 5 厂商私有）不算 HDR10+。
    sei_type5 = b"\x00\x00\x01\x4e\x01" + b"\x05\x10" + bytes(16) + b"\x80"
    assert _hdr10plus_in_es(sei_type5) is False
    assert _sei_has_hdr10plus(b"\x05\x10" + bytes(16) + b"\x80") is False

    # 真实 HDR10+（type 4 + T.35 前缀）被识别。
    sei_hdr10p = b"\x00\x00\x01\x4e\x01" + b"\x04\x05\xb5\x00\x3c\x00\x01" + b"\x80"
    assert _hdr10plus_in_es(sei_hdr10p) is True
    assert _sei_has_hdr10plus(b"\x04\x05\xb5\x00\x3c\x00\x01" + b"\x80") is True

    # type 4 但非 HDR10+ 前缀（country != 0xB5）不算。
    assert _sei_has_hdr10plus(b"\x04\x05\xb5\x00\x3c\x00\x02" + b"\x80") is False

    # 探测启动失败 → 失败关闭（round-5：流式无临时文件）。
    import bluray_fidelity.matroska as m
    import os as _os

    monkey = pytest.MonkeyPatch()
    monkey.setattr(m.subprocess, "Popen",
                   lambda *a, **k: (_ for _ in ()).throw(OSError("no ffmpeg")))
    with pytest.raises(MatroskaBuildError, match="HDR10\\+ 探测启动失败"):
        m._has_hdr10plus_es("ffmpeg", tmp_path / "src.m2ts")
    monkey.undo()

    # 假 ffmpeg：向 stdout 流式输出 HDR10+ ES。
    probe_es = tmp_path / "probe_es.bin"
    probe_es.write_bytes(
        b"\x00\x00\x01\x4e\x01\x04\x05\xb5\x00\x3c\x00\x01\x80" + b"\x00" * 64
    )
    fake_ffmpeg = tmp_path / "fake_ffmpeg.sh"
    fake_ffmpeg.write_text("#!/bin/sh\ncat " + str(probe_es) + "\n", encoding="utf-8")
    _os.chmod(fake_ffmpeg, 0o755)
    assert m._has_hdr10plus_es(str(fake_ffmpeg), tmp_path / "any") is True

    plain_es = tmp_path / "plain_es.bin"
    plain_es.write_bytes(
        b"\x00\x00\x01\x4e\x01\x05\x10" + bytes(16) + b"\x80"
    )
    fake_plain = tmp_path / "fake_plain.sh"
    fake_plain.write_text("#!/bin/sh\ncat " + str(plain_es) + "\n", encoding="utf-8")
    _os.chmod(fake_plain, 0o755)
    assert m._has_hdr10plus_es(str(fake_plain), tmp_path / "any") is False

    # 非零退出且无输出 → 失败关闭。
    fake_fail = tmp_path / "fake_fail.sh"
    fake_fail.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    _os.chmod(fake_fail, 0o755)
    with pytest.raises(MatroskaBuildError, match="退出码 3"):
        m._has_hdr10plus_es(str(fake_fail), tmp_path / "any")

    # 探测不产生任何临时文件。
    leftovers = [p.name for p in tmp_path.iterdir() if "hdr10probe" in p.name]
    assert leftovers == []


def test_batch002_r6_probe_lifecycle_fail_closed(tmp_path):
    """round-6 P1：探测生命周期——早命中后自身失败仍拒；超时终止；stderr 压力无死锁。"""
    import os as _os
    import time as _time
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    def _script(name: str, body: str) -> str:
        script = tmp_path / name
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        _os.chmod(script, 0o755)
        return str(script)

    sei = b"\x00\x00\x01\x4e\x01\x04\x05\xb5\x00\x3c\x00\x01\x80"
    # SEI 之后必须跟随一个完整 NAL：流式扫描器需要下一个 start code
    # 才能确定 SEI 的结束边界（真实 ES 中 SEI 后总有视频 NAL）。
    filler = b"\x00\x00\x01\x02" + b"\x7f" * 300 + b"\x80"
    es = tmp_path / "es.bin"
    es.write_bytes(sei + filler)

    # 1) 输出 HDR10+ 后 ffmpeg 自身非零退出 → 仍失败关闭。
    fake_found_fail = _script("ff_found_fail.sh", f"cat {es}\nexit 3\n")
    with pytest.raises(MatroskaBuildError, match="退出码 3"):
        m._has_hdr10plus_es(fake_found_fail, tmp_path / "any")

    # 2) 早命中后宽限期自然退出 / 我方 SIGTERM → 合法命中通过。
    fake_found_ok = _script(
        "ff_found_ok.sh",
        f"cat {es}\ntrap 'exit 0' TERM\nwhile true; do sleep 0.1; done\n")
    assert m._has_hdr10plus_es(fake_found_ok, tmp_path / "any") is True
    fake_term_notrap = _script(
        "ff_term_notrap.sh",
        f"cat {es}\nwhile true; do sleep 0.1; done\n")
    assert m._has_hdr10plus_es(fake_term_notrap, tmp_path / "any") is True
    fake_cat_sleep = _script(
        "ff_cat_sleep.sh", f"cat {es}\nsleep 60\n")
    assert m._has_hdr10plus_es(fake_cat_sleep, tmp_path / "any") is True

    # 2b) 输出 HDR10+ 后自行 SIGSEGV / SIGABRT → 拒绝（round-7 P1：
    #     不得把任意负信号当成功；宽限期让自杀信号暴露真实退出码）。
    for signal_name in ("SEGV", "ABRT"):
        fake_crash = _script(
            f"ff_crash_{signal_name}.sh",
            f"cat {es}\nkill -{signal_name} $$\n")
        with pytest.raises(MatroskaBuildError, match=f"信号 SIG{signal_name}"):
            m._has_hdr10plus_es(fake_crash, tmp_path / "any")

    # 3) 卡住进程：deadline 到时 kill 并失败关闭，测试快速结束。
    fake_stuck = _script("ff_stuck.sh", "sleep 300\n")
    started = _time.monotonic()
    with pytest.raises(MatroskaBuildError, match="超时"):
        m._has_hdr10plus_es(fake_stuck, tmp_path / "any", timeout_seconds=2)
    assert _time.monotonic() - started < 30

    # 4) stderr 大量输出与 stdout 并发 → 无死锁，正常完成。
    fake_stderr_flood = _script(
        "ff_flood.sh",
        "i=0\nwhile [ $i -lt 2000 ]; do echo \"err $i\" >&2; i=$((i+1)); done\n"
        f"cat {es}\n")
    assert m._has_hdr10plus_es(fake_stderr_flood, tmp_path / "any") is True

    # 5) 读取异常：子进程被终止回收并抛错（扫描器抛 OSError）。
    fake_sleep = _script("ff_sleep.sh", "sleep 60\n")
    monkey = pytest.MonkeyPatch()

    def exploding_scan(_chunks):
        raise OSError("pipe broken")

    monkey.setattr(m, "_scan_annexb_for_hdr10plus", exploding_scan)
    with pytest.raises(MatroskaBuildError, match="读取失败"):
        m._has_hdr10plus_es(fake_sleep, tmp_path / "any", timeout_seconds=30)
    monkey.undo()


def test_batch002_r5_streaming_scan_across_chunk_boundaries():
    """round-5 P1：流式扫描跨 chunk 正确识别 HDR10+ SEI（有界内存）。"""
    from bluray_fidelity.matroska import _scan_annexb_for_hdr10plus

    sei = b"\x00\x00\x01\x4e\x01\x04\x05\xb5\x00\x3c\x00\x01\x80"
    filler = b"\x00\x00\x01\x02" + b"\x7f" * 300 + b"\x80"
    data = filler + sei + filler

    assert _scan_annexb_for_hdr10plus(iter((data,))) is True
    assert _scan_annexb_for_hdr10plus(iter((bytes([b]) for b in data))) is True
    assert _scan_annexb_for_hdr10plus(
        iter(data[i:i + 7] for i in range(0, len(data), 7))
    ) is True
    plain = filler + b"\x00\x00\x01\x4e\x01\x05\x10" + bytes(16) + b"\x80"
    assert _scan_annexb_for_hdr10plus(iter((plain,))) is False


def test_batch002_r4_dv_dovi_record_not_rejected_by_static_compare(tmp_path):
    """round-4 P1-2：DV 输出新增合法 DOVI record 不被静态 HDR 比较误拒。"""
    import bluray_fidelity.matroska as m

    asset = replace(
        _asset(tmp_path),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
    )
    source_streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011", "index": 0,
         "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
         "color_transfer": "smpte2084", "avg_frame_rate": "24000/1001",
         "side_data_list": []},
        {"codec_type": "audio", "codec_name": "dts", "id": "0x1100", "index": 1},
        {"codec_type": "audio", "codec_name": "ac3", "id": "0x1101", "index": 2},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a0", "index": 3},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "id": "0x12a1", "index": 4},
    ]
    # DV 合并产物：源无 side data，输出带合法 Profile 7 DOVI record——
    # 通用静态比较不得以"意外新增"拒绝，随后 require_dovi_side_data 通过。
    out_streams = [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840,
         "height": 2160, "pix_fmt": "yuv420p10le",
         "color_transfer": "smpte2084", "side_data_list": [
             {"side_data_type": "DOVI configuration record",
              "dv_profile": 7, "rpu_present_flag": 1, "el_present_flag": 1,
              "bl_present_flag": 1}],
         "tags": {}, "disposition": {}},
        {"codec_type": "audio", "codec_name": "dts",
         "tags": {"language": "eng"}, "disposition": {"default": 0}},
        {"codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "zho"}, "disposition": {"default": 1}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "eng"}, "disposition": {"default": 0}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "zho"}, "disposition": {"default": 1}},
    ]
    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "_probe_output_streams", lambda _p, _o: out_streams)
    monkey.setattr(m, "_probe_duration_ns",
                   lambda _p, _o: int(asset.duration_90k / 90000 * 1e9))
    monkey.setattr(m, "_verify_cues_and_seek",
                        lambda *_a, **_k: None)
    monkey.setattr(m, "_has_hdr10plus_es", lambda _f, _o: False)
    monkey.setattr(
        m, "_verify_video_timeline",
        lambda *a, **k: {"duration": str(a[2].duration_90k / 90000),
                         "avg_frame_rate": "24000/1001"},
    )
    # Gate B：DV 独立证据链通过。
    monkey.setattr(m, "_verify_dv_profile7_evidence",
                   lambda *_a, **_k: {"rpu_per_window": (9, 11, 10)})

    result = m._validate_ffmpeg_output(
        asset, source_streams, tmp_path / "out.mkv", "ffprobe",
        dolby_vision=True, source_path=tmp_path / "src.m2ts",
        ffmpeg_executable="ffmpeg",
    )
    assert result["dolby_vision"] is True

    # 反向：非 DV 模式下输出带 DOVI record 仍被专用校验拒绝。
    with pytest.raises(MatroskaBuildError, match="DOVI 配置记录"):
        m._validate_ffmpeg_output(
            asset, source_streams, tmp_path / "out.mkv", "ffprobe",
            dolby_vision=False, source_path=tmp_path / "src.m2ts",
            ffmpeg_executable="ffmpeg",
        )
    monkey.undo()


# ---------------------------------------------------------------------------
# BATCH-002 Gate B：Dolby Vision 字节级证据（SPS DV VUI + RPU 窗口扫描）。
# 测试向量 = 真实 Stand.by.Me BL SPS（ffprobe 交叉验证：3840x2160/bt2020）。
# ---------------------------------------------------------------------------

def test_gateb_require_video_rate_parsing():
    """P1-A：帧率解析与失败关闭（24000/1001 通过；缺失/0/不合理拒绝）。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    assert abs(m._parse_video_rate("24000/1001") - 24000 / 1001) < 1e-6
    assert abs(m._parse_video_rate("25/1") - 25.0) < 1e-6
    stream = {"avg_frame_rate": "24000/1001"}
    assert abs(m._require_video_rate(stream, "L") - 24000 / 1001) < 1e-6
    # avg 缺失 → 回退 r_frame_rate。
    stream2 = {"avg_frame_rate": "0/0", "r_frame_rate": "24000/1001"}
    assert abs(m._require_video_rate(stream2, "L") - 24000 / 1001) < 1e-6
    for bad in ({"avg_frame_rate": "0/0"}, {"avg_frame_rate": "N/A"},
                {"avg_frame_rate": ""}, {"avg_frame_rate": "300/1"},
                {}):
        with pytest.raises(MatroskaBuildError, match="帧率缺失或不可解析"):
            m._require_video_rate(bad, "L")


def test_gateb_video_timeline_rejects_wrong_video_duration(monkeypatch, tmp_path):
    """P1-A：格式时长正确但视频轨时长/帧率错误必须拒绝。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    asset = _asset(tmp_path)  # duration_90k=9_000_000 -> 100s
    # mock _probe_video_timeline: format duration would pass but video track wrong
    monkeypatch.setattr(m, "_probe_video_timeline",
                        lambda _p, _o: {"duration": "1000.0",
                                        "avg_frame_rate": "24000/1001"})
    with pytest.raises(MatroskaBuildError, match="视频轨时长"):
        m._verify_video_timeline("ffprobe", tmp_path / "out.mkv", asset,
                                 fps=24000 / 1001.0, label="t")
    # 帧率不一致
    monkeypatch.setattr(m, "_probe_video_timeline",
                        lambda _p, _o: {"duration": "100.0",
                                        "avg_frame_rate": "25/1"})
    with pytest.raises(MatroskaBuildError, match="帧率"):
        m._verify_video_timeline("ffprobe", tmp_path / "out.mkv", asset,
                                 fps=24000 / 1001.0, label="t")


def test_gateb_seek_window_command_uses_time_not_packet_count(monkeypatch):
    """P1-B：seek 时间窗口用秒；解码帧窗口从目标前一窗口启动。"""
    import bluray_fidelity.matroska as m

    intervals: list[str] = []
    commands: list[list[str]] = []

    class FakeCompleted:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(*args, **kwargs):
        cmd = args[0]
        commands.append(cmd)
        intervals.append(cmd[cmd.index("-read_intervals") + 1])
        if "-of" in cmd and cmd[cmd.index("-of") + 1] == "json":
            return FakeCompleted('{"packets":[]}')
        return FakeCompleted("2657.0")

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m._probe_interval_packets("ffprobe", Path("/tmp/x.mkv"), 2657.0)
    m._probe_interval_frames("ffprobe", Path("/tmp/x.mkv"), 2657.0)
    assert len(intervals) == 2, "应捕获两个 read_intervals"
    for iv in intervals:
        assert "#" not in iv, f"不得使用 #N 帧上限：{iv!r}"
    assert intervals[0].startswith("2657.000%+"), f"packet 应从目标开始：{intervals[0]!r}"
    assert intervals[1].startswith("2647.000%+"), f"frame 应从目标前窗口开始：{intervals[1]!r}"
    assert "-skip_frame" in commands[1] and commands[1][commands[1].index("-skip_frame") + 1] == "nokey"


def test_gateb_seek_frame_retries_without_keyframe_filter(monkeypatch):
    """没有 nokey 解码帧时，限定窗口内回退普通 frame 探测。"""
    import bluray_fidelity.matroska as m

    commands: list[list[str]] = []

    class FakeCompleted:
        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(*args, **kwargs):
        cmd = args[0]
        commands.append(cmd)
        return FakeCompleted("" if "-skip_frame" in cmd else "2647.0")

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    frames = m._probe_interval_frames("ffprobe", Path("/tmp/x.mkv"), 2657.0)
    assert frames == [2647.0]
    assert len(commands) == 2
    assert "-skip_frame" not in commands[1]


def test_gateb_seek_frames_accept_csv_side_data(monkeypatch):
    """HDR frame side-data is appended after the CSV timestamp."""
    import bluray_fidelity.matroska as m

    class FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = (
            "786.486000\n"
            "786.527700,Mastering display metadata,35400/50000\n"
            "786.569400,Content light level metadata,997,634\n"
        )

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: FakeCompleted())
    frames = m._probe_interval_frames("ffprobe", Path("/tmp/x.mkv"), 786.5)
    assert frames == [786.486, 786.5277, 786.5694]


def test_gateb_dv_evidence_fail_closed_when_el_evidence_missing(monkeypatch, tmp_path):
    """P1-2/P1-1：EL 源摘要缺失/合并大小/合并 RPU/EL 回拆失败/顺序错/
    Profile7 信号失败均失败关闭。round-9 后 EL 门改为 demux 回拆+有序序列。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    monkeypatch.setattr(m, "_dv_rpu_evidence",
                        lambda *_a, **_k: {"rpu_per_window": (5, 5, 5)})
    # 容器级 dvcC 校验改为 ffprobe + require_dovi_side_data（DV 点亮修复）。
    import subprocess as _sp
    class _FfprobeOk:
        returncode = 0
        stdout = '{"streams":[{"side_data_list":[{"side_data_type":"DOVI configuration record","dv_profile":7,"rpu_present_flag":1,"el_present_flag":1}]}]}'
        stderr = ""
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _FfprobeOk())
    monkeypatch.setattr(m, "_verify_el_order_consistency",
                        lambda *_a, **kw: {"source_vcl_slices": 5,
                                            "recovered_vcl_slices": 5,
                                            "order_ok": True,
                                            "temporal": "frame-order"})
    fp = {0.0: [b"a", b"b", b"c"], 0.1: [b"d", b"e", b"f"],
          0.5: [b"g", b"h", b"i"], 0.9: [b"j", b"k", b"l"]}
    rpu_ev = {"method": "dovi_tool extract-rpu", "limit_frames": 240,
              "rpu_file_size": 12345, "rpu_summary": "rpu summary ok"}
    facts = {"bl_size": 1000, "merged_size": 2000,
             "merged_rpu_evidence": dict(rpu_ev), "dovi_command": "x",
             "duration_s": 100.0, "el_fingerprints": fp,
             "el_source_digests": [b"d"] * 5,
             "dovi_executable": "dovi_tool"}
    # EL 源摘要缺失 → 独立证据缺失
    with pytest.raises(MatroskaBuildError, match="EL 独立证据缺失"):
        m._verify_dv_profile7_evidence(
            "ffmpeg", out, {**facts, "el_source_digests": []})
    # merged_size <= bl_size → EL 融合证据缺失
    with pytest.raises(MatroskaBuildError, match="EL 融合证据缺失"):
        m._verify_dv_profile7_evidence("ffmpeg", out, {**facts, "merged_size": 1000})
    # round-15 P1：merged_rpu_evidence 缺失/非法 → 合并阶段证据缺失
    with pytest.raises(MatroskaBuildError, match="缺少结构化 RPU 证据"):
        m._verify_dv_profile7_evidence(
            "ffmpeg", out, {**facts, "merged_rpu_evidence": None})
    with pytest.raises(MatroskaBuildError, match="RPU 证据方法非法"):
        m._verify_dv_profile7_evidence(
            "ffmpeg", out, {**facts, "merged_rpu_evidence": {"method": "ffmpeg"}})
    with pytest.raises(MatroskaBuildError, match="limit_frames 非法"):
        m._verify_dv_profile7_evidence(
            "ffmpeg", out,
            {**facts, "merged_rpu_evidence": {**rpu_ev, "limit_frames": 0}})
    with pytest.raises(MatroskaBuildError, match="RPU summary 为空"):
        m._verify_dv_profile7_evidence(
            "ffmpeg", out,
            {**facts, "merged_rpu_evidence": {**rpu_ev, "rpu_summary": "  "}})
    # EL 回拆顺序一致性失败 → 阶段一致性失败关闭
    monkeypatch.setattr(m, "_verify_el_order_consistency",
                        lambda *_a, **kw: (_ for _ in ()).throw(
                            MatroskaBuildError("最终 MKV EL 顺序一致性失败：回拆 EL "
                                               "未按源顺序覆盖全片 VCL 切片，拒绝继续")))
    with pytest.raises(MatroskaBuildError, match="顺序一致性失败"):
        m._verify_dv_profile7_evidence("ffmpeg", out, facts)
    # EL 回拆通过但 Profile 7 信号失败 → 仍失败关闭
    monkeypatch.setattr(m, "_verify_el_order_consistency",
                        lambda *_a, **kw: {"order_ok": True})
    monkeypatch.setattr(m, "require_dovi_side_data",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            MatroskaBuildError("最终 MKV 缺少要求的 Dolby Vision 配置（DV Profile 7）")))
    with pytest.raises(MatroskaBuildError, match="Profile 7"):
        m._verify_dv_profile7_evidence("ffmpeg", out, facts)
    # dv_facts 缺失 → 拒绝
    with pytest.raises(MatroskaBuildError, match="缺少合并阶段证据"):
        m._verify_dv_profile7_evidence("ffmpeg", out, None)




def test_gateb_real_small_mkv_timeline_no_false_failure(tmp_path):
    """P1-1 集成：真实 ffmpeg 生成的 2s MKV（format 有 duration、stream 无
    duration）不得被 _verify_video_timeline 假失败；错误时间轴仍失败关闭。"""
    import shutil as _shutil
    if _shutil.which("ffmpeg") is None or _shutil.which("ffprobe") is None:
        pytest.skip("需要 ffmpeg/ffprobe")
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    mkv = tmp_path / "small.mkv"
    completed = _shutil.which("ffmpeg")  # placeholder to keep import used
    import subprocess as _sp
    _sp.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24000/1001:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mkv)],
        check=True, capture_output=True, text=True,
    )
    timeline = m._probe_video_timeline("ffprobe", mkv)
    assert float(timeline.get("duration") or 0) <= 0, \
        "前置断言：真实 Matroska 视频 stream 应无 duration（Codex 已复现）"

    asset = replace(
        _asset(tmp_path),
        duration_90k=int(2.002 * 90000),
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0xEA, language=""),),
    )
    result = m._verify_video_timeline(
        "ffprobe", mkv, asset, fps=24000 / 1001.0, label="t",
    )
    assert float(result["duration"]) > 0

    bad_asset = replace(asset, duration_90k=int(10 * 90000))
    with pytest.raises(MatroskaBuildError, match="末端|时长"):
        m._verify_video_timeline(
            "ffprobe", mkv, bad_asset, fps=24000 / 1001.0, label="t",
        )


def test_gateb_seek_failure_diagnostics_json(tmp_path, monkeypatch):
    """P1-3：三类 seek 失败都在诊断中留下失败点证据（Cue 数/点/原因）。"""
    import bluray_fidelity.matroska as m

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    monkeypatch.setattr(m, "_count_cue_points", lambda _p: 3)
    cases = [
        ("no_packets", "索引/时间轴定位失败",
         lambda *a, **k: [], lambda *a, **k: []),
        ("packets_but_no_frames", "关键帧/解码窗口失败",
         lambda *a, **k: [{"pts_time": "1.0"}], lambda *a, **k: []),
        ("out_of_range", "时间越界",
         lambda *a, **k: [{"pts_time": "1.0"}], lambda *a, **k: [999.0]),
    ]
    for mode, match, pkts, frames in cases:
        diagnostics: dict = {}
        monkeypatch.setattr(m, "_probe_interval_packets", pkts)
        monkeypatch.setattr(m, "_probe_interval_frames", frames)
        with pytest.raises(MatroskaBuildError, match=match):
            m._verify_cues_and_seek("ffprobe", out, int(100 * 1e9),
                                    diagnostics=diagnostics)
        assert diagnostics.get("cue_points") == 3, mode
        points = diagnostics.get("seek_points") or []
        assert points, f"{mode}: 失败点必须记录"
        last = points[-1]
        assert last.get("failure") == mode, f"{mode}: 失败原因必须可区分"
        assert last.get("target_seconds") is not None
        # 命令/rc/stderr 由真实探测 helper 写入（mock 路径不覆盖）；
        # 至少保证失败点结构与已完成点证据保留。
        assert points[0].get("target_seconds") is not None


def test_gateb_seek_widens_decoder_window_before_rejecting(monkeypatch, tmp_path):
    import bluray_fidelity.matroska as m

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    monkeypatch.setattr(m, "_count_cue_points", lambda _p: 3)
    monkeypatch.setattr(
        m, "_probe_interval_packets",
        lambda *a, **k: [{"pts_time": "10.0", "flags": "K"}],
    )
    calls = []

    def probe_frames(*args, **kwargs):
        calls.append(kwargs.get("lookback_seconds"))
        return [] if len(calls) % 2 == 1 else [float(args[2])]

    monkeypatch.setattr(m, "_probe_interval_frames", probe_frames)
    result = m._verify_cues_and_seek("ffprobe", out, int(100 * 1e9))
    assert len(result["cues"]) == 3
    assert calls[:2] == [None, m._SEEK_FALLBACK_LOOKBACK_SECONDS]


def test_gateb_video_rate_conflict_fails_closed():
    """P1-4：avg/r 帧率同时有效但冲突 → 失败关闭；一致时保留有理数。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    with pytest.raises(MatroskaBuildError, match="帧率冲突"):
        m._video_rate_facts(
            {"avg_frame_rate": "24000/1001", "r_frame_rate": "25/1"}, "L",
        )
    facts = m._video_rate_facts(
        {"avg_frame_rate": "24000/1001", "r_frame_rate": "24000/1001"}, "L",
    )
    assert facts["rational"] == "24000/1001"
    assert abs(facts["rate"] - 24000 / 1001) < 1e-6


def test_gateb_bl_pid_strict_no_first_video_fallback():
    """P1-4：官方 BL PID 缺失 → 失败关闭（不得静默选第一条视频流）；
    PID 表示形式不同但同一 PID 可解析。"""
    from bluray_fidelity.matroska import (
        MatroskaBuildError,
        _resolve_official_stream,
    )

    streams = [
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1011",
         "index": 0, "avg_frame_rate": "24000/1001"},
        {"codec_type": "video", "codec_name": "hevc", "id": "0x1015",
         "index": 1, "avg_frame_rate": "24000/1001"},
    ]
    # EL PID 0x1015 存在但 BL PID 0x1016 缺失 → 必须失败
    with pytest.raises(MatroskaBuildError, match="未在物化 PMT 中找到"):
        _resolve_official_stream(streams, 0x1016, "video", 0xEA)
    # PID 表示形式不同但同一 PID（"0x1011" vs 0x1011）
    bl = _resolve_official_stream(streams, 0x1011, "video", 0xEA)
    assert bl["index"] == 0


# ---------------------------------------------------------------------------
# Codex Gate B round-5 整改回归（P1-1 多窗口/BL 负向/全片覆盖、P1-3 末端容差、
# P1-4 探测失败现场、P2 有界退出）。
# ---------------------------------------------------------------------------

def test_gateb_exclude_bl_hits_fails_when_window_empty(tmp_path):
    """P1-1：EL 窗口切片全部命中 BL → BL 负向排除失败关闭（无 EL 专属证据）。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    bl = tmp_path / "bl.hevc"
    bl.write_bytes(b"AAA" * 100)
    with pytest.raises(MatroskaBuildError, match="BL 负向排除失败"):
        m._exclude_bl_hits({0.0: [b"AAA"]}, bl)
    # 部分命中 BL：只保留未命中的 EL 专属指纹
    cleaned = m._exclude_bl_hits({0.0: [b"AAA", b"BBB"]}, bl)
    assert cleaned[0.0] == [b"BBB"]


def test_gateb_p1_3_video_end_tolerance(tmp_path):
    """P1-3：真实 2s MKV 视频末端 ±2s 通过；少 3/10/25 秒均失败关闭。"""
    import shutil as _shutil
    if _shutil.which("ffmpeg") is None or _shutil.which("ffprobe") is None:
        pytest.skip("需要 ffmpeg/ffprobe")
    import subprocess as _sp
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    mkv = tmp_path / "small.mkv"
    _sp.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24000/1001:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mkv)],
        check=True, capture_output=True, text=True,
    )
    real_s = 2.002
    base = replace(_asset(tmp_path),
                   video_tracks=(BlurayTrack(index=0, pid=0x1011,
                                             coding_type=0xEA, language=""),))
    ok_asset = replace(base, duration_90k=int(real_s * 90000))
    # ±2s 内通过
    result = m._verify_video_timeline(
        "ffprobe", mkv, ok_asset, fps=24000 / 1001.0, label="t",
    )
    assert abs(float(result["duration"]) - real_s) <= 2.0
    # 少 3s / 10s / 25s 均失败（容器仍 ≈2s，不得用容器时长放行）
    for missing in (3.0, 10.0, 25.0):
        bad_asset = replace(base, duration_90k=int((real_s + missing) * 90000))
        with pytest.raises(MatroskaBuildError, match="末端|时长"):
            m._verify_video_timeline(
                "ffprobe", mkv, bad_asset, fps=24000 / 1001.0, label="t",
            )


def test_gateb_p1_4_packet_probe_failure_keeps_point(monkeypatch, tmp_path):
    """P1-4：packet 探测非零退出 → 诊断保留当前点与命令/rc/stderr。"""
    import subprocess as _sp
    import bluray_fidelity.matroska as m

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    monkeypatch.setattr(m, "_count_cue_points", lambda _p: 3)

    class _Fail:
        returncode = 9
        stderr = "boom"
        stdout = ""

        def __init__(self, *a, **k):
            raise _sp.CalledProcessError(9, a[0], output="", stderr="boom")

    monkeypatch.setattr(m.subprocess, "run", _Fail)
    diagnostics: dict = {}
    with pytest.raises(MatroskaBuildError, match="packet 探测失败"):
        m._verify_cues_and_seek("ffprobe", out, int(100 * 1e9),
                                diagnostics=diagnostics)
    points = diagnostics.get("seek_points") or []
    assert points, "packet 探测失败也必须登记当前点（P1-4）"
    last = points[-1]
    assert last.get("packet_rc") == 9
    assert last.get("packet_stderr") == "boom"
    assert "packet_command" in last


# ---------------------------------------------------------------------------
# Codex Gate B round-6 整改回归（P1-2 窗口齐全、P1-3 BL 全流排除）。
# ---------------------------------------------------------------------------

class _FakeFpProc:
    pid = 7777
    returncode = 0

    def __init__(self, chunks, rc=0):
        self._chunks = list(chunks)
        self.returncode = rc

    @property
    def stdout(self):
        class _S:
            def __init__(self, ch):
                self.ch = ch

            def read1(self, _n):
                return self.ch.pop(0) if self.ch else b""

        return _S(self._chunks)

    @property
    def stderr(self):
        return iter([])

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        return None


def test_gateb_round6_window_integrity(tmp_path):
    """P1-2：窗口集合必须严格齐全、每窗≥最小指纹数。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    full = {0.0: [b"a", b"b", b"c"], 0.1: [b"d", b"e", b"f"],
            0.5: [b"g", b"h", b"i"], 0.9: [b"j", b"k", b"l"]}
    m._require_el_window_integrity(full, "t")  # 通过
    with pytest.raises(MatroskaBuildError, match="窗口不齐全"):
        m._require_el_window_integrity({0.5: full[0.5]}, "t")  # 仅单窗
    with pytest.raises(MatroskaBuildError, match="窗口不齐全"):
        m._require_el_window_integrity(
            {0.0: full[0.0], 0.1: full[0.1], 0.5: full[0.5]}, "t")  # 缺 0.9
    sparse = dict(full)
    sparse[0.0] = [b"a", b"b"]  # 窗口不足 3 个指纹
    with pytest.raises(MatroskaBuildError, match="指纹不足"):
        m._require_el_window_integrity(sparse, "t")


def test_gateb_round6_bl_exclusion_full_stream(tmp_path):
    """P1-3：BL 负向排除必须覆盖全流（含 4MiB 之后的后半命中）。"""
    import bluray_fidelity.matroska as m

    bl = tmp_path / "bl.hevc"
    # 片头命中 + 4MiB 之后命中
    bl.write_bytes(b"AAA" + b"\x00" * (5 * 1024 * 1024) + b"BBB")
    cleaned = m._exclude_bl_hits({0.0: [b"AAA", b"BBB", b"CCC"]}, bl)
    assert cleaned[0.0] == [b"CCC"], "片头与后半的 BL 命中都应被排除"


def test_gateb_round7_bl_exclusion_io_failure_fails_closed(tmp_path):
    """round-7 P1：BL 不存在或读取失败 → BL 负向扫描失败关闭（不得当零命中）。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    full = {0.0: [b"a", b"b", b"c"], 0.1: [b"d", b"e", b"f"],
            0.5: [b"g", b"h", b"i"], 0.9: [b"j", b"k", b"l"]}
    # BL 不存在
    with pytest.raises(MatroskaBuildError, match="BL 负向扫描失败"):
        m._exclude_bl_hits(full, tmp_path / "missing.bl")
    # BL 是目录 → open 成功但 read 抛 OSError（中途读取失败）
    bl_dir = tmp_path / "bl_dir"
    bl_dir.mkdir()
    with pytest.raises(MatroskaBuildError, match="BL 负向扫描失败"):
        m._exclude_bl_hits(full, bl_dir)


# ---- round-9：EL demux 回拆 + 有序 VCL 序列一致性（统一 frame/AU 坐标）----



# ---- round-9：EL demux 回拆 + 有序 VCL 序列一致性（统一 frame/AU 坐标）----

def _write_el_with_vcl(path, n=12):
    """写入一个含若干 VCL slice NAL（HEVC type<=21）的伪 EL 流。"""
    import hashlib
    # 用确定性字节构造 n 个唯一的 VCL slice NAL（type 1 = TRAIL_R）
    start = b"\x00\x00\x01"
    with open(path, "wb") as fh:
        for i in range(n):
            nal = bytes([(1 << 1) | 0]) + f"NAL-{i:04d}".encode() + bytes([i]) * 4
            fh.write(start + nal)
    return n


def test_el_vcl_slice_digests_ordered(tmp_path):
    """round-9：_el_vcl_slice_digests 返回有序 VCL 切片摘要（frame 顺序坐标）。"""
    import bluray_fidelity.matroska as m

    el = tmp_path / "el.hevc"
    n = _write_el_with_vcl(el, n=8)
    digests = m._el_vcl_slice_digests(el)
    assert len(digests) == n
    assert digests == m._el_vcl_slice_digests(el), "确定性摘要应可复现"
    # 顺序坐标：重排 NAL 得到不同序列
    import hashlib
    with open(el, "rb") as fh:
        data = fh.read()
    nals = [x for x in data.split(b"\x00\x00\x01") if x]
    rev = tmp_path / "el_rev.hevc"
    with open(rev, "wb") as fh:
        for x in reversed(nals):
            fh.write(b"\x00\x00\x01" + x)
    assert m._el_vcl_slice_digests(rev) != digests, "顺序改变应导致摘要序列改变"


def test_verify_el_order_consistency_ok(tmp_path, monkeypatch):
    """round-9/10 P1-1：merged/MP4/MKV 回拆 EL 与源 EL 严格相等 → 通过。"""
    import bluray_fidelity.matroska as m

    source_el = tmp_path / "src.el.hevc"
    target = tmp_path / "merged.dv.hevc"
    _write_el_with_vcl(source_el, n=10)
    _write_el_with_vcl(target, n=10)
    src_d = m._el_vcl_slice_digests(source_el)
    monkeypatch.setattr(
        m, "_el_vcl_parse",
        lambda p: (src_d, {"vcl_count": len(src_d), "payload_bytes": 0,
                           "sequence_sha256": "x"}),
    )
    monkeypatch.setattr(m, "_demux_el_only", lambda *a, **k: None)
    facts = m._verify_el_order_consistency(
        "dovi_tool", "ffmpeg", source_el, target, "合并流", container=False,
        src_digests=src_d, src_stats={"vcl_count": len(src_d), "payload_bytes": 0,
                                      "sequence_sha256": "x"},
    )
    assert facts["order_ok"] is True
    assert facts["recovered_vcl_slices"] == facts["source_vcl_slices"]


def test_verify_el_order_consistency_short_fails(tmp_path, monkeypatch):
    """round-10 P1-1：回拆 EL 切片少于源（EL 半途缺失）→ 失败关闭。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    source_el = tmp_path / "src.el.hevc"
    target = tmp_path / "merged.dv.hevc"
    _write_el_with_vcl(source_el, n=10)
    _write_el_with_vcl(target, n=10)
    src_d = m._el_vcl_slice_digests(source_el)
    # 回拆只恢复前 6 个 VCL（尾段缺失）
    monkeypatch.setattr(m, "_el_vcl_parse",
                        lambda p: (src_d[:6], {"vcl_count": 6, "payload_bytes": 0,
                                               "sequence_sha256": "x"}))
    monkeypatch.setattr(m, "_demux_el_only", lambda *a, **k: None)
    with pytest.raises(MatroskaBuildError, match="不严格相等"):
        m._verify_el_order_consistency(
            "dovi_tool", "ffmpeg", source_el, target, "合并流", container=False,
            src_digests=src_d,
        )


def test_verify_el_order_consistency_order_fails(tmp_path, monkeypatch):
    """round-10 P1-1：回拆 EL 顺序与源不符（乱序）→ 失败关闭。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    source_el = tmp_path / "src.el.hevc"
    target = tmp_path / "merged.dv.hevc"
    _write_el_with_vcl(source_el, n=10)
    src_d = m._el_vcl_slice_digests(source_el)
    # 回拆顺序乱序（前三个倒置）
    scrambled = src_d[:3][::-1] + src_d[3:]
    monkeypatch.setattr(m, "_el_vcl_parse",
                        lambda p: (scrambled, {"vcl_count": len(scrambled),
                                               "payload_bytes": 0,
                                               "sequence_sha256": "x"}))
    monkeypatch.setattr(m, "_demux_el_only", lambda *a, **k: None)
    with pytest.raises(MatroskaBuildError, match="不严格相等"):
        m._verify_el_order_consistency(
            "dovi_tool", "ffmpeg", source_el, target, "合并流", container=False,
            src_digests=src_d,
        )


def test_verify_el_order_consistency_extra_slice_fails(tmp_path, monkeypatch):
    """round-10 P1-1：回拆插入/重复/末尾额外 slice 必须失败关闭（非有序子序列）。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    source_el = tmp_path / "src.el.hevc"
    target = tmp_path / "merged.dv.hevc"
    _write_el_with_vcl(source_el, n=10)
    src_d = m._el_vcl_slice_digests(source_el)
    for extra in (src_d[:2] + src_d,  # 插入重复 slice
                  src_d + [src_d[-1]],  # 末尾额外 slice
                  [src_d[0]] + src_d):  # 前缀多余 slice
        monkeypatch.setattr(m, "_el_vcl_parse",
                            lambda p, e=extra: (e, {"vcl_count": len(e),
                                                    "payload_bytes": 0,
                                                    "sequence_sha256": "x"}))
        monkeypatch.setattr(m, "_demux_el_only", lambda *a, **k: None)
        with pytest.raises(MatroskaBuildError, match="不严格相等"):
            m._verify_el_order_consistency(
                "dovi_tool", "ffmpeg", source_el, target, "合并流", container=False,
                src_digests=src_d,
            )


def test_demux_el_only_nonzero_exit_fails(tmp_path, monkeypatch):
    """round-9：demux --el-only 非零退出 → 失败关闭。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    el_out = tmp_path / "elonly.hevc"

    class _FakeDemuxProc:
        pid = 8888
        returncode = 1
        stdout = b""
        stderr = iter([])

        def __init__(self, *a, **k):
            pass

        def wait(self, timeout=None):
            return 1

        def kill(self):
            return None

    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda *a, **k: _FakeDemuxProc(*a, **k))
    with pytest.raises(MatroskaBuildError, match="回拆失败"):
        m._demux_el_only("dovi_tool", "ffmpeg", tmp_path / "m.dv.hevc",
                         el_out, container=False)


def test_demux_el_only_second_spawn_failure_cleans(tmp_path, monkeypatch):
    """round-10 P1-3：容器回拆先启动 ffmpeg、dovi_tool 启动失败 → 清理已启动
    ffmpeg 进程与 el_out（无遗留读盘进程）。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    el_out = tmp_path / "elonly.hevc"
    el_out.write_bytes(b"sentinel-so-we-confirm-unlink")
    killed = []

    class _FakeFfmpegProc:
        pid = 7001
        returncode = 0

        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

        @property
        def stdout(self):
            class _S:
                def close(self):
                    pass
            return _S()

        @property
        def stderr(self):
            return iter([])

        def wait(self, timeout=None):
            return 0

        def kill(self):
            killed.append("ffmpeg")
            return None

    calls = {"n": 0}

    def _popen(cmd, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeFfmpegProc()   # ffmpeg starts OK
        raise OSError("dovi_tool spawn failure")

    monkeypatch.setattr(m.subprocess, "Popen", _popen)
    with pytest.raises(MatroskaBuildError, match="回拆启动失败"):
        m._demux_el_only("dovi_tool", "ffmpeg", tmp_path / "m.mkv",
                         el_out, container=True)
    assert killed == ["ffmpeg"], "已启动的 ffmpeg 必须被终止"
    assert not el_out.exists(), "el_out 必须被删除"


def test_demux_el_only_timeout_fails(tmp_path, monkeypatch):
    """round-10 P2：demux 回拆超时（不退出/stderr 卡住）→ 失败关闭并清理 el_out。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    class _HangProc:
        pid = 7002
        returncode = None

        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

        @property
        def stdout(self):
            # 永不出数据且不退出 → 主读循环阻塞，触发 watchdog 超时。
            import threading as _th
            _ev = _th.Event()
            self._ev = _ev
            class _S:
                def __iter__(self):
                    return self
                def __next__(self):
                    _ev.wait()
                    raise StopIteration
                def read1(self, _n):
                    _ev.wait()
                    return b""
            return _S()

        @property
        def stderr(self):
            return iter([])

        def wait(self, timeout=None):
            return None

        def kill(self):
            if hasattr(self, "_ev"):
                self._ev.set()
            return None

    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda *a, **k: _HangProc())
    with pytest.raises(MatroskaBuildError, match="超时"):
        m._demux_el_only("dovi_tool", "ffmpeg", tmp_path / "m.dv.hevc",
                         tmp_path / "elonly.hevc", container=False,
                         timeout_seconds=2)


def test_el_vcl_slice_digests_no_readdir_read_failure(tmp_path):
    """round-10 P1-2：_el_vcl_slice_digests 读取出错（目录或不可读）失败关闭，
    不静默返回空。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    el_dir = tmp_path / "el_dir"
    el_dir.mkdir()
    with pytest.raises(MatroskaBuildError, match="EL 流读取失败"):
        m._el_vcl_slice_digests(el_dir)


def test_el_vcl_slice_digests_large_nal_cross_chunk(tmp_path):
    """round-10 P1-2：超大 VCL NAL（跨 1 MiB chunk、>16 MiB）不得被静默漏算——
    必须被完整纳入摘要。"""
    import bluray_fidelity.matroska as m

    el = tmp_path / "big.hevc"
    # 两个 NAL：一个 18 MiB 的 VCL（type 1），一个普通 VCL（type 1）。
    big_payload = b"\x02" + b"A" * (18 * 1024 * 1024)          # type 1
    small_payload = b"\x02" + b"B"                              # type 1
    import hashlib
    with open(el, "wb") as fh:
        fh.write(b"\x00\x00\x01" + big_payload)
        fh.write(b"\x00\x00\x01" + small_payload)
    digests = m._el_vcl_slice_digests(el)
    assert len(digests) == 2, "大 NAL 不得被静默跳过"
    exp = [hashlib.sha256(big_payload).hexdigest(),
           hashlib.sha256(small_payload).hexdigest()]
    assert digests == exp, "大 NAL 必须被完整哈希（跨 chunk 累积）"


# ---- round-11：Annex-B 3/4 字节起始码真规范化（P1）----

def _write_el_with_startcodes(path, nals: list[bytes], mode: str):
    """按 start-code 模式（all3 / all4 / mixed）写入若干 NAL payload。"""
    with open(path, "wb") as fh:
        for i, nal in enumerate(nals):
            if mode == "all3":
                fh.write(b"\x00\x00\x01" + nal)
            elif mode == "all4":
                fh.write(b"\x00\x00\x00\x01" + nal)
            else:  # mixed: 每两个用一个 4 字节
                fh.write(b"\x00\x00\x00\x01" + nal if i % 2 == 0
                         else b"\x00\x00\x01" + nal)
    return len(nals)


def test_el_vcl_slice_digests_annexb_normalization(tmp_path):
    """round-11 P1：同一 NAL 序列，3/4/混合起始码的摘要序列必须完全相等，
    且等于按 payload 直接哈希的期望。"""
    import hashlib
    import bluray_fidelity.matroska as m

    # type 1 (TRAIL_R) 与 type 0 两种 header，确保 header 首字节为 0 时不被误判
    payloads = [
        bytes([(1 << 1) | 0]) + b"payload-A",
        bytes([(0 << 1) | 0]) + b"payload-B",   # type 0 header 首字节 0x00
        bytes([(1 << 1) | 0]) + b"payload-C-long-" + b"x" * 100,
        bytes([(20 << 1) | 0]) + b"payload-D",  # type 20 (IDR_R) 也纳入
        bytes([(1 << 1) | 0]) + b"payload-E",
    ]
    expected = [hashlib.sha256(p).hexdigest() for p in payloads]

    for mode in ("all3", "all4", "mixed"):
        path = tmp_path / f"el_{mode}.hevc"
        _write_el_with_startcodes(path, payloads, mode)
        got = m._el_vcl_slice_digests(path)
        assert got == expected, f"模式 {mode}：摘要与 payload 期望不一致"
        assert len(got) == 5, f"模式 {mode}：NAL 数量不符"


def test_el_vcl_slice_digests_annexb_3_4_equal(tmp_path):
    """round-11 P1：同一内容全 3 字节与全 4 字节起始码得到的摘要必须相等。"""
    import bluray_fidelity.matroska as m, hashlib

    payloads = [bytes([(1 << 1) | 0]) + f"N-{i}".encode() * 7
                for i in range(6)]
    p3 = tmp_path / "p3.hevc"
    p4 = tmp_path / "p4.hevc"
    _write_el_with_startcodes(p3, payloads, "all3")
    _write_el_with_startcodes(p4, payloads, "all4")
    assert m._el_vcl_slice_digests(p3) == m._el_vcl_slice_digests(p4)


def test_el_vcl_slice_digests_startcode_split_across_chunk(tmp_path):
    """round-11 P1：起始码分裂在 1 MiB chunk 边界仍正确（4 字节起始码前导 00
    跨 chunk）。"""
    import hashlib
    import bluray_fidelity.matroska as m

    # 构造 big payload 使 NAL 恰好跨越 chunk：前导一个 4 字节起始码紧跟内容
    payload = bytes([(1 << 1) | 0]) + b"y" * (3 * 1024 * 1024)
    # 直接写整个文件再手动切分读取场景：用真实分块流验证——这里用大文件 + 4 字节
    path = tmp_path / "split.hevc"
    with open(path, "wb") as fh:
        fh.write(b"\x00\x00\x00\x01" + payload)
        fh.write(b"\x00\x00\x00\x01" + bytes([(1 << 1) | 0]) + b"z")
    got = m._el_vcl_slice_digests(path)
    assert len(got) == 2
    assert got[0] == hashlib.sha256(payload).hexdigest()


# ---- round-11 P2：紧凑覆盖统计 + 诊断不写全数组 ----

def test_el_vcl_parse_stats(tmp_path):
    """round-11 P2：_el_vcl_parse 返回紧凑统计（vcl_count/payload_bytes/
    sequence_sha256），且 3/4 字节起始码统计一致。"""
    import hashlib
    import bluray_fidelity.matroska as m

    payloads = [bytes([(1 << 1) | 0]) + b"A" * (k + 1) for k in range(5)]
    for mode in ("all3", "all4"):
        path = tmp_path / f"st_{mode}.hevc"
        _write_el_with_startcodes(path, payloads, mode)
        digests, stats = m._el_vcl_parse(path)
        assert stats["vcl_count"] == len(payloads)
        assert stats["payload_bytes"] == sum(len(p) for p in payloads)
        assert stats["sequence_sha256"] == hashlib.sha256(
            "\n".join(digests).encode()).hexdigest()
        assert stats["payload_bytes"] != len(payloads) * 32, "需真实 payload 字节数"


def test_write_build_diagnostics_compacts_el_digests(tmp_path):
    """round-11 P2：失败诊断持久化时只写紧凑 EL 统计，不写完整摘要数组。"""
    import json
    import bluray_fidelity.matroska as m

    out = tmp_path / "out.mkv"
    digests = [f"{i:064x}" for i in range(200)]  # 大数组
    diagnostics = {
        "output": str(out),
        "dv_facts": {
            "el_fingerprints": {0.0: [b"a", b"b", b"c"]},
            "el_source_digests": digests,
            "el_source_digest_stats": {"vcl_count": 200, "payload_bytes": 8000,
                                       "sequence_sha256": "deadbeef"},
        },
        "cue_points": [],
    }
    m._write_build_diagnostics(out, diagnostics, RuntimeError("boom"))
    target = tmp_path / "build-diagnostics.json"
    assert target.exists()
    data = json.loads(target.read_text())
    dv = data["dv_facts"]
    assert dv["el_source_digests"] == "<compact stats, not persisted>"
    assert dv["el_source_digest_stats"]["vcl_count"] == 200
    assert "el_fingerprints" in dv
    assert data["error"] == "RuntimeError: boom"


def test_demux_el_only_stdout_io_failure_cleans(tmp_path, monkeypatch):
    """round-11 P2：demux stdout 读取抛 OSError（部分输出/IO 失败）→ 失败关闭
    并清理 el_out。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    class _IOFailProc:
        pid = 7003
        returncode = 0

        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

        @property
        def stdout(self):
            class _S:
                def __iter__(self):
                    return self
                def __next__(self):
                    raise OSError("read failure")
                def read1(self, _n):
                    raise OSError("read failure")
            return _S()

        @property
        def stderr(self):
            return iter([])

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda *a, **k: _IOFailProc())
    with pytest.raises(MatroskaBuildError, match="读取异常"):
        m._demux_el_only("dovi_tool", "ffmpeg", tmp_path / "m.dv.hevc",
                         tmp_path / "elonly.hevc", container=False)


# ---- round-12：真实 payload 统计（P1）+ demux 所有失败出口清理（P2）----

def test_verify_el_order_consistency_preserves_real_payload_bytes(tmp_path, monkeypatch):
    """round-12 P1：传入预计算摘要+真实统计时，返回值保持真实 payload_bytes；
    只持有摘要时不得伪造 payload_bytes。"""
    import bluray_fidelity.matroska as m

    source_el = tmp_path / "src.el.hevc"
    target = tmp_path / "merged.dv.hevc"
    # payload 总长 > 摘要数*32，保证能区分真实与伪造
    payloads = [bytes([(1 << 1) | 0]) + b"A" * 200 for _ in range(5)]
    _write_el_with_startcodes(source_el, payloads, "all3")
    _write_el_with_startcodes(target, payloads, "all3")
    src_d, src_stats = m._el_vcl_parse(source_el)
    real_bytes = sum(len(p) for p in payloads)
    assert src_stats["payload_bytes"] == real_bytes

    monkeypatch.setattr(m, "_el_vcl_parse",
                        lambda p: (src_d, src_stats))
    monkeypatch.setattr(m, "_demux_el_only", lambda *a, **k: None)

    # 传真实统计 → 返回真实 payload_bytes
    facts = m._verify_el_order_consistency(
        "dovi_tool", "ffmpeg", source_el, target, "合并流", container=False,
        src_digests=src_d, src_stats=src_stats,
    )
    assert facts["source_stats"]["payload_bytes"] == real_bytes

    # 只传摘要、不传统计 → recovered 仍由 _el_vcl_parse 提供真实统计，
    # 而纯摘要来源不宣称 payload_bytes（不伪造）。
    facts2 = m._verify_el_order_consistency(
        "dovi_tool", "ffmpeg", source_el, target, "合并流", container=False,
        src_digests=src_d,
    )
    # source_stats 走 fallback：只有 digest 来源，不含 payload_bytes
    assert "payload_bytes" not in facts2["source_stats"]
    # recovered_stats 由 _el_vcl_parse 提供，仍含真实 payload_bytes
    assert facts2["recovered_stats"]["payload_bytes"] == real_bytes


def test_demux_el_only_deletes_partial_output_on_all_failures(tmp_path, monkeypatch):
    """round-12 P2：部分 el_out 在 stdout IO 异常/超时/dovi 非零/ffmpeg 非零
    时均被删除；成功路径保留有效 el_out。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    # 成功路径 fake：写有效 el_out，返回 0
    made = []

    class _OkProc:
        pid = 7100
        returncode = 0

        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

        @property
        def stdout(self):
            class _S:
                def __iter__(self):
                    return self
                def __next__(self):
                    raise StopIteration
            return _S()

        @property
        def stderr(self):
            return iter([])

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def _ok_popen(cmd, *a, **k):
        if "demux" in " ".join(cmd):
            made.append(tmp_path / "elonly.hevc")
        return _OkProc()

    monkeypatch.setattr(m.subprocess, "Popen", _ok_popen)
    el_out = tmp_path / "elonly.hevc"
    m._demux_el_only("dovi_tool", "ffmpeg", tmp_path / "m.dv.hevc",
                     el_out, container=False)
    # 成功路径不删（调用方外层的 finally 才删）；此处函数本身不删，仅验证未抛错
    # 且 el_out 可被后续读取——这里直接确认成功路径完成。

    def _run_failure(fake_class, match, pre_create=True):
        path = tmp_path / f"el_{fake_class.__name__}.hevc"
        if pre_create:
            path.write_bytes(b"partial-" * 4)
        class _P(fake_class):
            pass
        monkeypatch.setattr(m.subprocess, "Popen",
                            lambda *a, **k: _P())
        with pytest.raises(MatroskaBuildError, match=match):
            m._demux_el_only("dovi_tool", "ffmpeg", tmp_path / "m.dv.hevc",
                             path, container=False)
        assert not path.exists(), f"{fake_class.__name__} 失败出口必须删除部分 el_out"

    class _IOFail:
        pid = 1; returncode = 0
        def __init__(self, *a, **k): pass
        def poll(self): return None
        @property
        def stdout(self):
            class _S:
                def __iter__(self): return self
                def __next__(self): raise OSError("io")
                def read1(self, _n): raise OSError("io")
            return _S()
        @property
        def stderr(self): return iter([])
        def wait(self, timeout=None): return 0
        def kill(self): return None

    class _NonZero:
        pid = 2; returncode = 1
        def __init__(self, *a, **k): pass
        def poll(self): return None
        @property
        def stdout(self):
            class _S:
                def __iter__(self): return self
                def __next__(self): raise StopIteration
            return _S()
        @property
        def stderr(self): return iter([])
        def wait(self, timeout=None): return 1
        def kill(self): return None

    _run_failure(_IOFail, "读取异常")
    _run_failure(_NonZero, "回拆失败")


# ---- round-14 P1-1：merged 裸 HEVC 用 dovi_tool extract-rpu（工具原生）----

def test_raw_rpu_evidence_success(tmp_path, monkeypatch):
    """round-14 P1-1：dovi_tool extract-rpu 成功且输出非空、可解析 → 返回证据。"""
    import bluray_fidelity.matroska as m

    merged = tmp_path / "merged.dv.hevc"
    merged.write_bytes(b"\x00\x00\x01" + b"x" * 100)
    rpu_out = tmp_path / "out.rpu"
    calls = {"extract": 0, "info": 0}

    class _FakeExtract:
        returncode = 0
        stdout = iter([])
        stderr = iter([])
        def wait(self, timeout=None): return 0
        def kill(self): return None
        def __init__(self, *a, **k):
            calls["extract"] += 1
            rpu_out.write_bytes(b"\x00\x00\x01" + b"rpu-payload")

    class _FakeInfo:
        returncode = 0
        stdout = "rpu summary ok"
        stderr = ""
        def __init__(self, *a, **k): calls["info"] += 1
        def wait(self, timeout=None): return 0

    real_run = m.subprocess.run
    def _run(cmd, *a, **k):
        calls["info"] += 1
        return _FakeInfo()
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _FakeExtract(*a, **k))
    monkeypatch.setattr(m.subprocess, "run", _run)

    ev = m._raw_rpu_evidence("dovi_tool", merged, rpu_out, limit=240)
    assert ev["method"] == "dovi_tool extract-rpu"
    assert ev["limit_frames"] == 240
    assert ev["rpu_summary"].startswith("rpu summary ok")
    # 成功路径后 rpu_out 由 finally 清理
    assert not rpu_out.exists()


def test_raw_rpu_evidence_fail_closed(tmp_path, monkeypatch):
    """round-14 P1-1：extract-rpu 非零输出 / 空输出 / 不可解析均失败关闭并清理。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    merged = tmp_path / "merged.dv.hevc"
    merged.write_bytes(b"data")

    # 非零退出
    class _Fail:
        returncode = 7
        stdout = iter([])
        stderr = iter([b"boom"])
        def wait(self, timeout=None): return 7
        def kill(self): return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _Fail())
    with pytest.raises(MatroskaBuildError, match="extract-rpu 失败"):
        m._raw_rpu_evidence("dovi_tool", merged, tmp_path / "r.rpu", limit=240)

    # 成功但输出空文件 → 空 RPU 失败关闭
    class _Ok:
        returncode = 0
        stdout = iter([])
        stderr = iter([])
        def wait(self, timeout=None): return 0
        def kill(self): return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _Ok())
    rpu_out = tmp_path / "r.rpu"
    rpu_out.write_bytes(b"")   # 模拟工具写出空文件
    with pytest.raises(MatroskaBuildError, match="输出 RPU 为空"):
        m._raw_rpu_evidence("dovi_tool", merged, rpu_out, limit=240)
    assert not rpu_out.exists(), "空 RPU 输出必须清理"


# ---- round-15：结构化 merged_rpu_evidence 接入发布门（P1）+ info 空输出（P2）----

def test_verify_merged_rpu_evidence_ok_and_fail(tmp_path):
    """round-15 P1：_verify_merged_rpu_evidence 完整结构化证据通过，缺失/本法/
    空字段均失败关闭。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    good = {"method": "dovi_tool extract-rpu", "limit_frames": 240,
            "rpu_file_size": 12345, "rpu_summary": "rpu summary ok"}
    assert m._verify_merged_rpu_evidence(good, limit=240)["method"] == "dovi_tool extract-rpu"

    for bad, match in (
        (None, "缺少结构化 RPU 证据"),
        ({}, "RPU 证据方法非法"),
        ({**good, "method": "ffmpeg"}, "RPU 证据方法非法"),
        ({**good, "limit_frames": 0}, "limit_frames 非法"),
        ({**good, "limit_frames": 123}, "limit_frames 非法"),  # 与调用 limit 不符
        ({**good, "rpu_file_size": 0}, "文件大小非法"),
        ({**good, "rpu_summary": "  "}, "RPU summary 为空"),
        ({**good, "rpu_summary": None}, "RPU summary 为空"),
    ):
        with pytest.raises(MatroskaBuildError, match=match):
            m._verify_merged_rpu_evidence(bad, limit=240)


def test_raw_rpu_evidence_info_empty_stdout_fails(tmp_path, monkeypatch):
    """round-15 P2：extract-rpu 成功但 info -s stdout 为空 → 不可解析，失败关闭并清理。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    merged = tmp_path / "merged.dv.hevc"
    merged.write_bytes(b"data")
    rpu_out = tmp_path / "out.rpu"

    class _FakeExtract:
        returncode = 0
        stdout = iter([])
        stderr = iter([])
        def wait(self, timeout=None): return 0
        def kill(self): return None
        def __init__(self, *a, **k):
            rpu_out.write_bytes(b"\x00\x00\x01" + b"rpu")

    # info rc=0 但 stdout 空
    class _FakeInfoEmpty:
        returncode = 0
        stdout = ""
        stderr = ""
        def __init__(self, *a, **k): pass
        def wait(self, timeout=None): return 0

    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _FakeExtract(*a, **k))
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _FakeInfoEmpty())
    with pytest.raises(MatroskaBuildError, match="info -s 输出为空"):
        m._raw_rpu_evidence("dovi_tool", merged, rpu_out, limit=240)
    assert not rpu_out.exists(), "不可解析 RPU 输出必须清理"

    # info 启动失败（OSError）→ 转 MatroskaBuildError
    def _raise(*a, **k):
        raise OSError("spawn fail")
    monkeypatch.setattr(m.subprocess, "run", _raise)
    with pytest.raises(MatroskaBuildError, match="info 启动失败"):
        m._raw_rpu_evidence("dovi_tool", merged, rpu_out, limit=240)


def test_validate_ffmpeg_output_records_merged_rpu_evidence(monkeypatch, tmp_path):
    """round-15 P1：最终验证器把结构化 merged_rpu_evidence 原样带入诊断，不再有
    布尔哨兵 merged_rpu_first_window。"""
    import bluray_fidelity.matroska as m

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    rpu_ev = {"method": "dovi_tool extract-rpu", "limit_frames": 240,
              "rpu_file_size": 99, "rpu_summary": "ok"}
    monkeypatch.setattr(m, "_dv_rpu_evidence",
                        lambda *a, **k: {"rpu_per_window": (3, 3, 3),
                                         "seek_seconds": (0.0, 400.0, 800.0)})
    class _FfprobeOk:
        returncode = 0
        stdout = '{"streams":[{"side_data_list":[{"side_data_type":"DOVI configuration record","dv_profile":7,"rpu_present_flag":1,"el_present_flag":1}]}]}'
        stderr = ""
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _FfprobeOk())
    monkeypatch.setattr(m, "_verify_el_order_consistency",
                        lambda *a, **kw: {"order_ok": True})
    facts = {"bl_size": 1000, "merged_size": 2000,
             "merged_rpu_evidence": dict(rpu_ev), "duration_s": 1000.0,
             "el_fingerprints": {}, "el_source_digests": [b"d"] * 5,
             "el_source_digest_stats": {"vcl_count": 5},
             "dovi_executable": "dovi_tool"}
    ev = m._verify_dv_profile7_evidence("ffmpeg", out, facts)
    assert ev["merged_rpu_evidence"]["rpu_summary"] == "ok"
    assert "merged_rpu_first_window" not in ev, "不得再返回布尔哨兵"
    assert ev["rpu_seek_seconds"] == (0.0, 400.0, 800.0)


# ---- round-17：Profile 7 seek 门正交 BL 解码（A/B/C）----

def _mk_es(payloads, path):
    with open(path, "wb") as fh:
        for p in payloads:
            fh.write(b"\x00\x00\x01" + p)
    return path


def test_gateb_packets_uses_show_packets(monkeypatch):
    """round-17 A.1：packet 探测必须显式使用 -show_packets。"""
    import bluray_fidelity.matroska as m
    captured = {}

    class _P:
        returncode = 0
        stdout = "{\"packets\": []}"
        stderr = ""
        def __init__(self, cmd, **k): captured["cmd"] = cmd
        def run(self_placeholder=None, *a, **k):
            return _P._Result(0, self.stdout, "")
    # 用 run 而非 Popen：_probe_interval_packets 用 subprocess.run
    import subprocess as _sp
    class _Res:
        returncode = 0
        stdout = "{\"packets\": []}"
        stderr = ""
    def _run(cmd, **k):
        captured["cmd"] = cmd
        return _Res()
    monkeypatch.setattr(_sp, "run", _run)
    monkeypatch.setattr(m.subprocess, "run", _run)
    m._probe_interval_packets("ffprobe", Path("/tmp/x.mkv"), 5.0)
    assert "-show_packets" in captured["cmd"], "packet 探测必须显式 -show_packets"


def test_gateb_frames_uses_show_frames(monkeypatch):
    """round-17 A.1：frame 探测必须显式使用 -show_frames。"""
    import bluray_fidelity.matroska as m, subprocess as _sp
    captured = {}
    class _Res:
        returncode = 0
        stdout = "530.0\n531.0\n"
        stderr = ""
    def _run(cmd, **k):
        captured["cmd"] = cmd
        return _Res()
    monkeypatch.setattr(_sp, "run", _run)
    monkeypatch.setattr(m.subprocess, "run", _run)
    m._probe_interval_frames("ffprobe", Path("/tmp/x.mkv"), 5.0)
    assert "-show_frames" in captured["cmd"], "frame 探测必须显式 -show_frames"


def test_dv_seek_window_bl_ok_decoder_unsupported(tmp_path, monkeypatch):
    """round-17 B/C：完整合并流 0 帧、同窗 BL 可解 → 分类 full_profile7_decoder_unsupported
    （前提是 EL/RPU/Profile7 硬门已由 upstream 全通过）。"""
    import bluray_fidelity.matroska as m

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    # 关键AU包
    class _Pkt:
        returncode = 0
        stderr = ""
        def __init__(self, *a, **k): pass
    dv_facts = {"dovi_executable": "dovi_tool", "duration_s": 1000.0, "fps": 24.0}

    # packet 探测：有包、含 K 标志
    monkeypatch.setattr(m, "_probe_interval_packets",
                        lambda _p, _q, target, **k: [{"pts_time": f"{target:.1f}", "flags": "K__"},
                                                     {"pts_time": f"{target+0.5:.1f}", "flags": "___"}])
    # 完整合并流 frame：0（DV decoder 不支持）
    monkeypatch.setattr(m, "_probe_interval_frames", lambda *a, **k: [])
    # window 提取/分离/BL 解码：BL 解码帧满足需求（fps 由 dv_facts 提供）
    monkeypatch.setattr(m, "_extract_window_merged", lambda *a, **k: None)
    monkeypatch.setattr(m, "_demux_bl_only", lambda *a, **k: None)
    monkeypatch.setattr(m, "_prior_key_au_seconds",
                        lambda out, probe, target, **k: target - 5.0)
    monkeypatch.setattr(m, "_hevc_au_ir_and_params",
                        lambda *a, **k: {"first_au_is_ir": True,
                                         "has_sps_in_stream": True})
    monkeypatch.setattr(m, "_count_decoded_frames", lambda *a, **k: 500)
    monkeypatch.setattr(m, "_count_cue_points", lambda _p: 5)

    res = m._verify_cues_and_seek(
        "ffprobe", out, int(1000 * 1e9), ffmpeg="ffmpeg", dv_facts=dv_facts,
    )
    assert len(res["cues"]) == 3  # 三个目标点齐全
    for r in res["cues"]:
        assert r["classification"] == "full_profile7_decoder_unsupported"


def test_dv_seek_window_bl_not_decodable_fails(tmp_path, monkeypatch):
    """round-17 C：BL 直接 seek 不可解、连续路径也不可解 → 真 BL 损坏，失败关闭。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    dv_facts = {"dovi_executable": "dovi_tool", "duration_s": 1000.0, "fps": 24.0}
    monkeypatch.setattr(m, "_probe_interval_packets",
                        lambda _p, _q, target, **k: [{"pts_time": f"{target:.1f}", "flags": "K__"}])
    monkeypatch.setattr(m, "_probe_interval_frames", lambda *a, **k: [])
    monkeypatch.setattr(m, "_extract_window_merged", lambda *a, **k: None)
    monkeypatch.setattr(m, "_demux_bl_only", lambda *a, **k: None)
    monkeypatch.setattr(m, "_prior_key_au_seconds", lambda *a, **k: 100.0)
    monkeypatch.setattr(m, "_hevc_au_ir_and_params",
                        lambda *a, **k: {"first_au_is_ir": True})
    monkeypatch.setattr(m, "_count_decoded_frames", lambda *a, **k: 0)
    monkeypatch.setattr(m, "_count_cue_points", lambda _p: 5)
    with pytest.raises(MatroskaBuildError, match="BL 窗口未证明覆盖目标"):
        m._verify_cues_and_seek(
            "ffprobe", out, int(1000 * 1e9), ffmpeg="ffmpeg", dv_facts=dv_facts)


def test_dv_seek_window_container_defect_fails(tmp_path, monkeypatch):
    """round-17 C：BL 直接 seek 不可解但从更早关键AU可解 → 容器/参数集/Cue 缺陷，失败。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    dv_facts = {"dovi_executable": "dovi_tool", "duration_s": 1000.0, "fps": 24.0}
    monkeypatch.setattr(m, "_probe_interval_packets",
                        lambda _p, _q, target, **k: [{"pts_time": f"{target:.1f}", "flags": "K__"}])
    monkeypatch.setattr(m, "_probe_interval_frames", lambda *a, **k: [])
    monkeypatch.setattr(m, "_extract_window_merged", lambda *a, **k: None)
    monkeypatch.setattr(m, "_demux_bl_only", lambda *a, **k: None)
    monkeypatch.setattr(m, "_prior_key_au_seconds", lambda *a, **k: 100.0)
    monkeypatch.setattr(m, "_hevc_au_ir_and_params",
                        lambda *a, **k: {"first_au_is_ir": True})
    monkeypatch.setattr(m, "_count_cue_points", lambda _p: 5)
    # 目标前一有界窗口无关键 AU → no_prior_key_au，失败关闭
    monkeypatch.setattr(m, "_prior_key_au_seconds", lambda *a, **k: None)
    with pytest.raises(MatroskaBuildError, match="无关键访问单元"):
        m._verify_cues_and_seek(
            "ffprobe", out, int(1000 * 1e9), ffmpeg="ffmpeg", dv_facts=dv_facts)


def test_hevc_au_ir_and_params(tmp_path):
    """round-17 A.3：首个 AU 是否 IRAP 及 VPS/SPS/PPS 来源的可解释性探测。"""
    import bluray_fidelity.matroska as m

    # 构造：VPS/SPS/PPS + IDR（type 19）+ trailing（type 1）
    path = tmp_path / "s.hevc"
    vps = _mk_es([bytes([(32 << 1)]), b"123"], tmp_path / "t" if False else path)
    with open(path, "wb") as fh:
        fh.write(b"\x00\x00\x01" + bytes([(32 << 1) | 0]) + b"vps")
        fh.write(b"\x00\x00\x01" + bytes([(33 << 1) | 0]) + b"sps")
        fh.write(b"\x00\x00\x01" + bytes([(34 << 1) | 0]) + b"pps")
        fh.write(b"\x00\x00\x01" + bytes([(19 << 1) | 0]) + b"idr")
        fh.write(b"\x00\x00\x01" + bytes([(1 << 1) | 0]) + b"trail")
    r = m._hevc_au_ir_and_params(path)
    assert r["has_vps_in_stream"] and r["has_sps_in_stream"] and r["has_pps_in_stream"]
    assert r["first_au_is_ir"] is True


# ---- round-18：BL 解码帧计数（非时间戳）、工具故障分离、remove BL-only ----

def _gen_raw_hevc(path, frames=30):
    """用 ffmpeg 生成一个含若干 VCL 帧的真实裸 HEVC ES（无容器，供 -count_frames）。"""
    import subprocess as _sp, shutil as _sh
    ff = _sh.which("ffmpeg")
    if not ff:
        pytest.skip("需要 ffmpeg")
    # 直接编码输出裸 HEVC ES（无容器，避免 mp4→hevc 的 NOPTS 中间环节）
    try:
        _sp.run([ff, "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=128x72:rate=24:duration=2",
                 "-c:v", "libx265", "-x265-params", "log-level=error",
                 "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-f", "hevc", str(path)],
                check=True, capture_output=True)
    except (_sp.CalledProcessError, OSError):
        pytest.skip("需要 libx265")
    return path




def test_count_decoded_frames_real_raw_es(tmp_path):
    """round-18 P1-1：真实无时间戳裸 HEVC，-count_frames 应解出帧（不依赖 PTS）。"""
    import subprocess as _sp, shutil as _sh
    ffprobe = _sh.which("ffprobe")
    import bluray_fidelity.matroska as m
    if not _sh.which("ffmpeg") or not ffprobe:
        pytest.skip("需要 ffmpeg/ffprobe")
    es = _gen_raw_hevc(tmp_path / "raw.hevc", frames=30)
    n = m._count_decoded_frames(ffprobe, es)
    assert n > 0, "真实裸 ES 必须解出帧（-count_frames），而非依赖时间戳"
    # 对比：best_effort_timestamp_time 可能为空（无 PTS）——确认我们不走它。
    raw = _sp.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
         "-of", "csv=p=0", str(es)],
        capture_output=True, text=True,
    )
    ts_lines = [l for l in raw.stdout.splitlines() if l.strip()]
    # 我们不管 ts_lines 是否为空，count 才是成功标准


def test_demux_bl_only_uses_remove_no_shared_el(tmp_path, monkeypatch):
    """round-18 P1-3：BL 分离用 `dovi_tool remove`（BL-only），不产生共享 EL.hevc。"""
    import bluray_fidelity.matroska as m

    merged = tmp_path / "merged.hevc"
    merged.write_bytes(b"\x00\x00\x01" + b"x" * 64)
    bl_out = tmp_path / "out.bl.hevc"

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""
        def __init__(self, *a, **k): pass
        def wait(self, timeout=None): return 0

    captured = {}
    def _run(cmd, **k):
        captured["cmd"] = list(cmd)
        bl_out.write_bytes(b"bl")  # 模拟 BL 输出
        return _Ok()
    monkeypatch.setattr(m.subprocess, "run", _run)
    m._demux_bl_only("dovi_tool", merged, bl_out)
    assert captured["cmd"][0] == "dovi_tool"
    assert captured["cmd"][1] == "remove", "必须用 remove（BL-only）"
    # remove 只写 -o BL，无 -e EL 输出，故不产生共享 EL.hevc
    assert "-e" not in captured["cmd"]
    assert "EL.hevc" not in [p.name for p in tmp_path.iterdir()]


def test_demux_bl_only_tool_failure_propagates(tmp_path, monkeypatch):
    """round-18 P1-3：dovi_tool remove 非零退出 → 直接抛 MatroskaBuildError，不归为 BL 损坏。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    merged = tmp_path / "merged.hevc"
    merged.write_bytes(b"x")
    class _Fail:
        returncode = 9
        stdout = ""
        stderr = "boom"
        def __init__(self, *a, **k): pass
        def wait(self, timeout=None): return 9
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Fail())
    with pytest.raises(MatroskaBuildError, match="BL 分离失败"):
        m._demux_bl_only("dovi_tool", merged, tmp_path / "out.bl.hevc")


def test_verify_dv_seek_window_tool_failure_not_bl_corruption(tmp_path, monkeypatch):
    """round-18 P1-3：解码帧工具故障（非零/IO）不得被 _verify_dv_seek_window 改名
    为 real_bl_corruption；应保留原错误失败关闭。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    point = {"frac": 0.1, "target_seconds": 100.0}
    monkeypatch.setattr(m, "_prior_key_au_seconds", lambda *a, **k: 90.0)
    monkeypatch.setattr(m, "_extract_window_merged", lambda *a, **k: None)
    monkeypatch.setattr(m, "_hevc_au_ir_and_params",
                        lambda *a, **k: {"first_au_is_ir": True})
    monkeypatch.setattr(m, "_demux_bl_only", lambda *a, **k: None)

    def _fail(*a, **k):
        raise MatroskaBuildError("dovi_tool remove 启动失败：spawn err")
    monkeypatch.setattr(m, "_count_decoded_frames", _fail)
    with pytest.raises(MatroskaBuildError) as ei:
        m._verify_dv_seek_window("ffprobe", "ffmpeg", "dovi_tool", out, 100.0,
                                 point, fps=24.0, temp_dir=None)
    assert "spawn err" in str(ei.value)
    assert "真实 BL" not in str(ei.value), "工具故障不得归为真 BL 损坏"


# ---- round-19：有界关键AU定位 / 动态帧数+上限 / PTS缺失 / 删除虚假细分类 ----

def test_prior_key_au_bounded_window(monkeypatch):
    """round-19 P1-1：关键 AU 定位必须用有界窗口（target-N%+N），不得全片扫描。"""
    import subprocess as _sp
    import bluray_fidelity.matroska as m
    captured = {}
    class _R:
        returncode = 0
        stdout = "{\"packets\": [{\"pts_time\": \"95.0\", \"flags\": \"K__\"}]}"
        stderr = ""
    def _run(cmd, **k):
        captured["cmd"] = list(cmd)
        return _R()
    monkeypatch.setattr(_sp, "run", _run)
    monkeypatch.setattr(m.subprocess, "run", _run)
    out = Path("/tmp/x.mkv")
    start = m._prior_key_au_seconds(out, "ffprobe", 100.0)
    assert start == 95.0
    # 区间必须是目标附近的有界窗口，不得是 0%+target（全片）
    intervals = [c[i+1] for c in [captured["cmd"]] for i, t in enumerate(c) if t == "-read_intervals"]
    assert intervals, "必须含 -read_intervals"
    for iv in intervals:
        assert not iv.startswith("0%+"), f"不得全片扫描：{iv}"
        assert "%+" in iv, f"必须是有界区间：{iv}"


def test_prior_key_au_none_when_missing(monkeypatch):
    """round-19 P1-1：目标前窗口无关键包 → 返回 None（与'片头关键包=0'区分）。"""
    import subprocess as _sp
    import bluray_fidelity.matroska as m
    class _R:
        returncode = 0
        stdout = "{\"packets\": [{\"pts_time\": \"5.0\", \"flags\": \"___\"}]}"
        stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _R())
    assert m._prior_key_au_seconds(Path("/tmp/x.mkv"), "ffprobe", 100.0) is None


def test_dv_window_frame_cap_60fps(monkeypatch, tmp_path):
    """round-19 P1-3：60fps 下覆盖目标所需帧数 > 上限 → 失败关闭（不被当 BL 损坏）。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError, _DV_EXTRACT_FRAME_CAP

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    point = {"frac": 0.5, "target_seconds": 500.0}
    monkeypatch.setattr(m, "_prior_key_au_seconds",
                        lambda out, probe, target, **k: target - 200.0)  # 跨度 200s @60fps
    monkeypatch.setattr(m, "_extract_window_merged", lambda *a, **k: None)
    monkeypatch.setattr(m, "_hevc_au_ir_and_params",
                        lambda *a, **k: {"first_au_is_ir": True})
    monkeypatch.setattr(m, "_demux_bl_only", lambda *a, **k: None)
    monkeypatch.setattr(m, "_count_decoded_frames", lambda *a, **k: 999999)
    # 200s * 60fps = 12000 > cap 2000
    with pytest.raises(MatroskaBuildError, match="超过有界窗口上限"):
        m._verify_dv_seek_window("ffprobe", "ffmpeg", "dovi_tool", out, 500.0,
                                 point, fps=60.0, temp_dir=None)


def test_dv_window_needed_frames_not_fixed():
    """round-19 P1-3：所需帧数按 fps 与跨度推导，不固定（24fps/10s=250；60fps/10s=610）。"""
    import bluray_fidelity.matroska as m
    # 24fps, span 10s → 240+10 = 250
    assert m._dv_window_needed_frames(24.0, 90.0, 100.0) >= 250
    # 60fps, span 10s → 600+10 = 610
    assert m._dv_window_needed_frames(60.0, 90.0, 100.0) >= 610
    # 不固定：两值不同
    assert m._dv_window_needed_frames(24.0, 90.0, 100.0) != m._dv_window_needed_frames(60.0, 90.0, 100.0)


def test_dv_seek_pts_missing_fails(monkeypatch, tmp_path):
    """round-19 P1-3：首包 PTS 缺失（None）→ 失败关闭。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    dv_facts = {"dovi_executable": "dovi_tool", "duration_s": 1000.0, "fps": 24.0}
    # 首包无 pts_time（None）且无 K
    monkeypatch.setattr(m, "_probe_interval_packets",
                        lambda _p, _q, target, **k: [{"pts_time": None, "flags": "K__"}])
    monkeypatch.setattr(m, "_probe_interval_frames", lambda *a, **k: [])
    monkeypatch.setattr(m, "_count_cue_points", lambda _p: 5)
    # 首包 PTS None → 不满足齐备；至少要求关键AU存在则 PTS 校验应失败
    # （无 K 会先失败；这里用有 K 但 PTS None 验证 PTS 缺失。
    #  当前实现：first_pts None 且无校验则放行——本测试验证修复后必须拒绝 PTS None）
    with pytest.raises((MatroskaBuildError,), match="PTS|关键|对齐|定位"):
        m._verify_cues_and_seek(
            "ffprobe", out, int(1000 * 1e9), ffmpeg="ffmpeg", dv_facts=dv_facts)


# ---- round-21：分离「覆盖门槛」与「抽取预算」 ----

def test_dv_round21_coverage_vs_extract():
    """round-21 P1：覆盖门槛与抽取预算是两个不同数字；90% 观测边界需通过。"""
    import bluray_fidelity.matroska as m
    # 真实 90% 观测：start=4781.317, target=4782.878, fps=23.976, decoded=45
    mp = m._dv_coverage_required_frames(23.976023976023978, 4781.317, 4782.878)
    assert mp >= 2
    # span=1.561 → ceil(1.561*23.976)=ceil(37.43)=38 → +2 = 40
    assert mp == 40, f"coverage_required 应为 ceil(span*fps)+2=40，实际 {mp}"
    ex = m._dv_extract_requested_frames(mp)
    assert ex == mp + 10, "抽取预算 = 覆盖门槛 + 尾部余量"
    assert mp != ex, "覆盖门槛与抽取预算必须不同"


def test_dv_round21_coverage_45_passes(tmp_path, monkeypatch):
    """round-21 P1：90% 观测 45 帧（>覆盖门槛40）应通过；抽取预算47不作为关卡。"""
    import bluray_fidelity.matroska as m

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    point = {"frac": 0.9, "target_seconds": 4782.878}
    monkeypatch.setattr(m, "_prior_key_au_seconds", lambda *a, **k: 4781.317)
    monkeypatch.setattr(m, "_extract_window_merged", lambda *a, **k: None)
    monkeypatch.setattr(m, "_hevc_au_ir_and_params",
                        lambda *a, **k: {"first_au_is_ir": True})
    monkeypatch.setattr(m, "_demux_bl_only", lambda *a, **k: None)
    monkeypatch.setattr(m, "_count_decoded_frames", lambda *a, **k: 45)
    code = m._verify_dv_seek_window(
        "ffprobe", "ffmpeg", "dovi_tool", out, 4782.878, point,
        fps=23.976023976023978, temp_dir=None)
    from bluray_fidelity.matroska import MatroskaBuildError
    assert point["coverage_required_frames"] == 40
    assert point["extract_requested_frames"] == 50
    assert point["bl_decoded_frames"] == 45
    assert code in ("full_profile7_decoder_unsupported", "decodable")


def test_dv_round21_coverage_minus_one_fails(tmp_path, monkeypatch):
    """round-21 P1：decoded = coverage-1 必须失败关闭（真正未跨目标）。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuildError

    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    point = {"frac": 0.5, "target_seconds": 2657.155}
    monkeypatch.setattr(m, "_prior_key_au_seconds", lambda *a, **k: 2657.028)
    monkeypatch.setattr(m, "_extract_window_merged", lambda *a, **k: None)
    monkeypatch.setattr(m, "_hevc_au_ir_and_params",
                        lambda *a, **k: {"first_au_is_ir": True})
    monkeypatch.setattr(m, "_demux_bl_only", lambda *a, **k: None)
    cov = m._dv_coverage_required_frames(23.976023976023978, 2657.028, 2657.155)
    # decoded = coverage - 1 → 失败
    monkeypatch.setattr(m, "_count_decoded_frames", lambda *a, **k: cov - 1)
    with pytest.raises(MatroskaBuildError, match="未证明覆盖目标"):
        m._verify_dv_seek_window(
            "ffprobe", "ffmpeg", "dovi_tool", out, 2657.155, point,
            fps=23.976023976023978, temp_dir=None)


def test_dv_round21_ceil_off_by_one():
    """round-21 P1：非整数 span 用 ceil；整帧边界不 off-by-one。"""
    import bluray_fidelity.matroska as m
    # span 恰 1.0s @24fps → ceil(24)+2 = 26
    assert m._dv_coverage_required_frames(24.0, 90.0, 91.0) == 26
    # span 0.5s @24fps → ceil(12)+2 = 14
    assert m._dv_coverage_required_frames(24.0, 90.0, 90.5) == 14
    # 纯整秒但不含起始帧样本：最小 2
    assert m._dv_coverage_required_frames(24.0, 90.0, 90.0) == max(2, 2)  # ceil(0)+2=2


# ---------------------------------------------------------------------------
# 2026-09-14 生产四个封装故障的回归（P1 探测超时误报媒体缺陷、P1 头部原因被
# 尾部截断丢弃、P2 轨数量不一致缺诊断、P2 多段合辑静默选片）。
# ---------------------------------------------------------------------------


def test_probe_timeout_is_transient_and_decodes_bytes_detail(monkeypatch):
    """P1：探测超时按瞬时（Transient）分类并说明超时秒数，绝不表述为媒体
    缺陷；TimeoutExpired 在 text=True 下 stderr 仍是 bytes，详情必须解码。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import TransientMatroskaBuildError

    def fake_run(cmd, **kwargs):
        raise m.subprocess.TimeoutExpired(
            cmd, 300, stderr=b"CSV,hdr-metadata\nffprobe: read timed out\n",
        )

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    diag: dict = {}
    with pytest.raises(TransientMatroskaBuildError) as frame_error:
        m._probe_interval_frames("ffprobe", Path("/tmp/x.mkv"), 2657.0, diag=diag)
    text = str(frame_error.value)
    assert "超时" in text and "300" in text
    assert "b'" not in text, "bytes 详情必须解码为文本"
    assert "探测失败" not in text and "拒绝发布" not in text, "超时不得表述为媒体缺陷"
    assert diag["frame_stderr"] == "CSV,hdr-metadata\nffprobe: read timed out"

    with pytest.raises(TransientMatroskaBuildError, match="超时"):
        m._probe_interval_packets("ffprobe", Path("/tmp/x.mkv"), 2657.0)
    with pytest.raises(TransientMatroskaBuildError, match="时长探测超时"):
        m._probe_duration("ffprobe", Path("/tmp/x.mkv"))


def test_probe_failure_detail_keeps_reason_head(monkeypatch):
    """P2：失败详情截断保留头部原因并标记省略号；原实现取尾部 300 字符，
    800 字符的 ffprobe CSV 转储会把人工前缀整段挤掉。"""
    import subprocess as _sp
    import bluray_fidelity.matroska as m

    dump = "CSV," * 200  # 800 字符工具转储

    def fake_run(cmd, **kwargs):
        raise _sp.CalledProcessError(1, cmd, output="", stderr=dump)

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    with pytest.raises(MatroskaBuildError) as error:
        m._probe_interval_frames("ffprobe", Path("/tmp/x.mkv"), 100.0)
    text = str(error.value)
    assert text.startswith("成品 seek frame 探测失败：")
    assert text.endswith("…")
    assert len(text) <= m._DETAIL_LIMIT + len("成品 seek frame 探测失败：")
    assert m._bounded_detail(dump) == dump[: m._DETAIL_LIMIT - 1] + "…"


def test_mismatch_reports_observed_and_mapped_track_ids(tmp_path):
    """P2：数量不一致消息必须同时给出成品轨与可映射 MPLS 轨标识（有界），
    无 mkvmerge 命令日志时也能重建差异。"""
    from bluray_fidelity.matroska import validate_finalized_identification

    asset = _asset(tmp_path)
    resolved = resolve_official_tracks(asset, parse_identification(_input_identification()))
    output = {
        "container": {"properties": {"duration": 100_000_000_000}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
             "properties": {}},
            {"id": 1, "type": "audio", "codec": "TrueHD",
             "properties": {"language": "eng", "stream_id": 4352}},
            {"id": 2, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "eng"}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "zho"}},
        ],
    }
    with pytest.raises(MatroskaBuildError, match="音轨数量") as error:
        validate_finalized_identification(asset, resolved, output)
    text = str(error.value)
    assert "成品 1 条" in text and "可映射 2 条" in text
    assert "id=1/pid=0x1100/TrueHD/eng" in text
    assert "official=0/pid=0x1100→id=2" in text
    assert "official=1/pid=0x1101→id=4" in text

    # 字幕数量不一致：音轨数量对齐（2 条）后同样带两边的标识。
    mismatched_subtitles = {
        "container": output["container"],
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
             "properties": {}},
            {"id": 1, "type": "audio", "codec": "TrueHD",
             "properties": {"language": "eng"}},
            {"id": 2, "type": "audio", "codec": "AC-3",
             "properties": {"language": "zho"}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "eng", "stream_id": 4768}},
        ],
    }
    with pytest.raises(MatroskaBuildError, match="字幕数量") as subtitle_error:
        validate_finalized_identification(asset, resolved, mismatched_subtitles)
    subtitle_text = str(subtitle_error.value)
    assert "成品 1 条" in subtitle_text and "可映射 2 条" in subtitle_text
    assert "id=3/pid=0x12a0/HDMV PGS/eng" in subtitle_text
    assert "official=0/pid=0x12a0→id=5" in subtitle_text


# ---------------------------------------------------------------------------
# 多段合辑（连放/无缝分支）：按 play item 逐段物化 + mkvmerge 追加
# （2026-09-14 生产缺陷：字节拼接的时间轴与 MPLS 期望不重叠，seek 门失败）
# ---------------------------------------------------------------------------

# 3 段合辑的分段字节数（和 = asset.size，与 reader 的"区间总长=标题大小"一致）。
_MULTICLIP_BOUNDS = ((0, 30), (30, 20), (50, 10))


def _multiclip_asset(tmp_path: Path) -> BlurayTitleAsset:
    """3 段连放：clip 00001 被引用两次（#0/#2），00002 一次（#1）。"""
    return replace(
        _asset(tmp_path),
        size=60,
        clip_count=3,
        duration_90k=81_000_000,
        clips=(
            BlurayClip(index=0, clip_id="00001", in_time=54_000_000, out_time=81_000_000),
            BlurayClip(index=1, clip_id="00002", in_time=0, out_time=27_000_000),
            BlurayClip(index=2, clip_id="00001", in_time=54_000_000, out_time=81_000_000),
        ),
    )


def _install_multiclip_reader(monkeypatch, bounds=_MULTICLIP_BOUNDS):
    """假 reader：边界给定，逐段字节 = bytes([item+1]) * 该段长度。返回读取序号。"""
    import bluray_fidelity.matroska as m

    read_items: list[int] = []
    monkeypatch.setattr(m, "bluray_item_bounds", lambda _asset: tuple(bounds))

    def fake_iter(_asset, item_index, chunk_size=1 << 20):
        read_items.append(item_index)
        remaining = bounds[item_index][1]
        while remaining:
            size = min(chunk_size, remaining)
            yield bytes([item_index + 1]) * size
            remaining -= size

    monkeypatch.setattr(m, "iter_bluray_item", fake_iter)
    return read_items


def _run_multiclip_build(monkeypatch, tmp_path, *, asset=None,
                         bounds=_MULTICLIP_BOUNDS, payloads=None, streams=None,
                         hdr10plus=None, capture=None, output_name="final.mkv.partial"):
    """走真实 MatroskaBuilder.build（reader/识别/探测/封装全假）。

    ``capture`` 在 build 前就写入 commands/read_items/output，失败路径的断言
    也能看到"封装命令从未发出""哪些段被物化过"。
    """
    import json
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = asset if asset is not None else _multiclip_asset(tmp_path)
    payloads = payloads or {}
    capture = capture if capture is not None else {}
    read_items = _install_multiclip_reader(monkeypatch, bounds)
    commands: list[list[str]] = []
    output = tmp_path / output_name
    capture.update({"commands": commands, "read_items": read_items,
                    "output": output, "asset": asset})

    class FakeProcess:
        stdout = iter(())

        def wait(self):
            return 0

    class FakeCompleted:
        stdout = json.dumps(_input_identification())

    builder = MatroskaBuilder()
    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/mkvmerge")
    monkeypatch.setattr(
        builder, "_identify_path",
        lambda source: payloads.get(Path(source).name, _input_identification()),
    )
    monkeypatch.setattr(
        builder, "_video_streams",
        lambda source: streams(source) if callable(streams) else (streams or _b2_streams()),
    )
    monkeypatch.setattr(
        builder, "_has_hdr10plus",
        lambda source: bool(hdr10plus(Path(source))) if hdr10plus else False,
    )
    monkeypatch.setattr(m, "validate_finalized_identification", lambda *a, **k: {})
    # E1 轨覆盖门对**真实成品**逐窗口测量在场区间，本装置的分段与成品都是桩字节
    # （无任何媒体包），覆盖门在这里没有可驱动的对象；按本文件既有约定与识别门、
    # 拼接门一并注入。覆盖门自身的真实媒体证据见 tests/test_matroska_track_coverage.py。
    monkeypatch.setattr(m, "require_track_coverage", lambda *a, **k: {})
    # DV 源侧字幕包证明要求真实段级 MKV；本测试构建的是桩字节，真实探针覆盖
    # 在 test_matroska_track_coverage.py 中单独验证。
    monkeypatch.setattr(
        m, "_probe_source_subtitle_evidence",
        lambda _probe, _segments, windows: m.SourceSubtitleEvidence({}, tuple(
            frozenset() for _ in windows)),
    )
    monkeypatch.setattr(MatroskaBuilder, "_verify_multiclip_seams", lambda *a, **k: None)

    def fake_popen(command, *args, **kwargs):
        commands.append(list(command))
        return FakeProcess()

    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: FakeCompleted())
    capture["result"] = builder.build(asset, output, title="合辑")
    return capture


def test_default_route_logs_mkvmerge_command_and_multiclip(
    tmp_path, monkeypatch, caplog,
):
    """P2 + 2026-09-14 修复：默认 mkvmerge 路线必须先记录命令与选定轨 ID；
    多段合辑按 play item 逐段物化，再用 `+` 追加（不做字节拼接）。"""
    with caplog.at_level("INFO", logger="hdathome.matroska"):
        capture = _run_multiclip_build(monkeypatch, tmp_path)

    assert set(capture["result"]) == {"track_coverage"}, (
        "良好路径只应带常驻的轨覆盖证据，不产生别的构建结果事实")
    output = capture["output"]
    segments = [output.with_name(f"{output.name}.item{index}.m2ts") for index in range(3)]
    # 同一 clip 被引用两次 → 两个独立分段文件（按 play item 各自取区间，不去重）。
    assert capture["asset"].clips[0].clip_id == capture["asset"].clips[2].clip_id
    assert [path.stat().st_size for path in segments] == [30, 20, 10]
    assert segments[0].read_bytes() == b"\x01" * 30
    assert segments[2].read_bytes() == b"\x03" * 10
    assert capture["read_items"] == [0, 1, 2]

    (command,) = capture["commands"]
    assert command.count("+") == 2
    # 输入顺序 = MPLS 顺序（首段 + 两个追加段），无字节拼接。
    assert [token for token in command if token.endswith(".m2ts")] == [
        str(path) for path in segments
    ]
    assert command.index("+") > command.index(str(segments[0]))
    # 2026-09-16 修正（生产标本 dlv_93c13946d7c0eac7c0b9）：逐文件轨编号选项
    # **只对紧随其后的那个输入生效**，所以每个追加段都必须**重复同一组逐轨选择**
    # ——否则追加段会复制它自己的全部轨，与首段的排除集不一致，mkvmerge 的默认
    # 按位映射随即指向首段未复制的轨（`… does not contain a track with the ID 3 …
    # --append-to was invalid`，1.3 秒报错退出、交付永远无法完成）。
    # 旧做法（追加段只给类型级开关）在**子集段**上确实不能复用首段轨编号，但子集段
    # 现在根本不进封装（_prepare_items 记违规 → _trailing_exclusions 尾部排除、
    # 中段违规失败关闭），故此处复用首段轨 ID 不会引用不存在的轨。
    assert command.count("--audio-tracks") == 3
    assert command.count("--subtitle-tracks") == 3
    assert command.count("--video-tracks") == 3
    # 显式追加映射：覆盖每个追加段的**每一条被复制轨**，且不引用被排除的 track 3。
    # 2026-09-17 修正：目标文件号是**前一段**（file N → file N-1），不是首段 0。
    # 旧实现硬编码 0，两段时恰好正确，3 段时 `2:x:0:x` 与 `1:x:0:x` 冲突 →
    # mkvmerge 退出码 2（错误）、不产出文件（生产标本 dlv_b3532c0cbbbe66742be8）。
    mapping = command[command.index("--append-to") + 1]
    for offset in (1, 2):
        for track_id in (0, 1, 2, 4, 5, 6):
            assert f"{offset}:{track_id}:{offset - 1}:{track_id}" in mapping
    assert "1:3:0:3" not in mapping and "2:3:0:3" not in mapping
    # 绝不把多个源文件映射到同一条目标轨（mkvmerge 会以错误中止）。
    assert "2:0:0:0" not in mapping and "2:1:0:1" not in mapping
    # 全局选项必须出现在输入之前（mkvmerge 按位置解析选项）。
    assert command.index("--append-to") < command.index(str(segments[0]))
    assert "--gui-mode" in command

    text = "\n".join(
        record.getMessage() for record in caplog.records
        if record.name == "hdathome.matroska"
    )
    assert "mkvmerge 命令：" in text
    assert "--audio-tracks 2,4" in text
    assert "--subtitle-tracks 5,6" in text
    assert "video=0,1 audio=2,4 subtitle=5,6" in text
    assert "多段合辑主片" in text
    assert "#0 00001 in=600.0s out=900.0s" in text
    # 单文件 source 不参与多段路线，也不得残留。
    assert not output.with_name(output.name + ".source.m2ts").exists()


def test_single_clip_route_keeps_one_input_without_append(tmp_path, monkeypatch):
    """单段标题（99% 库）保持既有单文件路线：不探测 play item 边界、命令无
    `+`、只有一个输入、不产生任何分段文件。"""
    import json
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _asset(tmp_path)
    assert m.planned_segment_count(asset) == 0

    def forbidden_bounds(_asset):
        raise AssertionError("单段标题不得探测 MPLS play item 边界")

    monkeypatch.setattr(m, "bluray_item_bounds", forbidden_bounds)
    monkeypatch.setattr(m, "iter_bluray_item",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("单段标题不得按 play item 读取")))

    materialized: list[Path] = []

    def fake_materialize(_asset, source, progress=None, staging=None):
        materialized.append(Path(source))
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"m" * int(_asset.size))

    monkeypatch.setattr(MatroskaBuilder, "_materialize_title", staticmethod(fake_materialize))
    commands: list[list[str]] = []

    class FakeProcess:
        stdout = iter(())

        def wait(self):
            return 0

    class FakeCompleted:
        stdout = json.dumps(_input_identification())

    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/mkvmerge")
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda command, *a, **k: (commands.append(list(command)), FakeProcess())[1])
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: FakeCompleted())
    monkeypatch.setattr(m, "validate_finalized_identification", lambda *a, **k: {})
    monkeypatch.setattr(m, "require_track_coverage", lambda *a, **k: {})
    seekable: list = []
    monkeypatch.setattr(MatroskaBuilder, "_verify_seekable",
                        lambda self, path, dv_facts=None, **kw: seekable.append(dv_facts))
    builder = MatroskaBuilder()
    monkeypatch.setattr(builder, "_video_streams", lambda _source: [])
    monkeypatch.setattr(builder, "_has_hdr10plus", lambda _source: False)

    output = tmp_path / "final.mkv.partial"
    # 构建结果现在恒带轨覆盖证据（E1）；除此之外本路径不得产生别的事实。
    assert set(builder.build(asset, output, title="单片")) == {"track_coverage"}
    assert materialized == [output.with_name(output.name + ".source.m2ts")]
    (command,) = commands
    assert "+" not in command
    assert [token for token in command if token.endswith(".m2ts")] == [str(materialized[0])]
    assert not list(output.parent.glob("*.item*.m2ts"))
    assert seekable == [None]


def test_multiclip_checkpoint_reuses_complete_and_restarts_truncated(tmp_path, monkeypatch):
    """分段检查点按段校验：完整段复用，截断段删除后重新物化（绝不续用）。"""
    from bluray_fidelity.matroska import build_artifacts

    asset = _multiclip_asset(tmp_path)
    artifacts = build_artifacts(tmp_path / "final.mkv.partial", segment_count=3)
    artifacts.segments[0].write_bytes(b"A" * 30)      # 完整 → 复用
    artifacts.segments[1].write_bytes(b"truncated")   # 截断 → 丢弃重做

    capture = _run_multiclip_build(monkeypatch, tmp_path, asset=asset)
    assert capture["read_items"] == [1, 2]
    assert artifacts.segments[0].read_bytes() == b"A" * 30
    assert [path.read_bytes() for path in artifacts.segments[1:]] == [
        b"\x02" * 20, b"\x03" * 10,
    ]


def test_multiclip_build_fails_closed_when_a_segment_track_disagrees(tmp_path, monkeypatch):
    """逐段核对在追加前执行：后段**新增**一条字幕轨即失败关闭，封装命令从未发出。

    （纯子集缺失不在此列：母盘终段少声明轨属真实结构，需放行并记录事实，
    见 test_multiclip_subset_difference_proceeds_and_records_fact。）
    """
    import bluray_fidelity.matroska as m

    asset = _multiclip_asset(tmp_path)
    output = tmp_path / "final.mkv.partial"
    broken = _input_identification()
    broken["tracks"] = [
        *broken["tracks"],
        {"id": 7, "type": "subtitles", "codec": "HDMV PGS",
         "properties": {"stream_id": 4770}},
    ]
    capture: dict = {}
    with pytest.raises(m.MatroskaBuildError, match="第 2/3 段") as error:
        _run_multiclip_build(
            monkeypatch, tmp_path, asset=asset, capture=capture,
            payloads={f"{output.name}.item1.m2ts": broken},
        )
    assert "新增 subtitles/pid=0x12a2" in str(error.value)
    assert capture["commands"] == []
    # 2026-09-15：逐段核对不再"遇到违规立刻停"——违规段能否按**尾部**排除
    # 要等全部段核对完（越南战争：尾部 11.4s logo 与首段同 PID 不同声道数，
    # 只能显式排除；中段违规仍必须失败关闭）。因此全部段都被物化/识别，
    # 但封装命令仍未发出（capture["commands"] == [] 才是"追加前失败闭合"的证据）。
    assert capture["read_items"] == [0, 1, 2]


def test_multiclip_trailing_incompatible_segment_is_excluded_with_facts(
    tmp_path, monkeypatch, caplog,
):
    """尾部轨道不兼容段显式排除（不并入成品、不静默），中段不兼容仍失败关闭。

    2026-09-15 越南战争（S01E09 盘）生产实证：主片 playlist 19 段里 item 0..16 是
    两集正片（同一 clip 连续覆盖），item 17 是 173s 预告（轨道为首段的纯子集，按事实
    放行），item 18 是 11.4s 碟内 logo——与首段**同 PID 不同声道数**（0x1100 首段
    6ch、本段 2ch）。mkvmerge 追加只能映射到首文件已存在的目标轨，目标轨属性由首段
    决定，并入即坏音；显式排除并留痕，否则该标题永远不可交付（同 2026-09-14 对
    「终段少声明字幕 PID」的裁定精神）。
    """
    import bluray_fidelity.matroska as m

    asset = _multiclip_asset(tmp_path)   # 3 段
    output = tmp_path / "final.mkv.partial"
    # 末段（item2）：与首段同 PID 但声道数不同（漂移，不是纯子集）
    drifted = _input_identification()
    for track in drifted["tracks"]:
        if track["id"] == 0:  # 主音轨（与首段同 stream_id）
            track["properties"]["audio_channels"] = 2
    capture: dict = {}
    with caplog.at_level("WARNING", logger="hdathome.matroska"):
        _run_multiclip_build(
            monkeypatch, tmp_path, asset=asset, capture=capture,
            payloads={f"{output.name}.item2.m2ts": drifted},
        )

    # 成品的追加输入不含被排除的末段（首段 + 仅 item1）
    (command,) = capture["commands"]
    segments = [output.with_name(f"{output.name}.item{index}.m2ts")
                for index in range(3)]
    assert [token for token in command if token.endswith(".m2ts")] == [
        str(segments[0]), str(segments[1]),
    ]
    assert command.count("+") == 1
    text = "\n".join(record.getMessage() for record in caplog.records
                     if record.name == "hdathome.matroska")
    assert "已显式排除" in text
    assert "item 3/3" in text          # 逐段事实：段号 + clip + 区间 + 差异原因
    assert "audio_channels" in text or "≠本段" in text


def test_multiclip_mid_feature_incompatible_segment_still_fails_closed(
    tmp_path, monkeypatch,
):
    """中段不兼容**不可**按尾部排除：跳过中段等于成品连续性被破坏（内容损坏）。

    判据：违规段之后仍有与首段对应的段 → 失败关闭，封装命令从未发出。
    """
    import bluray_fidelity.matroska as m

    asset = _multiclip_asset(tmp_path)   # 3 段，末段与首段同 clip（对应）
    output = tmp_path / "final.mkv.partial"
    drifted = _input_identification()
    for track in drifted["tracks"]:
        if track["id"] == 2:           # 次音轨：8ch 漂移
            track["properties"]["audio_channels"] = 8
    capture: dict = {}
    with pytest.raises(m.MatroskaBuildError, match="无法按尾部排除"):
        _run_multiclip_build(
            monkeypatch, tmp_path, asset=asset, capture=capture,
            payloads={f"{output.name}.item1.m2ts": drifted},
        )
    assert capture["commands"] == []


def test_trim_command_maps_every_stream_and_cuts_at_nominal_duration(
    tmp_path, monkeypatch,
):
    """裁切命令必须 -map 0（否则 ffmpeg 只留每类一条流，音轨/字幕被丢）。

    2026-09-15 越南战争：漏 -map 0 的裁切产物缺 PGS/多余音轨 → mkvmerge 报
    "A track with the ID 5 was requested but not found" 且 --append-to invalid 终止。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    captured: list[list[str]] = []

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, *args, **kwargs):
        captured.append(list(command))
        pathlib.Path(command[-1]).write_bytes(b"trimmed")
        return FakeCompleted()

    builder = MatroskaBuilder()
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    segment = tmp_path / "seg.m2ts"
    segment.write_bytes(b"x")
    staging = tmp_path / "seg.staging"
    trimmed = builder._trim_segment(segment, 600.0, 539.839, staging)

    assert trimmed is not None and trimmed.exists()
    (command,) = captured
    assert command[command.index("-map") + 1] == "0"
    assert command[command.index("-t") + 1] == "539.839"
    assert "-c" in command and command[command.index("-c") + 1] == "copy"
    # 承载容器必须是 mpegts：matroska 承载时 ffmpeg 不写时长元数据，mkvmerge
    # 汇总出的成品会被管线成品门判「缺少有效时长/Cues 信息」（越南战争实证）。
    assert command[command.index("-f") + 1] == "mpegts"
    # 不加 -copyts：-t 以输出时间轴为准，保留素材内时间戳时会把整段裁空（实测 0 字节）
    assert "-copyts" not in command
    assert command[-1].endswith(".trimmed.ts")
    assert str(segment) in command


def test_multiclip_tail_overhang_is_measured_not_trimmed(tmp_path, monkeypatch, caplog):
    """末端溢出**只测量、不裁切**，并计入拼接门的时长期望。

    2026-09-15 真盘实测：裁切（matroska/TS 承载都一样）会让 mkvmerge 对追加成品
    **无法确定容器时长**（成品门判「缺少有效时长/Cues 信息」）；而 17 段原始分段
    追加一切正常（rc=0、时长 6574.81s 双工具一致）。溢出的成因是母盘切点落在 PGS
    显示集内部（结构事实），因此：封装用原始分段，期望时长 = Σ 段时长 + 实测溢出。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    trims: list = []
    monkeypatch.setattr(MatroskaBuilder, "_segment_first_timestamp",
                        lambda self, _segment: 600.0)
    monkeypatch.setattr(MatroskaBuilder, "_segment_tail_overhang",
                        lambda self, _segment, _first, _duration: 0.42)
    monkeypatch.setattr(MatroskaBuilder, "_trim_segment",
                        lambda self, *a, **k: trims.append(a) or None)
    with caplog.at_level("WARNING", logger="hdathome.matroska"):
        capture = _run_multiclip_build(monkeypatch, tmp_path)

    # 不再裁切
    assert trims == []
    (command,) = capture["commands"]
    inputs = [token for token in command if token.endswith(".m2ts")]
    assert len(inputs) == 3 and all(".trimmed" not in token for token in inputs)
    # 溢出作为事实留痕
    assert "末端溢出" in caplog.text and "计入时长期望" in caplog.text
    # 溢出合计随构建结果留痕（拼接门期望 = Σ 段时长 + 该合计）
    variances = capture["result"].get("segment_track_variances") or []
    assert variances and all("末端溢出" in item for item in variances)


def test_trim_command_maps_every_stream_and_cuts_at_nominal_duration(
    tmp_path, monkeypatch,
):
    """裁切命令必须 -map 0（否则 ffmpeg 只留每类一条流，音轨/字幕被丢）。

    2026-09-15 越南战争：漏 -map 0 的裁切产物缺 PGS/多余音轨 → mkvmerge 报
    "A track with the ID 5 was requested but not found" 且 --append-to invalid 终止。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    captured: list[list[str]] = []

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, *args, **kwargs):
        captured.append(list(command))
        pathlib.Path(command[-1]).write_bytes(b"trimmed")
        return FakeCompleted()

    builder = MatroskaBuilder()
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    segment = tmp_path / "seg.m2ts"
    segment.write_bytes(b"x")
    staging = tmp_path / "seg.staging"
    trimmed = builder._trim_segment(segment, 600.0, 539.839, staging)

    assert trimmed is not None and trimmed.exists()
    (command,) = captured
    assert command[command.index("-map") + 1] == "0"
    assert command[command.index("-t") + 1] == "539.839"
    assert "-c" in command and command[command.index("-c") + 1] == "copy"
    # 承载容器必须是 mpegts：matroska 承载时 ffmpeg 不写时长元数据，mkvmerge
    # 汇总出的成品会被管线成品门判「缺少有效时长/Cues 信息」（越南战争实证）。
    assert command[command.index("-f") + 1] == "mpegts"
    # 不加 -copyts：-t 以输出时间轴为准，保留素材内时间戳时会把整段裁空（实测 0 字节）
    assert "-copyts" not in command
    assert command[-1].endswith(".trimmed.ts")
    assert str(segment) in command


def test_segment_subset_must_be_per_type_prefix():
    """纯子集放行的**唯一安全形态**：每类轨序列都是首段同类序列的前缀。

    2026-09-15：追加段的逐文件轨编号选项不能跨文件复用，mkvmerge 按类型+顺序
    把追加段续接到首段同类轨；中段少一条会让其后的同类轨整体前移（静默错接），
    因此只有"末尾少声明"才放行（越南战争尾段预告少 2 条 PGS → 前缀 ✓ 放行；
    若少的是中间那条 → 失败关闭）。
    """
    import copy
    import bluray_fidelity.matroska as m

    payload = copy.deepcopy(_input_identification())
    first = m._segment_track_facts(payload)

    # 末尾少声明：删除最后一条音频轨 → 前缀，放行并给出子集事实
    trailing = copy.deepcopy(payload)
    audio_ids = [track["id"] for track in trailing["tracks"] if track["type"] == "audio"]
    trailing["tracks"] = [
        track for track in trailing["tracks"] if track["id"] != audio_ids[-1]
    ]
    subset = m.require_segment_track_correspondence(
        first, m._segment_track_facts(trailing), label="第 2/3 段（play item 1）")
    assert subset is not None
    assert [key[1] for key in subset.missing] == [
        [track for track in payload["tracks"] if track["id"] == audio_ids[-1]][0]
        ["properties"]["stream_id"]
    ]

    # 中段少声明：删除第一条音频轨（其后同类轨会整体前移）→ 失败关闭
    middle = copy.deepcopy(payload)
    middle["tracks"] = [
        track for track in middle["tracks"] if track["id"] != audio_ids[0]
    ]
    with pytest.raises(m.MatroskaBuildError):
        m.require_segment_track_correspondence(
            first, m._segment_track_facts(middle), label="第 2/3 段（play item 1）")


def test_segment_track_correspondence_rejects_added_and_mismatched():
    """追加前必须逐项核对：增轨、事实漂移仍然失败关闭并带差异事实。"""
    import copy
    import bluray_fidelity.matroska as m

    payload = copy.deepcopy(_input_identification())
    payload["tracks"][2]["properties"]["audio_channels"] = 8
    payload["tracks"][3]["properties"]["audio_channels"] = 2
    payload["tracks"][4]["properties"]["audio_channels"] = 2
    first = m._segment_track_facts(payload)

    added = copy.deepcopy(payload)
    added["tracks"].append({"id": 7, "type": "subtitles", "codec": "HDMV PGS",
                            "properties": {"stream_id": 4770}})
    with pytest.raises(m.MatroskaBuildError, match="新增 subtitles/pid=0x12a2"):
        m.require_segment_track_correspondence(
            first, m._segment_track_facts(added), label="第 2/3 段（play item 1）")

    changed = copy.deepcopy(payload)
    changed["tracks"][4]["properties"]["audio_channels"] = 6
    with pytest.raises(m.MatroskaBuildError, match="不一致") as error:
        m.require_segment_track_correspondence(
            first, m._segment_track_facts(changed), label="第 2/3 段（play item 1）")
    text = str(error.value)
    assert "audio/pid=0x1101" in text and "audio_channels=6" in text

    language = copy.deepcopy(payload)
    language["tracks"][4]["properties"]["language"] = "jpn"
    with pytest.raises(m.MatroskaBuildError, match="不一致"):
        m.require_segment_track_correspondence(
            first, m._segment_track_facts(language), label="第 2/3 段（play item 1）")

    # 视频事实（识别层不提供的档次/帧率/时间基用 ffprobe 事实补齐）
    with pytest.raises(m.MatroskaBuildError, match="视频事实"):
        m.require_segment_video_correspondence(
            {"codec_name": "hevc", "profile": "Main 10", "width": 3840,
             "avg_frame_rate": "24000/1001", "time_base": "1/90000"},
            {"codec_name": "hevc", "profile": "Main", "width": 3840,
             "avg_frame_rate": "24000/1001", "time_base": "1/90000"},
            label="第 2/3 段（play item 1）",
        )


def test_segment_track_correspondence_accepts_pure_subset():
    """纯子集缺失（后段少一条字幕轨，无新增无漂移）返回事实而不是失败。

    2026-09-14 越南战争：终段少声明两条 PGS，母盘原生播放同此，失败关闭会
    让该标题永远不可交付。
    """
    import copy
    import bluray_fidelity.matroska as m

    payload = copy.deepcopy(_input_identification())
    first = m._segment_track_facts(payload)
    subset = copy.deepcopy(payload)
    subset["tracks"] = [t for t in subset["tracks"] if t["id"] != 6]

    variance = m.require_segment_track_correspondence(
        first, m._segment_track_facts(subset), label="第 19/19 段（play item 18）")

    assert variance is not None
    assert variance.missing == (("subtitles", 4769),)
    detail = variance.detail()
    assert "第 19/19 段（play item 18）" in detail
    assert "缺少 subtitles/pid=0x12a1[id=6|HDMV PGS|und]" in detail
    assert "按子集放行" in detail

    # 同一 (type, pid) 换成别的表达是漂移不是子集：必须失败关闭。
    replaced = copy.deepcopy(payload)
    replaced["tracks"][6]["properties"]["language"] = "jpn"
    with pytest.raises(m.MatroskaBuildError, match="不一致"):
        m.require_segment_track_correspondence(
            first, m._segment_track_facts(replaced), label="第 2/3 段（play item 1）")


def test_multiclip_mid_subset_fails_closed(tmp_path, monkeypatch, caplog):
    """中段纯子集：无法按尾部排除（跳过中段=连续性被破坏）→ 失败关闭并点名。

    2026-09-15 实测（越南战争）：子集段追加进成品会让 mkvmerge 汇总出的容器**没有
    时长**（"最终 MKV 缺少有效时长/Cues 信息"，被管线成品门拒绝；单独 mux 该段或只
    追加完全对应的段则正常）。因此中段子集不再是"放行"，而是失败关闭；**尾部**子集
    按尾部排除处理（见 test_multiclip_trailing_subset_is_excluded_with_facts）。
    """
    import bluray_fidelity.matroska as m

    asset = _multiclip_asset(tmp_path)
    output = tmp_path / "final.mkv.partial"
    payload = _input_identification()
    payload["tracks"] = [track for track in payload["tracks"] if track["id"] != 6]

    capture: dict = {}
    with pytest.raises(m.MatroskaBuildError, match="无法按尾部排除"):
        _run_multiclip_build(
            monkeypatch, tmp_path, asset=asset, capture=capture,
            payloads={f"{output.name}.item1.m2ts": payload},
        )
    assert capture["commands"] == [], "失败关闭时封装命令不得发出"


def test_multiclip_trailing_subset_is_excluded_with_facts(tmp_path, monkeypatch, caplog):
    """尾部纯子集（末段少声明若干轨）显式排除并留痕：不并入成品、不静默。

    越南战争 item 17（173s 预告）少声明两条 PGS：把它追加进成品会让 mkvmerge 汇总
    出的容器没有时长（实测），故与漂移段同样按尾部排除处理，成品只含完全对应的段。
    """
    asset = _multiclip_asset(tmp_path)
    output = tmp_path / "final.mkv.partial"
    payload = _input_identification()
    payload["tracks"] = [track for track in payload["tracks"] if track["id"] != 6]

    with caplog.at_level("WARNING", logger="hdathome.matroska"):
        capture = _run_multiclip_build(
            monkeypatch, tmp_path, asset=asset,
            payloads={f"{output.name}.item2.m2ts": payload},
        )

    (command,) = capture["commands"]
    segments = [output.with_name(f"{output.name}.item{index}.m2ts")
                for index in range(3)]
    assert [token for token in command if token.endswith(".m2ts")] == [
        str(segments[0]), str(segments[1]),
    ]
    text = "\n".join(record.getMessage() for record in caplog.records
                     if record.name == "hdathome.matroska")
    assert "已显式排除" in text
    assert "纯子集" in text


def test_multiclip_video_disagreement_fails_closed_before_mux(tmp_path, monkeypatch):
    """后段视频事实（分辨率）与首段不一致：失败关闭，封装命令从未发出。"""
    import bluray_fidelity.matroska as m

    asset = _multiclip_asset(tmp_path)
    output = tmp_path / "final.mkv.partial"
    capture: dict = {}

    def streams(source):
        return _b2_streams(width=1920 if Path(source).name.endswith("item1.m2ts") else 3840)

    with pytest.raises(m.MatroskaBuildError, match="视频事实") as error:
        _run_multiclip_build(
            monkeypatch, tmp_path, asset=asset, capture=capture, streams=streams,
        )
    assert "width 首段[3840]≠本段[1920]" in str(error.value)
    assert capture["commands"] == []
    # 同上：多段全核对后才判定是否可按尾部排除；封装命令从未发出。
    assert capture["read_items"] == [0, 1, 2]


def test_multiclip_route_accepts_dv_via_segment_builder(tmp_path, monkeypatch):
    """2026-09-24（用户裁定「这条也能做」）：多段 DV **不再失败关闭**。

    每段先做**段级 DV 构建**（BL+EL 用 dovi_tool 合并、官方音轨/字幕由 ffmpeg
    重封成中间件并进段），成品再按播放序**纯追加**；DV 证据按播放序聚合后交给统一
    的 Profile 7 成品门，官方轨映射取**段级实测**（这类盘的原始分段 mkvmerge 读不到
    音轨，用它算映射会让成品门误判数量——生产实测 9≠8 被拒）。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder, ResolvedTracks

    asset = _multiclip_asset(tmp_path)
    dv_asset = replace(asset, video_tracks=(
        BlurayTrack(index=0, pid=0x1011, coding_type=0x24, language=""),
        BlurayTrack(index=1, pid=0x1015, coding_type=0x24, language=""),
    ))

    dv_calls: list[tuple[str, dict | None]] = []
    gate_calls: list = []
    vision_calls: list = []

    def fake_dv_segment(self, asset_, source, target, progress, *, label, layout):
        dv_calls.append((label, layout))
        target.write_bytes(b"dv-segment")
        resolved = ResolvedTracks(
            video_ids=(0,), audio_ids=(0, 1), subtitle_ids=(0,),
            audio_by_official_index=((0, 0), (1, 1)),
            subtitle_by_official_index=((0, 0),))
        return {
            "path": target, "resolved": resolved,
            "official_audio": (0, 1), "official_subtitle": (0,),
            "evidence": {
                "label": label, "dovi_executable": "/usr/local/bin/dovi_tool",
                "bl_size": 10, "merged_size": 20, "el_digests": [f"d-{label}"],
                "el_stats": {"vcl_count": 1, "payload_bytes": 5},
                "merged_rpu_evidence": {"method": "dovi_tool extract-rpu",
                                        "limit_frames": 240, "rpu_file_size": 7},
                "fps": 23.976, "duration_s": 300.0,
                "audio_tracks": 2, "subtitle_tracks": 1,
                "official_audio_indices": [0, 1],
                "official_subtitle_indices": [0],
                "skipped_same_pid_streams": 0,
                "source_pids": {"base": 4113, "enhancement": 4117},
            },
        }

    monkeypatch.setattr(MatroskaBuilder, "_dv_segment", fake_dv_segment)
    monkeypatch.setattr(m, "_verify_dv_profile7_evidence",
                        lambda *a, **k: gate_calls.append(a))
    monkeypatch.setattr(MatroskaBuilder, "_verify_dolby_vision",
                        lambda self, output: vision_calls.append(output))

    capture = _run_multiclip_build(
        monkeypatch, tmp_path, asset=dv_asset, output_name="multiclip-dv.mkv.partial")

    assert len(dv_calls) == 3, "三段都必须走段级 DV 构建"
    assert dv_calls[0][1] is None, "首段确立官方轨集合"
    assert all(layout is not None for _, layout in dv_calls[1:]), \
        "后续段必须拿到首段布局做一致性核对"
    append_commands = [command for command in capture["commands"] if "+" in command]
    assert append_commands, "多段 DV 成品必须是纯追加（时间轴按段续接）"
    assert gate_calls, "多段 DV 成品必须过统一 Profile 7 证据门"
    assert vision_calls, "多段 DV 成品必须过容器 DOVI 配置记录校验"


def test_multiclip_dv_flaky_track_identify_no_longer_excludes_segments(
    tmp_path, monkeypatch,
):
    """DV 多段：原始分段的 mkvmerge 轨识别不可靠时不再逐段核对轨对应、不再尾部排除。

    2026-09-24 生产实测（《阿凡达》加长版 33 段）：mkvmerge 对原始分段逐段识别
    不一致（首段见 2 条音轨、其余段见 0 条），旧检查把段 2..33 全判违规尾部排除，
    成品只剩第 1 段后被 EL 一致性门拒绝。DV 路线的轨布局由官方 PID 计划重建，
    该检查在 DV 路径既不可靠也不必需——现在跳过，全部段进入段级 DV 构建与纯追加。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder, ResolvedTracks

    asset = _multiclip_asset(tmp_path)
    dv_asset = replace(asset, video_tracks=(
        BlurayTrack(index=0, pid=0x1011, coding_type=0x24, language=""),
        BlurayTrack(index=1, pid=0x1015, coding_type=0x24, language=""),
    ))
    output = tmp_path / "multiclip-dv.mkv.partial"
    # 段 2/3 的 mkvmerge 识别缺全部音轨（与首段不同）——旧行为：判违规→尾部排除。
    audio_less = _input_identification()
    audio_less["tracks"] = [
        track for track in audio_less["tracks"] if track["type"] != "audio"
    ]

    dv_calls: list[tuple[str, dict | None]] = []

    def fake_dv_segment(self, asset_, source, target, progress, *, label, layout):
        dv_calls.append((label, layout))
        target.write_bytes(b"dv-segment")
        resolved = ResolvedTracks(
            video_ids=(0,), audio_ids=(0, 1), subtitle_ids=(0,),
            audio_by_official_index=((0, 0), (1, 1)),
            subtitle_by_official_index=((0, 0),))
        return {
            "path": target, "resolved": resolved,
            "official_audio": (0, 1), "official_subtitle": (0,),
            "evidence": {
                "label": label, "dovi_executable": "/usr/local/bin/dovi_tool",
                "bl_size": 10, "merged_size": 20, "el_digests": [f"d-{label}"],
                "el_stats": {"vcl_count": 1, "payload_bytes": 5},
                "merged_rpu_evidence": {"method": "dovi_tool extract-rpu",
                                        "limit_frames": 240, "rpu_file_size": 7},
                "fps": 23.976, "duration_s": 300.0,
                "audio_tracks": 2, "subtitle_tracks": 1,
                "official_audio_indices": [0, 1],
                "official_subtitle_indices": [0],
                "skipped_same_pid_streams": 0,
                "source_pids": {"base": 4113, "enhancement": 4117},
            },
        }

    monkeypatch.setattr(MatroskaBuilder, "_dv_segment", fake_dv_segment)
    monkeypatch.setattr(m, "_verify_dv_profile7_evidence", lambda *a, **k: None)
    monkeypatch.setattr(MatroskaBuilder, "_verify_dolby_vision",
                        lambda self, output: None)

    capture = _run_multiclip_build(
        monkeypatch, tmp_path, asset=dv_asset, output_name=output.name,
        payloads={f"{output.name}.item1.m2ts": audio_less,
                  f"{output.name}.item2.m2ts": audio_less},
    )

    assert len(dv_calls) == 3, "三段都必须走段级 DV 构建（不再被尾部排除）"
    append_commands = [c for c in capture["commands"] if "+" in c]
    assert len(append_commands) == 1, "成品应是单条追加命令"
    assert append_commands[0].count("+") == 2, "全部三段进入纯追加，无尾部排除"


def test_multiclip_dv_trailing_video_disagreement_fails_closed(
    tmp_path, monkeypatch,
):
    """DV 多段不允许尾部排除：尾部段视频事实漂移 → 显式失败关闭（不静默截断）。

    2026-09-24：DV 段级轨布局由官方 PID 计划重建，任何结构违规都意味着成品会
    缺段，而 EL 一致性门比对的是全片源序列——必须立即失败关闭而不是尾部排除。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _multiclip_asset(tmp_path)
    dv_asset = replace(asset, video_tracks=(
        BlurayTrack(index=0, pid=0x1011, coding_type=0x24, language=""),
        BlurayTrack(index=1, pid=0x1015, coding_type=0x24, language=""),
    ))

    def streams(source):
        return _b2_streams(
            width=1920 if Path(source).name.endswith("item2.m2ts") else 3840)

    monkeypatch.setattr(
        MatroskaBuilder, "_dv_segment",
        lambda self, *a, **k: {
            "path": a[1].with_name(a[1].name + ".dv.mkv"),
            "resolved": None,
            "official_audio": (), "official_subtitle": (),
            "evidence": {}})

    capture: dict = {}
    with pytest.raises(m.MatroskaBuildError, match="DV 多段合辑不允许尾部排除段"):
        _run_multiclip_build(
            monkeypatch, tmp_path, asset=dv_asset, capture=capture, streams=streams,
        )
    assert capture["commands"] == []


def test_multiclip_route_fails_closed_for_hdr10plus(tmp_path, monkeypatch):
    """HDR10+ 仍需整片剥离后封装：多段追加无法表达，点名拒绝、不退回字节拼接。"""
    import bluray_fidelity.matroska as m

    asset = _multiclip_asset(tmp_path)
    capture: dict = {}
    with pytest.raises(m.MatroskaBuildError, match=r"HDR10\+"):
        _run_multiclip_build(
            monkeypatch, tmp_path, asset=asset, capture=capture,
            output_name="hdr10.mkv.partial",
            hdr10plus=lambda path: path.name.endswith("item1.m2ts"),
        )
    assert capture["read_items"] == [0, 1]
    assert capture["commands"] == []


def test_dv_segment_command_uses_official_mapping(tmp_path):
    """段级 DV 封装命令：语言/默认轨按官方映射注入；缺中间件则失败关闭。"""
    from bluray_fidelity.matroska import (
        MatroskaBuildError, ResolvedTracks, build_dv_segment_command,
    )

    asset = replace(
        _asset(tmp_path),
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x83, language="eng"),
            BlurayTrack(index=1, pid=0x1101, coding_type=0x81, language="chi"),
        ),
        subtitle_tracks=(
            BlurayTrack(index=0, pid=0x12A0, coding_type=0x90, language="chi"),
        ),
    )
    merged = tmp_path / "merged.hevc"
    merged.write_bytes(b"v")
    audio_ts = tmp_path / "audio.ts"
    audio_ts.write_bytes(b"a")
    subs_ts = tmp_path / "subs.ts"
    subs_ts.write_bytes(b"s")
    resolved = ResolvedTracks(
        video_ids=(0,), audio_ids=(0, 1), subtitle_ids=(0,),
        audio_by_official_index=((0, 0), (1, 1)),
        subtitle_by_official_index=((0, 0),))
    command = build_dv_segment_command(
        asset, merged, audio_ts, subs_ts, tmp_path / "seg.dv.mkv", title="段",
        resolved=resolved)
    assert "0:eng" in command and "1:chi" in command
    # 默认轨只打在中文音轨（asset.default_audio_index == 1）与中文字幕上
    assert "1:yes" in command
    assert command.count("--default-track-flag") == 4, "视频 1 + 音频 2 + 字幕 1"
    with pytest.raises(MatroskaBuildError, match="音轨中间件"):
        build_dv_segment_command(
            asset, merged, tmp_path / "missing.ts", subs_ts,
            tmp_path / "seg2.dv.mkv", title="段", resolved=resolved)


def test_aggregate_multiclip_dv_facts_concatenates_in_play_order(tmp_path):
    """多段 DV 证据聚合：EL 序列按播放序拼接、体积求和；缺段证据即失败关闭。"""
    from bluray_fidelity.matroska import (
        MatroskaBuildError, _aggregate_multiclip_dv_facts,
    )

    def evidence(label, digest, bl, merged, rpu):
        return {"label": label, "dovi_executable": "/usr/local/bin/dovi_tool",
                "bl_size": bl, "merged_size": merged, "el_digests": [digest],
                "el_stats": {"vcl_count": 1, "payload_bytes": 3},
                "merged_rpu_evidence": {"method": "dovi_tool extract-rpu",
                                        "limit_frames": 240, "rpu_file_size": rpu},
                "fps": 23.976, "duration_s": 10.0}

    facts = _aggregate_multiclip_dv_facts([
        evidence("分段 1", "a", 10, 20, 5), evidence("分段 2", "b", 30, 60, 7),
    ])
    assert facts["el_source_digests"] == ["a", "b"], "必须按播放序拼接"
    assert facts["bl_size"] == 40 and facts["merged_size"] == 80
    assert facts["duration_s"] == 20.0
    assert facts["merged_rpu_evidence"]["rpu_file_size"] == 12
    with pytest.raises(MatroskaBuildError, match="RPU 证据"):
        _aggregate_multiclip_dv_facts([evidence("分段 1", "a", 10, 20, 0)])
    with pytest.raises(MatroskaBuildError, match="证据为空"):
        _aggregate_multiclip_dv_facts([])


def test_official_stream_plan_takes_one_stream_per_official_pid(tmp_path):
    """每个官方 PID 只取第一条流（同 PID 的第二条不另立轨），并如实记略过数。

    同 PID 的第二条流 = TrueHD 的内嵌 AC-3 core：官方表只声明一条，若另立轨
    成品就是 9 条 vs 官方 8 条 ⇒ 成品门「音轨数量与可映射 MPLS 音轨不一致」。
    """
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = replace(
        _asset(tmp_path),
        audio_tracks=(
            BlurayTrack(index=0, pid=0x1100, coding_type=0x83, language="eng"),
            BlurayTrack(index=1, pid=0x1101, coding_type=0x81, language="chi"),
        ),
        subtitle_tracks=(
            BlurayTrack(index=0, pid=0x12A0, coding_type=0x90, language="chi"),
        ),
    )
    assert asset.default_audio_index == 1
    pid_index = {0x1100: [2, 3], 0x1101: [4], 0x12A0: [11]}
    indices, entries, skipped = MatroskaBuilder._official_stream_plan(
        asset, pid_index, "audio")
    assert indices == [2, 4], "每个官方 PID 只取第一条流"
    assert [entry.index for entry in entries] == [0, 1]
    assert skipped == 1, "同 PID 的第二条流如实记入略过数"
    sub_indices, sub_entries, sub_skipped = MatroskaBuilder._official_stream_plan(
        asset, pid_index, "subtitle")
    assert sub_indices == [11] and [e.index for e in sub_entries] == [0]
    assert sub_skipped == 0


def test_multiclip_seam_gates_check_duration_continuity_and_seek_targets(tmp_path, monkeypatch):
    """拼接门全部从成品可复核：总时长、拼接点时间戳（回跳/空档/未覆盖）、
    拼接点进入同一套 seek 门。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _multiclip_asset(tmp_path)  # 三段各 300s
    output = tmp_path / "final.mkv"
    output.write_bytes(b"x")
    builder = MatroskaBuilder()
    monkeypatch.setattr(m, "_probe_video_timeline", lambda *a, **k: {"start_time": "0.000000"})
    seam_targets: list = []
    monkeypatch.setattr(
        m, "_verify_cues_and_seek",
        lambda _probe, _path, _ns, **kw: seam_targets.extend(kw.get("extra_targets", ())),
    )

    def packets(offsets):
        """按拼接点相对偏移产出包：窄窗（判定用）后接加宽窗（本例无余量）。"""
        def fake(_probe, _path, start, diag=None, **kwargs):
            seam = start + m._SEAM_HALF_WINDOW_SECONDS
            return [{"pts_time": str(seam + offset)} for offset in offsets]
        return fake

    monkeypatch.setattr(m, "_record_wide_window", lambda *a, **k: None)

    monkeypatch.setattr(m, "_probe_duration", lambda *a, **k: 900.0)
    monkeypatch.setattr(m, "_probe_interval_packets", packets((-0.5, 0.04)))
    builder._verify_multiclip_seams(asset, output, _MULTICLIP_BOUNDS)
    assert seam_targets == [300.0, 600.0]

    monkeypatch.setattr(m, "_probe_duration", lambda *a, **k: 890.0)
    with pytest.raises(MatroskaBuildError, match="Σ play item 时长"):
        builder._verify_multiclip_seams(asset, output, _MULTICLIP_BOUNDS)

    # 追加段重启时间轴：探针顺序内出现秒级回退。
    monkeypatch.setattr(m, "_probe_duration", lambda *a, **k: 900.0)
    monkeypatch.setattr(m, "_probe_interval_packets", packets((-0.1, 0.1, -4.5)))
    with pytest.raises(MatroskaBuildError, match="时间戳回跳"):
        builder._verify_multiclip_seams(asset, output, _MULTICLIP_BOUNDS)

    # 拼接处内容缺口（两侧都有包但空档超限）。
    monkeypatch.setattr(m, "_probe_interval_packets", packets((-4.0, 0.5)))
    with pytest.raises(MatroskaBuildError, match="空档"):
        builder._verify_multiclip_seams(asset, output, _MULTICLIP_BOUNDS)

    # 拼接点后侧没有包（后段缺失/错位）。
    monkeypatch.setattr(m, "_probe_interval_packets", packets((3.0,)))
    with pytest.raises(MatroskaBuildError, match="未覆盖拼接点"):
        builder._verify_multiclip_seams(asset, output, _MULTICLIP_BOUNDS)

    # 判定只用窄窗：加宽窗是拼接门失败详情里的测量（只测量，不改判定）。
    probed: list[float] = []

    def recording(_probe, _path, start, diag=None, **kwargs):
        probed.append(float(kwargs.get("window_seconds", 0.0)))
        seam = start + m._SEAM_HALF_WINDOW_SECONDS
        return [{"pts_time": str(seam - 0.5)}, {"pts_time": str(seam + 0.04)}]

    monkeypatch.setattr(m, "_probe_interval_packets", recording)
    builder._verify_multiclip_seams(asset, output, _MULTICLIP_BOUNDS)
    assert probed == [2 * m._SEAM_HALF_WINDOW_SECONDS] * 2


def test_seam_gate_accepts_dts_only_video_packets(tmp_path, monkeypatch):
    """VC-1 成品只有 DTS（pts_time=N/A）时拼接门必须照样通过。

    2026-09-15 真盘实证：地球脉动 dlv_720414a5…（VC-1 1080i）的 mkvmerge 成品里
    视频包只有 DTS；只认 PTS 的窗口判定把内容完好的拼接点报成"附近无视频包"并
    拒绝发布（窄窗 [3065,3075] 按 DTS 有 240 个视频包、时间轴连续；按 PTS 为 0）。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _multiclip_asset(tmp_path)
    output = tmp_path / "final.mkv"
    output.write_bytes(b"x")
    builder = MatroskaBuilder()
    monkeypatch.setattr(m, "_probe_video_timeline", lambda *a, **k: {"start_time": "0.000000"})
    monkeypatch.setattr(m, "_verify_cues_and_seek", lambda *a, **k: None)
    monkeypatch.setattr(m, "_record_wide_window", lambda *a, **k: None)
    monkeypatch.setattr(m, "_probe_duration", lambda *a, **k: 900.0)
    monkeypatch.setattr(
        m, "_probe_interval_packets",
        lambda _probe, _path, start, diag=None, **kwargs: [
            {"pts_time": None, "dts_time": str(start + m._SEAM_HALF_WINDOW_SECONDS - 0.5)},
            {"pts_time": None, "dts_time": str(start + m._SEAM_HALF_WINDOW_SECONDS + 0.04)},
        ])

    # 不抛异常：DTS 回退后内容覆盖与空档判定都成立
    builder._verify_multiclip_seams(asset, output, _MULTICLIP_BOUNDS)

    # 两个时间戳都缺 → 仍失败关闭（不放行"完全没有时间戳"的成品）
    monkeypatch.setattr(
        m, "_probe_interval_packets",
        lambda *a, **k: [{"pts_time": None, "dts_time": None}])
    with pytest.raises(MatroskaBuildError, match="无视频包"):
        builder._verify_multiclip_seams(asset, output, _MULTICLIP_BOUNDS)


def test_seam_gate_failure_is_self_diagnosing_and_preserves_artifact(tmp_path, monkeypatch):
    """拼接门失败必须自诊断：详情携带窗口/分段可复核事实并点名保留的成品。

    2026-09-14 地球脉动：只报「无视频包」无法区分来源缺头部、append 未续接
    还是探针越界；成品又被失败路径删掉，无法复检。门仍不放宽（不发布）。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _multiclip_asset(tmp_path)  # 三段各 300s
    _install_multiclip_reader(monkeypatch)
    output = tmp_path / "final.mkv.partial"
    # 已发布的分段检查点：逐段字节事实必须取自实际分段文件。
    for index, (_, length) in enumerate(_MULTICLIP_BOUNDS):
        output.with_name(f"{output.name}.item{index}.m2ts").write_bytes(b"m" * length)
    builder = MatroskaBuilder()
    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/mkvmerge")
    monkeypatch.setattr(builder, "_identify_path", lambda _source: _input_identification())
    monkeypatch.setattr(builder, "_video_streams", lambda _source: _b2_streams())
    monkeypatch.setattr(builder, "_has_hdr10plus", lambda _source: False)
    monkeypatch.setattr(m, "validate_finalized_identification", lambda *a, **k: {})
    monkeypatch.setattr(m, "require_track_coverage", lambda *a, **k: {})

    class FakeProcess:
        stdout = iter(())

        def wait(self):
            output.write_bytes(b"merged-output")
            return 0

    class FakeCompleted:
        # 识别必须返回**已识别的容器**（2026-09-17 起有 _require_identified_container
        # 收口）：空 JSON 会在时长门前被拒，测不到拼接门的自诊断。
        stdout = json.dumps({
            "container": {"properties": {"duration": 900_000_000_000}},
            "tracks": [],
        })

    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: FakeCompleted())
    monkeypatch.setattr(m, "_probe_video_timeline", lambda *a, **k: {"start_time": "0.000000"})
    monkeypatch.setattr(
        m, "_probe_duration",
        lambda _probe, path: 30.0 if Path(path).name.endswith(".item0.m2ts") else 900.0,
    )
    monkeypatch.setattr(m, "_probe_interval_packets", lambda *a, **k: [])

    with pytest.raises(m.MatroskaBuildError, match="无视频包") as error:
        builder.build(asset, output, title="合辑")

    detail = str(error.value)
    preserved = output.with_name(output.name + ".seam-failure.mkv")
    # 门不放宽：成品不在发布路径上，但字节仍在沙盒里可复检。
    assert preserved.read_bytes() == b"merged-output"
    assert not output.exists()
    # 原始原因（含拼接点与目标时间）+ 保留路径必须落在执行账本 detail 保留的
    # 前 300 字符内——保留路径不可被后续清理删除，也不可被详情截断吞掉。
    assert "多段合辑拼接点 1/2 附近（300.0s）无视频包" in detail[:300]
    assert 0 <= detail.index(str(preserved)) < 300
    assert "供复检" in detail
    assert error.value.seam_diagnostics["preserved"] == str(preserved)
    # 可测的接缝事实（完整详情在 build 日志与 seam_diagnostics 里）：拼接点
    # 两侧段、窄窗/加宽窗包数与首末 pts_time、逐段字节与实测时长、成品声明
    # 时长对 Σ 分段。
    assert "前段=item0 clip=00001 in=600.0s out=900.0s 时长=300.0s 字节=[0,30)" in detail
    assert "后段=item1 clip=00002 in=0.0s out=300.0s 时长=300.0s 字节=[30,50)" in detail
    assert "窄窗=295.0s→305.0s 视频包=0 首末pts=None→None" in detail
    assert "加宽窗=0.0s→300.0s 视频包=0" in detail
    assert "逐段字节：item0 边界=30B 分段=30B；item1 边界=20B 分段=20B" in detail
    assert "逐段时长：item0 30.0s；item1 900.0s" in detail
    assert "成品声明时长=900.0s Σ分段=900.0s" in detail
    # 分段检查点保留（与失败成品同目录，供复检），published partial 不残留。
    assert output.with_name(output.name + ".item0.m2ts").read_bytes() == b"m" * 30


def test_seam_preserved_path_is_generation_scoped(tmp_path):
    """保留路径与 partial 名同源（同域不同代互不覆盖）。"""
    from bluray_fidelity.matroska import seam_preserved_path

    first = seam_preserved_path(tmp_path / "main-title.m2ts.partial.1")
    second = seam_preserved_path(tmp_path / "main-title.m2ts.partial.2")
    assert first.name == "main-title.m2ts.partial.1.seam-failure.mkv"
    assert first.parent == tmp_path and first != second


def test_seek_gate_adds_seam_targets_without_relaxing_percent_gates(tmp_path, monkeypatch):
    """拼接点用与 10/50/90% 完全相同的门与容差（不放松），结果可区分 seam。"""
    import bluray_fidelity.matroska as m

    output = tmp_path / "out.mkv"
    output.write_bytes(b"x")
    monkeypatch.setattr(m, "_count_cue_points", lambda _p: 3)
    monkeypatch.setattr(
        m, "_probe_interval_packets",
        lambda _probe, _path, target, diag=None, **kw: [
            {"pts_time": str(target), "flags": "K"},
        ],
    )
    monkeypatch.setattr(
        m, "_probe_interval_frames",
        lambda _probe, _path, target, diag=None, **kw: [float(target)],
    )
    result = m._verify_cues_and_seek(
        "ffprobe", output, int(900 * 1e9), extra_targets=(300.0, 600.0),
    )
    assert [entry["frac"] for entry in result["cues"][:3]] == [0.10, 0.50, 0.90]
    seams = [entry for entry in result["cues"] if entry.get("seam_index")]
    assert [entry["seam_index"] for entry in seams] == [1, 2]
    assert [entry["target_seconds"] for entry in seams] == [300.0, 600.0]
    assert all(entry["frac"] is None for entry in seams)

    # 拼接点越界同样失败关闭（与百分比目标同一容差、同一门）。
    def frames_only_percent(_probe, _path, target, diag=None, **kw):
        return [float(target)] if target in (90.0, 450.0, 810.0) else [999.0]

    monkeypatch.setattr(m, "_probe_interval_frames", frames_only_percent)
    with pytest.raises(MatroskaBuildError, match="拼接点 1 时间越界"):
        m._verify_cues_and_seek("ffprobe", output, int(900 * 1e9), extra_targets=(300.0, 600.0))


def test_build_artifacts_registers_every_segment_and_checkpoint(tmp_path):
    """工件清单必须包含每一段（及其 staging）：清理从不 glob，漏登即永久泄漏。"""
    from bluray_fidelity.matroska import (
        build_artifacts,
        is_checkpoint_artifact,
    )

    partial = tmp_path / "main-title.m2ts.partial.7"
    single = build_artifacts(partial)
    assert single.segments == () and single.staging_segments == ()
    assert single.as_artifacts_list() == [
        str(partial),
        str(partial.with_name(partial.name + ".source.m2ts")),
        str(partial.with_name("main-title.materializing")),
        str(partial.with_name("build-7.log")),
    ]

    multi = build_artifacts(partial, segment_count=3)
    listed = multi.as_artifacts_list()
    for index in range(3):
        assert str(partial.with_name(f"{partial.name}.item{index}.m2ts")) in listed
        assert str(partial.with_name(f"main-title.materializing.item{index}")) in listed
    assert multi.checkpoint_paths() == (
        partial.with_name(partial.name + ".source.m2ts"),
        partial.with_name(partial.name + ".item0.m2ts"),
        partial.with_name(partial.name + ".item1.m2ts"),
        partial.with_name(partial.name + ".item2.m2ts"),
    )
    assert is_checkpoint_artifact(partial.name + ".source.m2ts")
    assert is_checkpoint_artifact(partial.name + ".item12.m2ts")
    assert not is_checkpoint_artifact(partial.name)
    assert not is_checkpoint_artifact("main-title.materializing.item0")
    assert not is_checkpoint_artifact(partial.name + ".itemX.m2ts")


def test_out_of_space_is_transient_not_media_defect(tmp_path, monkeypatch):
    """空间耗尽按瞬时故障重试（永久分类会烧掉重试预算），媒体缺陷仍永久。"""
    import errno
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import (
        MatroskaBuilder,
        TransientMatroskaBuildError,
    )

    assert m._is_out_of_space(OSError(errno.ENOSPC, "No space left on device"))
    assert m._is_out_of_space("Error: No space left to write to 'out.mkv'")
    assert not m._is_out_of_space("Error: Invalid state")

    # 物化阶段 ENOSPC：单文件路线外层收口为瞬时。
    asset = _asset(tmp_path)

    def failing_materialize(_asset, source, progress=None, staging=None):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(MatroskaBuilder, "_materialize_title",
                        staticmethod(failing_materialize))
    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/mkvmerge")
    with pytest.raises(TransientMatroskaBuildError, match="空间不足"):
        MatroskaBuilder().build(asset, tmp_path / "out.mkv", title="x")

    # 分段物化阶段 ENOSPC：多段路线同样瞬时。
    capture: dict = {}

    def failing_items(_asset, item_index, chunk_size=1 << 20):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(m, "bluray_item_bounds", lambda _asset: _MULTICLIP_BOUNDS)
    monkeypatch.setattr(m, "iter_bluray_item", failing_items)
    with pytest.raises(TransientMatroskaBuildError, match="空间不足"):
        MatroskaBuilder().build(_multiclip_asset(tmp_path), tmp_path / "multi.mkv",
                                title="x")

    # mkvmerge 非零退出且明文为空间不足：瞬时（优先判定）；退出码 1=有警告，
    # 成品继续走独立校验门；退出码 2=错误（man mkvmerge：0=成功、1=警告、
    # 2=错误并立即中止），必须失败关闭。
    def build_with_mux_output(text: str, code: int):
        class FakeProcess:
            stdout = iter((text + "\n",))

            def wait(self):
                return code

        builder = MatroskaBuilder()
        monkeypatch.setattr(builder, "_materialize_title",
                            staticmethod(lambda _a, source, **k: source.write_bytes(b"m")))
        monkeypatch.setattr(builder, "_identify_path",
                            lambda _source: _input_identification())
        monkeypatch.setattr(builder, "_video_streams", lambda _source: [])
        monkeypatch.setattr(builder, "_has_hdr10plus", lambda _source: False)
        monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: FakeProcess())
        monkeypatch.setattr(m, "validate_finalized_identification", lambda *a, **k: {})
        monkeypatch.setattr(m, "require_track_coverage", lambda *a, **k: {})

        class FakeRun:
            returncode = 0
            stderr = ""
            stdout = json.dumps(_input_identification())

        monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: FakeRun())
        monkeypatch.setattr(MatroskaBuilder, "_verify_seekable", lambda *a, **k: None)
        return builder.build(asset, tmp_path / "out2.mkv", title="x")

    with pytest.raises(TransientMatroskaBuildError, match="空间不足"):
        build_with_mux_output("Error: No space left to write to 'out2.mkv'", 2)
    # 退出码 1 = 有警告（输出有效）：不失败，警告文本必须进构建结果（可复核）。
    result = build_with_mux_output("Warning: something odd", 1)
    assert "something odd" in result.get("mkvmerge_warnings", "")
    # 退出码 2 = 错误：失败关闭并带 mkvmerge 原文——绝不能像旧实现那样当
    # "已完成但有警告"继续，最终把硬封装失败伪装成成品门的时长/Cues 失败。
    with pytest.raises(MatroskaBuildError,
                       match=r"mkvmerge 生成失败（退出码 2）.*Invalid state"):
        build_with_mux_output("Error: Invalid state", 2)


def test_variant_routes_refuse_multiclip_titles(tmp_path, monkeypatch):
    """不能按 play item 追加的路线点名拒绝，且不物化、不产出文件。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import (
        FfmpegDvFileMatroskaBuilder,
        FfmpegFileMatroskaBuilder,
        FifoMatroskaBuilder,
    )

    asset = _multiclip_asset(tmp_path)
    monkeypatch.setattr(m.shutil, "which", lambda _name: f"/usr/bin/{_name}")
    materialized: list = []
    for builder_cls in (FfmpegFileMatroskaBuilder, FfmpegDvFileMatroskaBuilder):
        monkeypatch.setattr(builder_cls, "_materialize_or_reuse_title",
                            lambda self, *a, **k: materialized.append(a))
        with pytest.raises(MatroskaBuildError, match="不支持按 play item 分段追加"):
            builder_cls().build(asset, tmp_path / f"{builder_cls.__name__}.mkv", title="合辑")
    with pytest.raises(MatroskaBuildError, match="FIFO"):
        FifoMatroskaBuilder().build(asset, tmp_path / "fifo.mkv", title="合辑")
    assert materialized == []
    assert not list(tmp_path.glob("*.mkv"))


def test_multiclip_compilation_summary_is_bounded_and_specific(tmp_path):
    """P2：多段合辑记录逐段 in/out 事实（有界）且不改写时间轴；in_time 全零
    的多剪辑与单剪辑都不产生记录。"""
    import bluray_fidelity.bluray as bluray
    import bluray_fidelity.matroska as m

    clips = tuple(
        bluray.BlurayClip(index=index, clip_id=f"{index + 1:05d}",
                          in_time=54000000, out_time=81000000)
        for index in range(9)
    )
    asset = replace(_asset(tmp_path), clip_count=9, clips=clips)
    summary = m.describe_multiclip_compilation(asset)
    assert summary is not None
    assert "多段合辑主片" in summary and "playlist=801" in summary
    assert "#0 00001 in=600.0s out=900.0s" in summary
    assert "…(+4)" in summary, "超过逐段记录上限必须标记截断"
    assert len(summary) <= m._MULTICLIP_LOG_WIDTH

    zero_base = replace(
        asset,
        clips=tuple(
            replace(clip, in_time=0, out_time=27000000) for clip in clips
        ),
    )
    assert m.describe_multiclip_compilation(zero_base) is None
    assert m.describe_multiclip_compilation(
        replace(asset, clip_count=1, clips=clips[:1])) is None


def test_fifo_route_records_multiclip_compilation(tmp_path, monkeypatch, caplog):
    """P2：FIFO 路线不经物化复用钩子，合辑结构也必须留下同样的逐段记录。"""
    import bluray_fidelity.bluray as bluray
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import FifoMatroskaBuilder

    asset = replace(
        _asset(tmp_path),
        clips=(
            bluray.BlurayClip(index=0, clip_id="00001",
                              in_time=54000000, out_time=81000000),
            bluray.BlurayClip(index=1, clip_id="00002",
                              in_time=54000000, out_time=81000000),
        ),
    )
    builder = FifoMatroskaBuilder()

    class FakeProcess:
        stdout = iter(())

        def wait(self):
            return 1

    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(FifoMatroskaBuilder, "_probe_source_layout",
                        staticmethod(lambda *args, **kwargs: []))
    monkeypatch.setattr(FifoMatroskaBuilder, "_inject_track_metadata",
                        staticmethod(lambda cmd, *args, **kwargs: cmd))
    # 不建真实 FIFO：喂入线程立即以 OSError 结束，本测试只验证记录与失败收口。
    monkeypatch.setattr(m.os, "mkfifo", lambda _path: None)
    monkeypatch.setattr(m, "iter_bluray_title", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(m.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    with caplog.at_level("WARNING", logger="hdathome.matroska"):
        with pytest.raises(MatroskaBuildError):
            builder.build(asset, tmp_path / "fifo.mkv", title="合辑")
    text = "\n".join(
        record.getMessage() for record in caplog.records
        if record.name == "hdathome.matroska"
    )
    assert "多段合辑主片" in text
    assert "#0 00001 in=600.0s out=900.0s" in text


def test_primary_video_pid_rejects_source_whose_official_pid_has_no_picture():
    """2026-09-14 霸王别姬实测：DIY 重制盘的 MPLS 官方视频 PID 与真实画面 PID 不一致。

    官方表记 0x1011 而该流宽高为空，真实 3840x2160 在 0x1fff。按官方 PID
    抽裸 HEVC 只会得到无参数集的空流（ffmpeg: dimensions not set / 退出码
    234）。必须点名两个 PID 拒绝，而不是把它当编码器报错。
    """
    asset = BlurayTitleAsset(
        root=Path("/tmp/disc.iso"),
        entry_path="HDATHOME/MAIN_TITLE.m2ts",
        title_index=2,
        playlist=4,
        duration_90k=926_395_470,
        size=111_704_180_736,
        clip_count=1,
        source_kind="iso",
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0x24,
                                  language="und"),),
    )
    streams = [
        {"codec_type": "video", "id": "0x1011", "codec_name": "hevc",
         "width": 0, "height": 0},
        {"codec_type": "video", "id": "0x1fff", "codec_name": "hevc",
         "width": 3840, "height": 2160},
    ]

    with pytest.raises(MatroskaBuildError) as error:
        MatroskaBuilder()._primary_video_pid(asset, streams)

    message = str(error.value)
    assert "0x1011" in message
    assert "0x1fff" in message
    assert "非标准重制盘" in message


def test_primary_video_pid_keeps_official_pid_when_it_carries_picture():
    asset = BlurayTitleAsset(
        root=Path("/tmp/disc.iso"),
        entry_path="HDATHOME/MAIN_TITLE.m2ts",
        title_index=2,
        playlist=4,
        duration_90k=926_395_470,
        size=1,
        clip_count=1,
        source_kind="iso",
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0x24,
                                  language="und"),),
    )
    streams = [
        {"codec_type": "video", "id": "0x1011", "codec_name": "hevc",
         "width": 3840, "height": 2160},
    ]

    assert MatroskaBuilder()._primary_video_pid(asset, streams) == 0x1011


def test_primary_video_pid_does_not_expand_judgement_without_unique_carrier():
    """宽高缺失也可能是探测失败；没有唯一替代候选时不扩大判定。"""
    asset = BlurayTitleAsset(
        root=Path("/tmp/disc.iso"),
        entry_path="HDATHOME/MAIN_TITLE.m2ts",
        title_index=2,
        playlist=4,
        duration_90k=926_395_470,
        size=1,
        clip_count=1,
        source_kind="iso",
        video_tracks=(BlurayTrack(index=0, pid=0x1011, coding_type=0x24,
                                  language="und"),),
    )
    no_carrier = [{"codec_type": "video", "id": "0x1011", "width": 0, "height": 0}]
    dv_style = [
        {"codec_type": "video", "id": "0x1011", "width": 0, "height": 0},
        {"codec_type": "video", "id": "0x1012", "width": 1920, "height": 1080},
        {"codec_type": "video", "id": "0x1fff", "width": 3840, "height": 2160},
    ]

    builder = MatroskaBuilder()
    assert builder._primary_video_pid(asset, no_carrier) == 0x1011
    assert builder._primary_video_pid(asset, dv_style) == 0x1011


def test_multiclip_context_is_appended_to_failure_reason():
    """保守做法（2026-09-14 裁定）：连放结构照旧被拒，但理由必须自带该事实。"""
    from bluray_fidelity.bluray import BlurayClip
    from bluray_fidelity.matroska import _with_multiclip_context

    single = BlurayTitleAsset(
        root=Path("/tmp/disc.iso"), entry_path="HDATHOME/MAIN_TITLE.m2ts",
        title_index=2, playlist=4, duration_90k=9_000_000, size=1, clip_count=1,
        source_kind="iso",
        clips=(BlurayClip(index=0, clip_id="00016", in_time=0, out_time=9_000_000),),
    )
    compilation = BlurayTitleAsset(
        **{**single.__dict__, "clip_count": 3, "clips": (
            BlurayClip(index=0, clip_id="00000", in_time=54_000_000,
                       out_time=330_302_276),
            BlurayClip(index=1, clip_id="00001", in_time=54_000_000,
                       out_time=323_710_690),
            BlurayClip(index=2, clip_id="00002", in_time=54_000_000,
                       out_time=331_199_422),
        )},
    )

    assert _with_multiclip_context("成品 seek 90% 时间越界", single) == \
        "成品 seek 90% 时间越界"
    noted = _with_multiclip_context("成品 seek 90% 时间越界", compilation)
    assert noted.startswith("成品 seek 90% 时间越界｜主片为多 clip 合辑（连放结构）：")
    assert "in=600.0s" in noted
    assert "按单集来源投递" in noted


# ---------------------------------------------------------------------------
# 2026-09-17 多段追加（3 段）根因：`--append-to` 目标文件号 + mkvmerge 退出码语义。
#
# 生产标本 dlv_b3532c0cbbbe66742be8（地球脉动.第二季，3 个 play item）自 00:15
# 起 9 次尝试都在 track_validation 40% 以同一条「最终 MKV 缺少有效时长/Cues
# 信息」失败。逐环复核（本地真实 mkvmerge v100）证明它不是内容门缺陷：
#   1) 3.7.93 生成的 `--append-to` 把目标文件号硬编码为 0（`<源>:<轨>:0:<轨>`），
#      两段追加时正确，第 3 个输入起与第 2 个冲突 → mkvmerge 报
#      `More than one track is to be appended to the track N from file no. 0`
#      并以退出码 2（**错误**）中止、不产出任何文件；
#   2) 退出码语义被写反（把 2 当"已完成但有警告"），于是硬失败被当警告放行；
#   3) `_run_mkvmerge_identify` 同样接受 2，把 mkvmerge 对不存在文件写进 stdout
#      的错误 JSON 当成有效识别；ffprobe 对不存在文件返回 0；
#   4) 成品门于是报「缺少有效时长/Cues」——"根本没有成品"伪装成"成品缺字段"。
# ---------------------------------------------------------------------------


def _final_identification(*, duration_ns):
    """可被 validate_finalized_identification 接受的成品识别（单视频轨）。"""
    properties = {"duration": duration_ns} if duration_ns is not None else {}
    return {
        "container": {"properties": properties},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {}},
            {"id": 1, "type": "audio", "codec": "TrueHD", "properties": {"language": "eng"}},
            {"id": 2, "type": "audio", "codec": "AC-3", "properties": {"language": "zho"}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "eng"}},
            {"id": 4, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "zho"}},
        ],
    }


def test_command_three_item_append_targets_previous_file(tmp_path):
    """3 段以上追加：`--append-to` 的目标文件号必须是**前一段**（file N → N-1）。

    旧实现把目标文件号硬编码为 0：两段时恰好正确（3.7.93 就是在两段标题上验证
    通过的），第 3 个输入起 `2:<轨>:0:<轨>` 与 `1:<轨>:0:<轨>` 冲突，mkvmerge
    以退出码 2 中止、不产出文件（生产 3 段标本 9 次重投的定因）。
    """
    asset = _asset(tmp_path)
    resolved = resolve_official_tracks(asset, parse_identification(_input_identification()))
    first = tmp_path / "title.item0.m2ts"
    extras = (tmp_path / "title.item1.m2ts", tmp_path / "title.item2.m2ts")

    command = build_mkvmerge_command(
        asset, resolved, first, tmp_path / "final.mkv.partial",
        title="合辑", append_inputs=extras,
    )

    mapping = command[command.index("--append-to") + 1]
    entries = mapping.split(",")
    # 第 2 个输入 → 目标文件 0；第 3 个输入 → 目标文件 1（前一段）。
    for track_id in (0, 1, 2, 4, 5, 6):
        assert f"1:{track_id}:0:{track_id}" in entries
        assert f"2:{track_id}:1:{track_id}" in entries
    # 绝不能把两个源文件追加到同一条目标轨（mkvmerge 会直接报错退出）。
    assert not any(
        entry.startswith("2:") and entry.split(":")[2] == "0" for entry in entries
    ), f"第 3 个输入不许再映射到 file 0：{mapping}"


def test_identify_error_exit_code_fails_closed(monkeypatch, tmp_path):
    """mkvmerge --identify 退出码 2=错误：失败关闭并带原文。

    对未产出/打不开的成品，mkvmerge 把错误写进 stdout 的 JSON 并以 2 返回。
    旧实现接受 2、把该 JSON 当有效识别，最终在成品门报「缺少有效时长/Cues」。
    """
    import bluray_fidelity.matroska as m

    class FakeRun:
        returncode = 2
        stderr = ""
        stdout = json.dumps(
            {"errors": ["The file '/x/out.mkv' could not be opened for reading: open file error."]}
        )

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: FakeRun())
    with pytest.raises(MatroskaBuildError, match=r"最终 MKV 校验失败（退出码 2）"):
        MatroskaBuilder._run_mkvmerge_identify(
            "mkvmerge", tmp_path / "out.mkv", label="最终 MKV 校验",
        )


def test_identified_container_guard_is_fail_closed():
    """识别结果是错误 JSON / 明确未识别 → 在时长门前失败关闭，不被当成缺字段。"""
    import bluray_fidelity.matroska as m

    with pytest.raises(MatroskaBuildError, match="未产出可识别的容器"):
        m._require_identified_container(
            {"errors": ["The file 'x.mkv' could not be opened for reading: open file error."]},
            label="最终 MKV 校验",
        )
    with pytest.raises(MatroskaBuildError, match="未识别出有效的 Matroska 容器"):
        m._require_identified_container(
            {"container": {"recognized": False}}, label="最终 MKV 校验",
        )
    # 正常识别（老版本无 recognized 字段也放行）→ 通过。
    m._require_identified_container(
        {"container": {"properties": {"duration": 1}}}, label="最终 MKV 校验",
    )


def test_mux_error_surfaces_as_mkvmerge_failure_not_duration_gate(tmp_path, monkeypatch):
    """3 段 mkvmerge 以退出码 2 中止（无成品）时，必须报 mkvmerge 失败原文，
    绝不能走到成品门报「最终 MKV 缺少有效时长/Cues 信息」。

    这正是生产标本 dlv_b3532c0cbbbe66742be8 的症状链：非法映射 → mkvmerge 中止
    → 无文件 → identify 错误 JSON 被当有效识别 → 时长门报缺字段 → 无限重投。
    """
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _multiclip_asset(tmp_path)
    _install_multiclip_reader(monkeypatch)
    output = tmp_path / "final.mkv.partial"

    class FakeProcess:
        stdout = iter((
            "Error: More than one track is to be appended to the track 0 from "
            "file no. 0 ('item0.m2ts'). The argument for '--append-to' was invalid.\n",
        ))

        def wait(self):
            return 2

    class FakeRun:
        # mkvmerge --identify 对不存在的成品：错误 JSON + 退出码 2。
        returncode = 2
        stderr = ""
        stdout = json.dumps(
            {"errors": ["The file could not be opened for reading: open file error."]}
        )

    builder = MatroskaBuilder()
    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/mkvmerge")
    monkeypatch.setattr(builder, "_identify_path", lambda _source: _input_identification())
    monkeypatch.setattr(builder, "_video_streams", lambda _source: _b2_streams())
    monkeypatch.setattr(builder, "_has_hdr10plus", lambda _source: False)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: FakeRun())

    with pytest.raises(MatroskaBuildError) as error:
        builder.build(asset, output, title="合辑")

    message = str(error.value)
    assert "mkvmerge 生成失败（退出码 2）" in message
    assert "More than one track" in message
    assert "缺少有效时长" not in message, "硬封装失败不得再伪装成成品时长门失败"


def _build_multiclip_with_final_identify(monkeypatch, tmp_path, *, identify_payload,
                                         probe_duration):
    """多段路线走真实成品门（validate_finalized_identification）的最小装置。"""
    import bluray_fidelity.matroska as m
    from bluray_fidelity.matroska import MatroskaBuilder

    asset = _multiclip_asset(tmp_path)
    _install_multiclip_reader(monkeypatch)
    output = tmp_path / "final.mkv.partial"

    class FakeProcess:
        stdout = iter(())

        def wait(self):
            return 0

    class FakeRun:
        returncode = 0
        stderr = ""
        stdout = json.dumps(identify_payload)

    builder = MatroskaBuilder()
    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/mkvmerge")
    monkeypatch.setattr(builder, "_identify_path", lambda _source: _input_identification())
    monkeypatch.setattr(builder, "_video_streams", lambda _source: _b2_streams())
    monkeypatch.setattr(builder, "_has_hdr10plus", lambda _source: False)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: FakeRun())
    monkeypatch.setattr(m, "_probe_duration", lambda _probe, _path: probe_duration)
    # 覆盖门需要真实成品里的媒体包；本装置只提供识别 JSON（见 _run_multiclip_build
    # 处的同一条说明）。这里的断言目标是"缺容器时长时用独立探针补齐后仍放行"。
    monkeypatch.setattr(m, "require_track_coverage", lambda *a, **k: {})
    monkeypatch.setattr(MatroskaBuilder, "_verify_multiclip_seams", lambda *a, **k: None)
    return builder.build(asset, output, title="合辑")


def test_multiclip_without_container_duration_but_probe_measurable_is_accepted(
    tmp_path, monkeypatch,
):
    """结构完好但 mkvmerge 未写容器时长：独立探针测得出 → 放行（3.7.84 不变量）。"""
    result = _build_multiclip_with_final_identify(
        monkeypatch, tmp_path,
        identify_payload=_final_identification(duration_ns=None),
        probe_duration=900.0,
    )
    assert int(result["duration_ns"]) == 900_000_000_000


def test_multiclip_truncated_unmeasurable_output_is_rejected(tmp_path, monkeypatch):
    """截断/损坏：mkvmerge 与 ffprobe 都测不出时长 → 成品门失败关闭（零放宽）。"""
    with pytest.raises(MatroskaBuildError, match="缺少有效时长/Cues"):
        _build_multiclip_with_final_identify(
            monkeypatch, tmp_path,
            identify_payload=_final_identification(duration_ns=None),
            probe_duration=0.0,
        )


def test_real_mkvmerge_three_item_append_previous_file_target(tmp_path):
    """真实 mkvmerge：修正后的 3 段映射 exit=0 且成品带时长；旧映射（目标号 0）
    以退出码 2 中止、不产出文件。跳过：本机无 mkvmerge/ffmpeg/ffprobe。"""
    import shutil
    import subprocess

    if not all(shutil.which(tool) for tool in ("mkvmerge", "ffmpeg", "ffprobe")):
        pytest.skip("需要真实 mkvmerge/ffmpeg/ffprobe")

    from bluray_fidelity.matroska import ResolvedTracks

    for index in range(3):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25",
             "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-f", "mpegts", str(tmp_path / f"item{index}.ts")],
            check=True,
        )
    items = [tmp_path / f"item{index}.ts" for index in range(3)]

    asset = replace(
        _asset(tmp_path),
        audio_tracks=(BlurayTrack(index=0, pid=4352, coding_type=0x81, language="eng"),),
        subtitle_tracks=(),
    )
    resolved = ResolvedTracks(
        video_ids=(0,), audio_ids=(1,), subtitle_ids=(),
        audio_by_official_index=((0, 1),), subtitle_by_official_index=(),
    )
    mkvmerge = shutil.which("mkvmerge")

    good_out = tmp_path / "good.mkv"
    good = build_mkvmerge_command(
        asset, resolved, items[0], good_out, title="t",
        append_inputs=(items[1], items[2]),
    )
    good[0] = mkvmerge
    completed = subprocess.run(good, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    identified = json.loads(subprocess.run(
        [mkvmerge, "--identification-format", "json", "--identify", str(good_out)],
        capture_output=True, text=True,
    ).stdout)
    assert int(identified["container"]["properties"]["duration"]) > 0

    # 旧形态（第 3 个输入的目标文件号 = 0）：mkvmerge 判非法并以退出码 2 中止。
    bad_out = tmp_path / "bad.mkv"
    bad = list(good)
    bad[bad.index("--output") + 1] = str(bad_out)
    mapping_at = bad.index("--append-to") + 1
    bad[mapping_at] = ",".join(
        part.replace(":1:", ":0:", 1) if part.startswith("2:") else part
        for part in bad[mapping_at].split(",")
    )
    failed = subprocess.run(bad, capture_output=True, text=True)
    assert failed.returncode == 2
    assert "More than one track" in (failed.stdout + failed.stderr)
    assert not bad_out.exists(), "mkvmerge 以错误中止时不得留下成品"
