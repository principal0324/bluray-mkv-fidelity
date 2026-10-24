"""E1 终化成品轨覆盖门（2026-09-17 Léon 事故的系统级防线）。

事故形态：《这个杀手不太冷》终化成品**声明了全部 13 条字幕轨、轨道计数与语言
逐条正确、容器时长也正确**，但字幕内容只覆盖到 2,608s / 7,978s（前 33%）——
第 2 个 play item 之后整段消失。既有的 `validate_finalized_identification` 只
比对**数量与语言**，数量相等即放行，因此这道缺陷在系统里完全不可见。

本文件分两层：
- 纯逻辑层（不依赖外部工具）：采样计划、未覆盖空档、逐轨事实折算；
- 真实工具层（本机 mkvmerge/ffmpeg/ffprobe，缺失即 skip）：用真实 mkvmerge
  构造"声明轨数相同、时长相同、仅覆盖不同"的两份成品，证明本门**能分辨
  计数门分辨不了的那一半**——这正是 Léon 的形状。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bluray_fidelity.matroska import (
    CoverageWindow,
    IdentifiedTrack,
    MatroskaBuildError,
    _COVERAGE_SUBTITLE_MAX_GAP_SECONDS,
    _probe_source_subtitle_evidence,
    _probe_source_subtitle_presence,
    _probe_window_presence,
    coverage_windows,
    dense_sample_windows,
    longest_uncovered_gap,
    longest_uncovered_packet_gap,
    require_track_coverage,
    summarise_track_coverage,
)


def _track(position: int, kind: str, *, codec: str = "SubRip/SRT") -> IdentifiedTrack:
    return IdentifiedTrack(
        id=position, type=kind, codec=codec, stream_id=None,
        language="und", multiplexed_tracks=(),
    )


# ── 纯逻辑层 ────────────────────────────────────────────────────────────


def test_coverage_windows_step_plus_window_equals_max_gap():
    """检出保证的前提：常规步长 + 窗宽 == max_gap（否则「任何 > max_gap 的空档
    必被完整窗口命中「不成立）。末尾那个贴尾窗口是唯一的例外（步长收窄）。"""
    windows = coverage_windows(7978.0, max_gap=600.0, window=60.0)
    steps = {round(b.start - a.start, 6) for a, b in zip(windows, windows[1:])}
    # 倒数第二步到贴尾窗口可以更短（收窄只会让检出更容易，不会漏检）
    assert steps - {round(windows[-1].start - windows[-2].start, 6)} == {540.0}
    assert 540.0 + 60.0 == 600.0
    assert windows[-1].start - windows[-2].start <= 540.0


def test_coverage_windows_span_the_film_and_anchor_the_tail():
    """采样必须铺满全片，且末尾窗口贴住影片结尾——它是「覆盖到接近结尾」的锚点。"""
    windows = coverage_windows(7978.0, max_gap=600.0, window=60.0)
    assert windows[0].start == 0.0
    assert windows[-1].end == pytest.approx(7978.0)
    assert windows[-1].length == pytest.approx(60.0)
    for previous, following in zip(windows, windows[1:]):
        assert following.start >= previous.end


def test_coverage_windows_reject_illegal_sampling_parameters():
    """window >= max_gap 会让窗口重叠到失去检出保证，必须拒绝而不是默默放行。"""
    with pytest.raises(MatroskaBuildError):
        coverage_windows(1000.0, max_gap=60.0, window=60.0)
    with pytest.raises(MatroskaBuildError):
        coverage_windows(0.0, max_gap=600.0, window=60.0)


def test_any_gap_longer_than_max_gap_is_detected():
    """检出保证的穷举验证：任意起点、任意长度 > max_gap 的空档，都必定完整
    包含至少一个采样窗口——即「字幕在某处整段消失」不可能漏检。"""
    duration = 5000.0
    max_gap, window = 600.0, 60.0
    windows = coverage_windows(duration, max_gap=max_gap, window=window)
    for gap_length in (max_gap + 1.0, max_gap + 60.0, max_gap + 300.0, 3000.0):
        for start in range(0, 4400, 13):
            gap = (float(start), float(start) + gap_length)
            covered = any(
                w.start >= gap[0] and w.end <= gap[1] for w in windows
            )
            assert covered, f"空档 {gap} 未被任何窗口完整命中"


def test_longest_uncovered_gap_counts_head_tail_and_unobserved_spans():
    """空档口径：**未被观测到 = 未覆盖**。

    采样窗口之间的空白区同样计入未覆盖空档（我们没在那里看过，就不能声称
    它被覆盖）。这条口径有两层作用：
      1. 首尾也计入 → 字幕"中途消失到片尾"得到 (片尾 − 最后在场) 的大空档，
         不会被当成"结尾没有对白"放过；
      2. 健康轨的常规空档 = step − window = 480s < 600s 上限 → 正常放行；
         一旦某个窗口整段缺席，空档就跨过相邻窗口累加到 > 600s → 失败关闭。
    """
    windows = (CoverageWindow(0.0, 60.0), CoverageWindow(540.0, 600.0),
               CoverageWindow(1080.0, 1140.0))
    # 只有第一个窗口在场 → 空档从 60s 一直到片尾（片中消失）
    gap, span = longest_uncovered_gap((True, False, False), windows, 1140.0)
    assert gap == pytest.approx(1080.0)
    assert span == (60.0, 1140.0)
    # 全程缺席 → 整片都是空档
    gap, span = longest_uncovered_gap((False, False, False), windows, 1140.0)
    assert gap == pytest.approx(1140.0)
    assert span == (0.0, 1140.0)
    # 全程在场 → 空档只剩"窗口之间的未观测区"，两段都是 480s（在 600s 上限内）
    gap, span = longest_uncovered_gap((True, True, True), windows, 1140.0)
    assert gap == pytest.approx(480.0)
    assert span == (60.0, 540.0)


def test_verdict_fires_for_every_truncation_shape():
    """检出保证的**判据级**验证：空档出现在开头/中间/结尾，三种形状都必然
    让「最长未覆盖空档 > max_gap"成立——不存在能绕过判据的截断位置。"""
    duration = 4000.0
    max_gap, window = 600.0, 60.0
    windows = coverage_windows(duration, max_gap=max_gap, window=window)

    def presence_for(gap: tuple[float, float]) -> tuple[bool, ...]:
        return tuple(
            not (w.start >= gap[0] and w.end <= gap[1]) for w in windows
        )

    for gap in ((0.0, 700.0), (1500.0, 2400.0), (3300.0, 4000.0)):
        gap_seconds, _ = longest_uncovered_gap(
            presence_for(gap), windows, duration)
        assert gap_seconds > max_gap, f"空档 {gap} 未被判据捕获"


def test_summarise_track_coverage_maps_windows_to_declared_tracks():
    """ffprobe 流序号与 mkvmerge 轨道声明按容器顺序一一对应。"""
    tracks = (_track(0, "video", codec="AVC/H.264"), _track(1, "audio", codec="AAC"),
              _track(2, "subtitles"))
    windows = (CoverageWindow(0.0, 60.0), CoverageWindow(540.0, 600.0))
    presence = (frozenset({0, 1, 2}), frozenset({0, 1}))
    facts = summarise_track_coverage(tracks, presence, windows, 600.0)
    assert [fact.kind for fact in facts] == ["video", "audio", "subtitles"]
    # 视频/音频两个窗口都在场；字幕第二个窗口缺席 → 空档 = 60s→600s
    assert facts[0].windows_present == 2
    assert facts[2].windows_present == 1
    assert facts[2].last_seen == pytest.approx(60.0)
    assert facts[2].longest_gap == pytest.approx(540.0)


def test_require_track_coverage_fails_closed_on_subtitle_truncation(tmp_path):
    """Léon 形状的**纯逻辑**复现：轨数/时长都对，只有字幕后半段消失。

    断言门失败、点名到具体轨、并带上空档区间——可运维、可诊断。
    """
    media = tmp_path / "placeholder.mkv"
    media.write_bytes(b"")
    tracks = tuple([_track(0, "video", codec="AVC/H.264")]
                   + [_track(index, "subtitles") for index in range(1, 14)])
    duration = 7978.0
    windows = coverage_windows(duration, max_gap=_COVERAGE_SUBTITLE_MAX_GAP_SECONDS,
                               window=60.0)
    # 字幕只覆盖到 2608s：头 5 个窗口在场，之后全部缺席
    presence = []
    for window in windows:
        seen = {0}
        if window.end <= 2641.0:
            seen.update(range(1, 14))
        presence.append(frozenset(seen))
    facts = summarise_track_coverage(tracks, tuple(presence), windows, duration)
    # 字幕轨最后在场的窗口是 [2160,2220)，之后到 7978s 全是空档 → 5,758s
    assert facts[1].longest_gap > _COVERAGE_SUBTITLE_MAX_GAP_SECONDS
    assert facts[1].last_seen == pytest.approx(2220.0)
    assert facts[1].longest_gap == pytest.approx(duration - 2220.0)
    # 视频轨全程在场 → 空档只剩窗口间未观测区（step − window = 480s），在 600s 上限内
    assert facts[0].windows_present == len(windows)
    assert facts[0].longest_gap == pytest.approx(480.0)
    assert facts[0].longest_gap <= _COVERAGE_SUBTITLE_MAX_GAP_SECONDS


def test_source_backed_sparse_subtitle_gaps_pass_but_source_packet_loss_fails(
        tmp_path, monkeypatch):
    """稀疏源字幕自身的长空窗可验证放行；输出少了源侧在场窗仍失败。"""
    media = tmp_path / "placeholder.mkv"
    media.write_bytes(b"")
    tracks = (_track(0, "video", codec="AVC/H.264"), _track(1, "subtitles"))
    duration = 5000.0
    sparse_windows = coverage_windows(
        duration, max_gap=_COVERAGE_SUBTITLE_MAX_GAP_SECONDS, window=60.0)
    # 源字幕在片头、第二窗及片尾有包；源侧自身的空窗超过 600 秒。
    source_hits = {0, 1, len(sparse_windows) - 1}
    source_packet_times = (10.0, 550.0, duration - 10.0)

    def install_output_hits(missing_source_window=None):
        output_hits = source_hits - ({missing_source_window}
                                     if missing_source_window is not None else set())
        calls = 0

        def probe(_probe, _path, windows, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:  # sparse subtitle plan
                return tuple(frozenset({0, 1} if i in output_hits else {0})
                             for i in range(len(windows)))
            # dense continuous-track plan; both declared tracks present throughout.
            return tuple(frozenset({0, 1}) for _ in windows)

        monkeypatch.setattr(
            "bluray_fidelity.matroska._probe_window_presence", probe)

    install_output_hits()
    result = require_track_coverage(
        "ffprobe", media, tracks, duration=duration, label="最终 MKV 校验",
        source_presence_by_window=tuple(
            frozenset({0, 1} if i in source_hits else {0})
            for i in range(len(sparse_windows))),
        source_packet_timestamps_by_stream={1: source_packet_times},
    )
    assert result["source_presence_verified"] is True
    subtitle = next(row for row in result["tracks"] if row["type"] == "subtitles")
    assert subtitle["source_proven_windows"] == len(source_hits)
    assert subtitle["longest_gap_seconds"] > _COVERAGE_SUBTITLE_MAX_GAP_SECONDS

    install_output_hits(missing_source_window=1)
    with pytest.raises(MatroskaBuildError, match="源侧在场窗缺失"):
        require_track_coverage(
            "ffprobe", media, tracks, duration=duration, label="最终 MKV 校验",
            source_presence_by_window=tuple(
                frozenset({0, 1} if i in source_hits else {0})
                for i in range(len(sparse_windows))),
            source_packet_timestamps_by_stream={1: source_packet_times},
        )


def test_packet_timestamp_gap_does_not_overestimate_between_sparse_windows():
    """复盘提醒：命中窗之间仍可能有 cue，不能把窗口间距当成真实源空档。"""
    duration = 2000.0
    packet_times = (10.0, 500.0, 1080.0, 1600.0, 1990.0)
    gap, span = longest_uncovered_packet_gap(packet_times, duration)
    assert gap == pytest.approx(580.0)
    assert span == pytest.approx((500.0, 1080.0))


def test_packet_level_source_gap_prevents_false_sparse_gap_exemption(
        tmp_path, monkeypatch):
    """窗口漏掉 500s cue 时，实际 580s 源空档不能被估成 1020s 后豁免。"""
    media = tmp_path / "placeholder.mkv"
    media.write_bytes(b"")
    tracks = (_track(0, "video", codec="AVC/H.264"), _track(1, "subtitles"))
    duration = 2000.0
    windows = coverage_windows(duration, max_gap=600.0, window=60.0)
    packet_times = (10.0, 500.0, 1080.0, 1600.0, 1990.0)
    source_hits = {0, 2, 4}  # 500s packet falls between the 0–60 and 540–600 windows.

    def probe(_probe, _path, requested_windows, **_kwargs):
        if len(requested_windows) == len(windows):
            return tuple(frozenset({0, 1} if i in source_hits else {0})
                         for i in range(len(requested_windows)))
        return tuple(frozenset({0, 1}) for _ in requested_windows)

    monkeypatch.setattr(
        "bluray_fidelity.matroska._probe_window_presence", probe)
    source_presence = tuple(
        frozenset({0, 1} if i in source_hits else {0})
        for i in range(len(windows)))
    with pytest.raises(MatroskaBuildError, match="源侧实测最长空档 580.0s"):
        require_track_coverage(
            "ffprobe", media, tracks, duration=duration, label="最终 MKV 校验",
            source_presence_by_window=source_presence,
            source_packet_timestamps_by_stream={1: packet_times},
        )


def test_source_packet_timestamps_map_each_segment_to_appended_timeline(monkeypatch):
    """完整源 packet PTS 以实测分段时长累计，包含后续段的 cue。"""
    from types import SimpleNamespace

    durations = {"first.mkv": 700.0, "second.mkv": 700.0}
    packet_rows = {
        "first.mkv": "2,20.000\n2,650.000\n",
        "second.mkv": "2,10.000\n2,690.000\n",
    }

    def run(command, **_kwargs):
        name = Path(command[-1]).name
        if "format=duration" in command:
            return SimpleNamespace(stdout=json.dumps({
                "format": {"duration": str(durations[name])},
            }))
        assert "-show_packets" in command
        return SimpleNamespace(stdout=packet_rows[name])

    monkeypatch.setattr("bluray_fidelity.matroska.subprocess.run", run)
    windows = coverage_windows(1400.0, max_gap=600.0, window=60.0)
    evidence = _probe_source_subtitle_evidence(
        "ffprobe", (Path("first.mkv"), Path("second.mkv")), windows)
    assert evidence.packet_timestamps_by_stream[2] == pytest.approx(
        (20.0, 650.0, 710.0, 1390.0))
    assert 2 in evidence.presence_by_window[0]
    assert 2 in evidence.presence_by_window[-1]


# ── 真实工具层（本机 mkvmerge/ffmpeg/ffprobe） ──────────────────────────


def _require_real_tools() -> None:
    missing = [tool for tool in ("mkvmerge", "ffmpeg", "ffprobe")
               if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"本机缺少真实媒体工具：{','.join(missing)}")


_SUBTITLE_TRACK_COUNT = 3


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, (
        f"命令失败（{completed.returncode}）：{' '.join(command)}\n"
        f"{completed.stdout}\n{completed.stderr}"
    )


def _write_cues(path: Path, duration: float, tag: str) -> None:
    """每 5 秒一条 cue，覆盖 [0, duration)；空档因此可控。"""
    lines: list[str] = []
    index = 1
    stamp = 0.0
    while stamp < duration - 1:
        start_seconds = int(stamp)
        end_seconds = int(stamp) + 1
        lines.append(str(index))
        lines.append(f"00:{start_seconds // 60:02d}:{start_seconds % 60:02d},000"
                     f" --> 00:{end_seconds // 60:02d}:{end_seconds % 60:02d},900")
        lines.append(f"{tag} at {start_seconds}s")
        lines.append("")
        index += 1
        stamp += 5
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_segment(directory: Path, name: str, *, seconds: float,
                   subtitles: int) -> Path:
    """用真实 ffmpeg + mkvmerge 造一段媒体；subtitles 条数可调。"""
    video = directory / f"{name}.v.mp4"
    audio = directory / f"{name}.a.m4a"
    _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
          "-i", f"color=c=black:size=160x120:rate=5:duration={seconds:.0f}",
          "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
          "-pix_fmt", "yuv420p", "-an", str(video)])
    _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
          "-i", f"sine=frequency=440:duration={seconds:.0f}",
          "-c:a", "aac", "-b:a", "16k", "-ac", "1", str(audio)])
    subtitle_files = []
    for index in range(subtitles):
        path = directory / f"{name}.s{index}.srt"
        _write_cues(path, seconds, f"SUB{index}")
        subtitle_files.append(str(path))
    segment = directory / f"{name}.mkv"
    _run(["mkvmerge", "-q", "-o", str(segment), "--no-audio",
          str(video), str(audio), *subtitle_files])
    return segment


def _declared_tracks(probe_file: Path) -> tuple[IdentifiedTrack, ...]:
    completed = subprocess.run(["mkvmerge", "-J", str(probe_file)],
                               capture_output=True, text=True, check=True)
    from bluray_fidelity.matroska import parse_identification
    return parse_identification(json.loads(completed.stdout))


def _duration(path: Path) -> float:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(completed.stdout.strip())


def _last_subtitle_pts(path: Path) -> dict[int, float]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "packet=stream_index,pts_time", "-of", "csv=p=0",
         str(path)],
        capture_output=True, text=True, check=True)
    last: dict[int, float] = {}
    for line in completed.stdout.splitlines():
        fields = line.split(",")
        if len(fields) < 2:
            continue
        try:
            index, stamp = int(fields[0]), float(fields[1])
        except ValueError:
            continue
        last[index] = max(last.get(index, 0.0), stamp)
    return last


def _append_without_subtitles(first: Path, second: Path, output: Path) -> None:
    """把第二段**只映射视频+音频**追加到第一段。

    这是产生 Léon 形状的**真实 mkvmerge 行为**：成品仍然声明第一段的全部
    字幕轨、容器时长也正确，但字幕内容停在第一段末尾，mkvmerge 退出码为 0、
    不报错、不警告——"命令成功"在这里完全不构成正确证据。
    """
    _run(["mkvmerge", "-q", "-o", str(output),
          "--append-to", "1:0:0:0,1:1:0:1", str(first), f"+{second}"])


def _append_all_tracks(first: Path, second: Path, output: Path) -> None:
    mapping = ",".join(
        f"1:{index}:0:{index}"
        for index in range(2 + _SUBTITLE_TRACK_COUNT))
    _run(["mkvmerge", "-q", "-o", str(output),
          "--append-to", mapping, str(first), f"+{second}"])


@pytest.mark.parametrize("segment_seconds", [700.0])
def test_real_like_gold_case_truncated_subtitles_fail_closed(tmp_path, segment_seconds):
    """**Léon gold case（真实 mkvmerge 复现）**：成品完整声明 5 条轨（含 3 条
    字幕）、容器时长 1400s 全部正确，唯独字幕内容停在 695s——门必须失败关闭。

    与"正确成品"逐项对照（同一测试文件的 counter-case）证明：**两份成品的
    轨数与时长逐字相同**，计数型门不可能分辨，只有覆盖门能。
    """
    _require_real_tools()
    first = _build_segment(tmp_path, "seg_a", seconds=segment_seconds,
                           subtitles=_SUBTITLE_TRACK_COUNT)
    tail = _build_segment(tmp_path, "seg_b", seconds=segment_seconds, subtitles=0)
    truncated = tmp_path / "out_truncated.mkv"
    _append_without_subtitles(first, tail, truncated)

    tracks = _declared_tracks(truncated)
    duration = _duration(truncated)
    # 前置事实：计数与时长都是"对的"——旧门在这里放行
    assert len(tracks) == 2 + _SUBTITLE_TRACK_COUNT
    assert sum(1 for track in tracks if track.type == "subtitles") == _SUBTITLE_TRACK_COUNT
    assert duration == pytest.approx(2 * segment_seconds, abs=2.0)
    measured = _last_subtitle_pts(truncated)
    assert measured and max(measured.values()) < segment_seconds + 5.0, (
        "前置事实不成立：本复现里字幕本应停在第一段末尾"
    )

    with pytest.raises(MatroskaBuildError) as failure:
        require_track_coverage("ffprobe", truncated, tracks,
                               duration=duration, label="最终 MKV 校验")
    message = str(failure.value)
    # 点名到字幕轨与空档，且带上逐轨实测事实（可运维、可诊断）
    assert "轨覆盖门失败" in message
    assert "字幕" in message or "subtitles" in message
    assert "最长未覆盖空档" in message


def test_real_like_counter_case_complete_subtitles_pass(tmp_path):
    """counter-case：同样 5 条轨、同样 1400s 时长，字幕覆盖到片尾 → 必须放行。

    与上面成对：证明本门判的是**覆盖**，不是"字幕天生可疑"。
    """
    _require_real_tools()
    first = _build_segment(tmp_path, "seg_a", seconds=700.0,
                           subtitles=_SUBTITLE_TRACK_COUNT)
    whole = _build_segment(tmp_path, "seg_full", seconds=700.0,
                           subtitles=_SUBTITLE_TRACK_COUNT)
    complete = tmp_path / "out_complete.mkv"
    _append_all_tracks(first, whole, complete)

    tracks = _declared_tracks(complete)
    duration = _duration(complete)
    assert len(tracks) == 2 + _SUBTITLE_TRACK_COUNT
    assert duration == pytest.approx(1400.0, abs=2.0)
    measured = _last_subtitle_pts(complete)
    assert max(measured.values()) > 1390.0, "前置事实不成立：字幕本应覆盖到片尾"

    result = require_track_coverage("ffprobe", complete, tracks,
                                    duration=duration, label="最终 MKV 校验")
    assert result["duration_seconds"] == pytest.approx(duration, abs=1.0)
    subtitles = [entry for entry in result["tracks"] if entry["type"] == "subtitles"]
    assert len(subtitles) == _SUBTITLE_TRACK_COUNT
    for entry in subtitles:
        assert entry["longest_gap_seconds"] <= _COVERAGE_SUBTITLE_MAX_GAP_SECONDS


def test_real_source_presence_maps_appended_segment_timeline(tmp_path):
    """真实 ffprobe 包事实按追加顺序映射后，成品须保留所有源侧在场窗。"""
    _require_real_tools()
    first = _build_segment(tmp_path, "source_first", seconds=700.0,
                           subtitles=1)
    second = _build_segment(tmp_path, "source_second", seconds=700.0,
                            subtitles=1)
    output = tmp_path / "source_mapped_output.mkv"
    _run(["mkvmerge", "-q", "-o", str(output),
          "--append-to", "1:0:0:0,1:1:0:1,1:2:0:2", str(first), f"+{second}"])
    tracks = _declared_tracks(output)
    duration = _duration(output)
    windows = coverage_windows(
        duration, max_gap=_COVERAGE_SUBTITLE_MAX_GAP_SECONDS, window=60.0)

    source_presence = _probe_source_subtitle_presence(
        "ffprobe", (first, second), windows)
    output_presence = _probe_window_presence("ffprobe", output, windows)
    subtitle_positions = {i for i, track in enumerate(tracks)
                          if track.type == "subtitles"}
    assert len(subtitle_positions) == 1
    for index, source_seen in enumerate(source_presence):
        assert (source_seen & subtitle_positions) <= output_presence[index], (
            f"采样窗 {windows[index]} 丢失源侧字幕包："
            f"source={source_seen}, output={output_presence[index]}"
        )


def test_real_declared_track_count_cannot_distinguish_the_two_variants(tmp_path):
    """本门存在的理由：**计数型判据对这两份成品完全无差别**。

    同一段真实媒体，追加时映射全部字幕轨 vs 不映射字幕轨 → 两份成品的
    轨道条数、轨道种类、容器时长逐项相同，唯有字幕覆盖不同。因此"成品识别
    显示 13 条字幕轨「永远不能证明」字幕在里面"——这正是 Léon 事故能穿过
    既有全部校验的原因，也是本门必须亲自测量覆盖的依据。
    """
    _require_real_tools()
    first = _build_segment(tmp_path, "seg_a", seconds=120.0,
                           subtitles=_SUBTITLE_TRACK_COUNT)
    same = _build_segment(tmp_path, "seg_full", seconds=120.0,
                          subtitles=_SUBTITLE_TRACK_COUNT)
    plain = _build_segment(tmp_path, "seg_plain", seconds=120.0, subtitles=0)
    truncated = tmp_path / "cmp_truncated.mkv"
    complete = tmp_path / "cmp_complete.mkv"
    _append_without_subtitles(first, plain, truncated)
    _append_all_tracks(first, same, complete)

    truncated_tracks = _declared_tracks(truncated)
    complete_tracks = _declared_tracks(complete)
    assert [(t.type, t.codec) for t in truncated_tracks] == \
           [(t.type, t.codec) for t in complete_tracks]
    assert _duration(truncated) == pytest.approx(_duration(complete), abs=0.5)
    # 计数/时长完全一致，覆盖才是唯一差别
    assert max(_last_subtitle_pts(truncated).values()) < 125.0
    assert max(_last_subtitle_pts(complete).values()) > 230.0


def test_real_expected_end_override_must_be_supplied_explicitly(tmp_path):
    """源侧事实证明的例外只能**显式传入**并随结果留痕；默认期望终点是片尾。

    override 的键是**轨道位置**（与 ffprobe 流序号一致）。未列入 override 的
    轨不受影响——例外不得顺带放宽别的轨。
    """
    _require_real_tools()
    first = _build_segment(tmp_path, "seg_a", seconds=700.0,
                           subtitles=_SUBTITLE_TRACK_COUNT)
    tail = _build_segment(tmp_path, "seg_b", seconds=700.0, subtitles=0)
    truncated = tmp_path / "out_override.mkv"
    _append_without_subtitles(first, tail, truncated)

    tracks = _declared_tracks(truncated)
    duration = _duration(truncated)
    # 不带 override → 失败关闭（默认口径：所有轨都必须覆盖到片尾）
    with pytest.raises(MatroskaBuildError):
        require_track_coverage("ffprobe", truncated, tracks,
                               duration=duration, label="最终 MKV 校验")
    override = {position: 700.0
                for position, track in enumerate(tracks)
                if track.type == "subtitles"}
    result = require_track_coverage("ffprobe", truncated, tracks,
                                    duration=duration, label="最终 MKV 校验",
                                    expected_end_by_stream=override)
    shortened = [entry for entry in result["tracks"]
                 if entry["type"] == "subtitles"]
    assert shortened and all(
        entry["expected_end_seconds"] == pytest.approx(700.0)
        for entry in shortened)
    untouched = [entry for entry in result["tracks"]
                 if entry["type"] != "subtitles"]
    assert untouched and all(
        entry["expected_end_seconds"] == pytest.approx(duration, abs=1.0)
        for entry in untouched)


def test_dense_sample_windows_cover_head_and_tail():
    """连续轨抽点：必须覆盖片头与片尾各一个窗口（首尾都在场才叫连续）。"""
    windows = dense_sample_windows(1800.0, count=12, window=60.0)
    assert windows[0].start == 0.0
    assert windows[-1].end == pytest.approx(1800.0)
    assert len(windows) == 13  # 12 个均匀点 + 贴尾窗口
    for previous, following in zip(windows, windows[1:]):
        assert following.start >= previous.start


# ── 用户硬约束（2026-09-17）：单碟电影零影响 ────────────────────────────


def test_single_disc_path_is_evidence_mode_not_blocking(tmp_path):
    """**单碟零影响**：`enforce=False` 时照常测量、照常给出逐轨事实与违规项，
    但**不抛异常**——母盘上本来就可能存在只覆盖局部的字幕轨。

    这是部署安全性的关键性质：多段合辑路线（Léon 事故所在）有逐段轨道对应证明，
    因而失败关闭；单段/单碟路线没有那层证明，只能先取证。
    """
    from bluray_fidelity.matroska import (
        CoverageWindow, longest_uncovered_gap, summarise_track_coverage,
    )

    tracks = tuple([_track(0, "video", codec="AVC/H.264")]
                   + [_track(index, "subtitles") for index in range(1, 4)])
    duration = 3600.0
    windows = coverage_windows(duration,
                               max_gap=_COVERAGE_SUBTITLE_MAX_GAP_SECONDS,
                               window=60.0)
    # 字幕只在前 1/4 有内容（模拟"只有局部字幕"的母盘结构）
    presence = []
    for window in windows:
        seen = {0}
        if window.end <= duration / 4:
            seen.update((1, 2, 3))
        presence.append(frozenset(seen))
    facts = summarise_track_coverage(tracks, tuple(presence), windows, duration)
    gap, _span = longest_uncovered_gap(
        tuple(1 in seen for seen in presence), windows, duration)
    assert gap > _COVERAGE_SUBTITLE_MAX_GAP_SECONDS, "前置事实：这是「局部字幕」形状"

    # 同一批事实在 enforce=False 下必须仍然产出违规项（不静默），只是不阻断
    result_facts = [{"violation": f.longest_gap > _COVERAGE_SUBTITLE_MAX_GAP_SECONDS}
                    for f in facts]
    assert any(item["violation"] for item in result_facts), (
        "取证模式必须照常产出可诊断事实——静默通过正是本次事故的成因")


def test_real_evidence_mode_does_not_block_truncated_subtitles(tmp_path):
    """真实媒体：同一份「字幕截断」成品，`enforce=True` 失败关闭、
    `enforce=False` 放行但把违规项与逐轨事实写进结果。"""
    _require_real_tools()
    first = _build_segment(tmp_path, "seg_a", seconds=700.0,
                           subtitles=_SUBTITLE_TRACK_COUNT)
    tail = _build_segment(tmp_path, "seg_b", seconds=700.0, subtitles=0)
    truncated = tmp_path / "ev_mode.mkv"
    _append_without_subtitles(first, tail, truncated)

    tracks = _declared_tracks(truncated)
    duration = _duration(truncated)
    with pytest.raises(MatroskaBuildError):
        require_track_coverage("ffprobe", truncated, tracks,
                               duration=duration, label="最终 MKV 校验")
    result = require_track_coverage("ffprobe", truncated, tracks,
                                    duration=duration, label="最终 MKV 校验",
                                    enforce=False)
    assert result["enforced"] is False
    assert result["violations"], "取证模式必须留下违规项，不得静默通过"
    assert any("空档" in item for item in result["violations"])
    subtitles = [e for e in result["tracks"] if e["type"] == "subtitles"]
    assert subtitles and all(e["last_seen"] is not None for e in subtitles)

# ---- 3.7.137/3.7.138 王朝形态：非 DV 多段路线的源侧稀疏豁免前置 ----

from types import SimpleNamespace


def _selection(v=(), a=(), s=()):
    return SimpleNamespace(video_ids=tuple(v), audio_ids=tuple(a),
                           subtitle_ids=tuple(s))


def _declared(*types):
    return tuple(_track(index, kind) for index, kind in enumerate(types))


def _fake_type_probe(lines_by_path, fail_paths=()):
    def run(command, **kwargs):
        path = command[-1]
        if path in fail_paths:
            raise subprocess.SubprocessError("probe failed")
        return subprocess.CompletedProcess(
            command, 0,
            stdout="".join(line + "\n" for line in lines_by_path[path]),
            stderr="")
    return run


def test_mapping_resolves_with_extra_ts_streams(monkeypatch, tmp_path):
    """裁切 TS 含 MPLS 未引用的额外流（如 data）时映射仍可证明。"""
    from bluray_fidelity import matroska as mk
    seg1 = tmp_path / "a.trimmed.ts"; seg1.write_bytes(b"")
    seg2 = tmp_path / "b.trimmed.ts"; seg2.write_bytes(b"")
    layout = ["video", "audio", "audio", "subtitle", "subtitle", "data"]
    monkeypatch.setattr(mk.subprocess, "run", _fake_type_probe(
        {str(seg1): layout, str(seg2): layout}))
    resolved = _selection(v=(0,), a=(1, 2), s=(3, 4))
    declared = _declared("video", "audio", "audio", "subtitles", "subtitles")
    assert mk._resolve_segment_track_mapping(
        "ffprobe", (seg1, seg2), resolved, declared) == {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}


def test_mapping_rejects_type_mismatch_in_selection(monkeypatch, tmp_path):
    from bluray_fidelity import matroska as mk
    seg = tmp_path / "a.trimmed.ts"; seg.write_bytes(b"")
    # 选择把 3 号（audio）当字幕轨 → 类型失配必须拒绝
    monkeypatch.setattr(mk.subprocess, "run", _fake_type_probe(
        {str(seg): ["video", "audio", "audio", "audio", "subtitle"]}))
    resolved = _selection(v=(0,), a=(1, 2), s=(3,))
    declared = _declared("video", "audio", "audio", "subtitles")
    assert mk._resolve_segment_track_mapping(
        "ffprobe", (seg,), resolved, declared) is None


def test_mapping_rejects_probe_failure_and_inconsistent_segments(
        monkeypatch, tmp_path):
    from bluray_fidelity import matroska as mk
    seg1 = tmp_path / "a.trimmed.ts"; seg1.write_bytes(b"")
    seg2 = tmp_path / "b.trimmed.ts"; seg2.write_bytes(b"")
    resolved = _selection(v=(0,), a=(1,), s=(2,))
    declared = _declared("video", "audio", "subtitles")
    good = ["video", "audio", "subtitle"]
    monkeypatch.setattr(mk.subprocess, "run", _fake_type_probe(
        {str(seg1): good}, fail_paths={str(seg2)}))
    assert mk._resolve_segment_track_mapping(
        "ffprobe", (seg1, seg2), resolved, declared) is None
    monkeypatch.setattr(mk.subprocess, "run", _fake_type_probe(
        {str(seg1): good, str(seg2): ["video", "subtitle", "audio"]}))
    assert mk._resolve_segment_track_mapping(
        "ffprobe", (seg1, seg2), resolved, declared) is None


def test_mapping_rejects_selection_count_mismatch():
    from bluray_fidelity import matroska as mk
    resolved = _selection(v=(0,), a=(1,), s=(2, 3))
    declared = _declared("video", "audio", "subtitles")
    assert mk._resolve_segment_track_mapping(
        "ffprobe", (), resolved, declared) is None


def test_dynasty_shape_sparse_source_backed_subtitle_passes(tmp_path, monkeypatch):
    """王朝形态端到端：三段每段头部 ~280s 有包、其后全空（源侧空档 >600s）。

    映射 1:1（v+a+s 序列一致）⇒ 流序号==轨位置；源侧完整 packet 时间戳证明
    自身稀疏 ⇒ 豁免放行（不 raise），并在结果里留 source_max_gap_seconds。
    """
    tracks = (_track(0, "video", codec="HEVC/H.265"),
              _track(1, "audio", codec="TrueHD Atmos"),
              _track(2, "subtitles"))
    media = tmp_path / "final.mkv"
    media.write_bytes(b"")
    segment_seconds = 3146.5  # 9439.5 / 3
    duration = segment_seconds * 3
    sparse_windows = coverage_windows(
        duration, max_gap=_COVERAGE_SUBTITLE_MAX_GAP_SECONDS, window=60.0)
    # 源 packet：每段头部 5.4–256s 有簇（映射到追加时间轴），其后全空
    offsets = (0.0, segment_seconds, segment_seconds * 2)
    packet_times = []
    for offset in offsets:
        packet_times.extend(offset + t for t in (5.4, 60.0, 120.0, 240.0, 255.9))
    packet_times = tuple(sorted(packet_times))
    source_hits = {
        index for index, window in enumerate(sparse_windows)
        if any(window.start - 0.5 <= t <= window.end + 0.5 for t in packet_times)}
    assert len(source_hits) < len(sparse_windows), "源侧必须呈稀疏在场"

    def probe(_probe, _path, windows, **_kwargs):
        if len(windows) == len(sparse_windows):
            return tuple(
                frozenset({0, 1} | ({2} if index in source_hits else set()))
                for index in range(len(windows)))
        return tuple(frozenset({0, 1}) for _ in windows)

    monkeypatch.setattr(
        "bluray_fidelity.matroska._probe_window_presence", probe)
    result = require_track_coverage(
        "ffprobe", media, tracks, duration=duration, label="最终 MKV 校验",
        source_presence_by_window=tuple(
            frozenset({0, 1} | ({2} if index in source_hits else set()))
            for index in range(len(sparse_windows))),
        source_packet_timestamps_by_stream={2: packet_times},
    )
    assert result["violations"] == []
    subtitle = next(row for row in result["tracks"] if row["type"] == "subtitles")
    assert subtitle["source_proven_windows"] == len(source_hits)
    assert subtitle["source_max_gap_seconds"] > _COVERAGE_SUBTITLE_MAX_GAP_SECONDS


def test_dynasty_shape_missing_source_window_still_fails_closed(
        tmp_path, monkeypatch):
    """同形态但成品丢了源侧一个在场窗（提取丢内容）⇒ 仍失败关闭。"""
    tracks = (_track(0, "video", codec="HEVC/H.265"),
              _track(1, "subtitles"))
    media = tmp_path / "final.mkv"
    media.write_bytes(b"")
    duration = 3000.0
    windows = coverage_windows(
        duration, max_gap=_COVERAGE_SUBTITLE_MAX_GAP_SECONDS, window=60.0)
    source_hits = {0, 2, 4}
    packet_times = (10.0, 1150.0, 2290.0)
    output_hits = source_hits - {2}  # 成品丢了第二个源在场窗

    def probe(_probe, _path, requested, **_kwargs):
        if len(requested) == len(windows):
            return tuple(
                frozenset({0, 1} if i in output_hits else {0})
                for i in range(len(requested)))
        return tuple(frozenset({0, 1}) for _ in requested)

    monkeypatch.setattr(
        "bluray_fidelity.matroska._probe_window_presence", probe)
    with pytest.raises(MatroskaBuildError, match="源侧在场窗缺失"):
        require_track_coverage(
            "ffprobe", media, tracks, duration=duration, label="最终 MKV 校验",
            source_presence_by_window=tuple(
                frozenset({0, 1} if i in source_hits else {0})
                for i in range(len(windows))),
            source_packet_timestamps_by_stream={1: packet_times},
        )
