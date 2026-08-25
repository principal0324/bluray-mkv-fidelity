"""Exact-track, stream-copy Matroska finalization for MPLS-selected titles."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .bluray import BlurayTitleAsset, iter_bluray_title


class MatroskaBuildError(RuntimeError):
    """Raised when a lossless finalized representation cannot be guaranteed."""


class TransientMatroskaBuildError(MatroskaBuildError):
    """A build failure that is transient and worth a bounded automatic retry.

    Only process-spawn failures, explicit timeouts, transient I/O/OS read
    errors, and explicitly listed tool faults are marked transient. Everything
    else (codec/PID/language mismatches, DV/BL/EL/RPU/Cues/seek gate failures,
    deterministic source errors, unclassified non-zero tool exits) stays plain
    MatroskaBuildError so it fails closed without wasting a full rebuild."""


# HDR10+ 的 ITU-T T.35 注册 SEI 标识（country=0xB5, provider=0x003C, code=0x0001）。
_HDR10PLUS_T35_PREFIX = b"\xb5\x00\x3c\x00\x01"


def _strip_dynamic_sei(es_path: Path) -> Path:
    """Remove dynamic-metadata SEI NALs from an Annex B HEVC elementary stream.

    Zidoo 固件对逐帧动态元数据（HDR10+ T.35 SEI、编码器私有
    user_data_unregistered SEI——例如 ATEME 编码的 UHD 原盘）的处理约
    数分钟后崩溃。剥离范围（均为 prefix/suffix SEI NAL，type 39/40 内）：
    - payload_type 5（user_data_unregistered，携带厂商私有 UUID）：
      非标准消息，合规播放不需要；
    - payload_type 4 且为 HDR10+ T.35 注册消息。
    母版显示 (ST 2086) 与内容亮度等标准静态 SEI 保留不动。
    """
    output = es_path.with_name(es_path.name + ".nostatic-dyn.hevc")
    try:
        return _strip_dynamic_sei_impl(es_path, output)
    except Exception:
        # round-2 P1-3：处理中断时清理半成品。
        output.unlink(missing_ok=True)
        raise


def _strip_dynamic_sei_impl(es_path: Path, output: Path) -> Path:
    removed = 0
    kept = 0
    with es_path.open("rb") as source, output.open("wb") as target:
        carry = bytearray()
        chunk_size = 16 * 1024 * 1024
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            carry.extend(chunk)
            position = 0
            while True:
                marker = carry.find(b"\x00\x00\x01", position)
                if marker < 0:
                    break
                if marker > position:
                    target.write(carry[position:marker])
                nal_start = marker + 3
                if nal_start + 2 > len(carry):
                    del carry[:position]
                    break
                nal_type = (carry[nal_start] >> 1) & 0x3F
                next_marker = carry.find(b"\x00\x00\x01", nal_start)
                if next_marker < 0:
                    # 流尾：剩余数据作为最后一个 NAL 处理（避免丢起始码）。
                    next_marker = len(carry)
                    drop = False
                    if nal_type in (39, 40):
                        payload = bytes(carry[nal_start + 2:next_marker])
                        if _dynamic_sei_payload(payload):
                            drop = True
                    if not drop:
                        target.write(b"\x00\x00\x01")
                        target.write(carry[nal_start:next_marker])
                        kept += 1
                    else:
                        removed += 1
                    position = next_marker
                    del carry[:position]
                    break
                drop = False
                if nal_type in (39, 40):
                    payload = bytes(carry[nal_start + 2:next_marker])
                    if _dynamic_sei_payload(payload):
                        drop = True
                if not drop:
                    target.write(b"\x00\x00\x01")
                    target.write(carry[nal_start:next_marker])
                    kept += 1
                else:
                    removed += 1
                position = next_marker
            if position > 0:
                del carry[:position]
        target.write(carry)
    if removed == 0:
        output.unlink(missing_ok=True)
        raise MatroskaBuildError("未在视频流中找到动态元数据 SEI，无需剥离")
    return output


def _vint(data: bytes) -> tuple[int, int] | None:
    """解析 EBML VINT：返回 (值, 字节长度)；unknown-size 返回值 -1。

    round-3 P1-1：VINT 长度由首个置位标记（从 MSB 数的第一个 1）的
    位置决定，即"前导 0 数量 + 1"，1-8 字节；值 = 去掉标记位后的位。
    所有值位全 1（2^(7*长度)-1）表示 unknown size。
    """
    if not data:
        return None
    first = data[0]
    length = 0
    probe = 0x80
    while probe and not (first & probe):
        length += 1
        probe >>= 1
    length += 1
    if length > 8 or length > len(data):
        return None
    value = (first & (probe - 1)) if probe else 0
    for index in range(1, length):
        value = (value << 8) | data[index]
    if value == (1 << (7 * length)) - 1:
        value = -1
    return value, length


def _ebml_element(data: bytes) -> tuple[bytes, int, int] | None:
    """从 EBML 元素头解析 (元素ID, ID长度, 数据长度)；无法解析返回 None。

    ID 与 size 均为 VINT（前导 0 数量 + 1 定长）；size 全 1 为未知长度
    （数据长度返回 -1）。
    """
    if not data:
        return None
    id_parsed = _vint(data)
    if id_parsed is None or id_parsed[1] > 4:
        return None
    id_value, id_length = id_parsed
    elem_id = data[:id_length]
    rest = data[id_length:]
    size_parsed = _vint(rest)
    if size_parsed is None:
        return None
    size_value, size_length = size_parsed
    if id_length + size_length > len(data):
        return None
    return elem_id, id_length, size_value


def _count_cue_points(path: Path) -> int:
    """结构化 EBML 校验：遍历 Segment 顶层元素，解析 Cues 的 CuePoint 数。

    round-2 P1-4：裸字节搜索会被媒体负载误命中；这里按 EBML 元素头
    顺序跳跃（mkvmerge 的 Segment 顶层元素长度已知），找到 Cues
    （0x1C53BB6B）后在其负载内按元素 ID 0xBB 计数 CuePoint。
    """
    segment_id = b"\x18\x53\x80\x67"
    cues_id = b"\x1c\x53\xbb\x6b"
    cue_point_id = b"\xbb"
    try:
        with path.open("rb") as stream:
            head = stream.read(256 * 1024)
            index = head.find(segment_id)
            if index < 0:
                return 0
            stream.seek(index + len(segment_id))
            header = stream.read(16)
            segment = _ebml_element(segment_id + header)
            if segment is None:
                return 0
            size_field_len = _ebml_size_len(header)
            cursor = index + len(segment_id) + size_field_len
            if segment[2] < 0:
                # unknown-size Segment：安全遍历到文件边界。
                stream.seek(0, 2)
                segment_end = stream.tell()
            else:
                segment_end = cursor + segment[2]
            while cursor + 2 < segment_end:
                stream.seek(cursor)
                header = stream.read(16)
                if not header:
                    break
                element = _ebml_element(header)
                if element is None or element[2] < 0:
                    break
                elem_id, id_length, size = element
                if elem_id == cues_id:
                    payload_offset = cursor + id_length + _ebml_size_len(header[id_length:])
                    stream.seek(payload_offset)
                    payload = stream.read(min(size, 4 * 1024 * 1024))
                    # round-3 P1-1：按 EBML 子元素边界逐项统计顶层 CuePoint。
                    return _count_top_level_elements(payload, cue_point_id)
                cursor += id_length + _ebml_size_len(header[id_length:]) + size
    except OSError:
        return 0
    return 0


def _ebml_size_len(data: bytes) -> int:
    """EBML size 字段的 VINT 字节长度（用于定位负载起点）。"""
    parsed = _vint(data)
    return parsed[1] if parsed is not None else 0


def _count_top_level_elements(payload: bytes, target_id: bytes) -> int:
    """按 EBML 子元素边界遍历，统计顶层 ID 等于 target_id 的元素数。

    round-4 P2：先验证元素完整（size 非负、header+size 不越过负载边界、
    header 可解析）才计数；截断 header、声明 size 超出剩余负载、
    unknown-size 元素一律按结构损坏返回 0（失败关闭）。
    """
    count = 0
    position = 0
    while position < len(payload):
        element = _ebml_element(payload[position:])
        if element is None:
            return 0  # 截断/非法 header
        elem_id, id_length, size = element
        size_length = _ebml_size_len(payload[position + id_length:])
        if size_length == 0:
            return 0
        if size < 0 or position + id_length + size_length + size > len(payload):
            return 0  # unknown-size 或声明大小越过负载边界
        if elem_id == target_id:
            count += 1
        position += id_length + size_length + size
    return count


def _probe_duration(probe: str, path: Path) -> float:
    """ffprobe 读取容器时长（秒）；失败或无效返回 0。"""
    try:
        completed = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        value = json.loads(completed.stdout)["format"]["duration"]
        return float(value)
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def _probe_video_fps(probe: str, path: Path) -> float:
    """ffprobe 读取视频流帧率（avg/r_frame_rate）；失败返回 0。"""
    try:
        completed = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        stream = (json.loads(completed.stdout).get("streams") or [{}])[0]
        return (
            _parse_video_rate(stream.get("avg_frame_rate"))
            or _parse_video_rate(stream.get("r_frame_rate"))
            or 0.0
        )
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def _probe_video_timeline(probe: str, path: Path) -> dict:
    """Probe the video stream timeline (duration, avg/r frame rate, time_base,
    start_time). Returns {} on failure (fail-closed at the caller)."""
    try:
        completed = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=duration,avg_frame_rate,r_frame_rate,time_base,start_time",
             "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        streams = json.loads(completed.stdout).get("streams") or []
        return streams[0] if streams else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
        return {}


def _verify_video_timeline(probe: str, path: Path, asset: BlurayTitleAsset,
                           fps: float, *, label: str, ffmpeg: str = "ffmpeg") -> dict:
    """Codex P1-A / round-4 P1-1 / round-5 P1-3：核对**视频轨**（非容器）
    时间轴。

    Matroska 常见行为是 format 有 duration 而视频 stream 不提供
    `stream.duration`。视频时间跨度必须用可审计的实际视频末端事实计算：
    - stream.duration 存在：直接使用（mp4 等）；
    - 缺失：探测期望末端附近的视频 packet，取 max(pts_time + duration)
      作为真实视频末端，与 MPLS 期望保持既有 ±2s 容差；不得用容器时长
      覆盖视频时长，不得用经验窗口当合格阈值。
    帧率与官方源 ±0.01 一致。任一不满足失败关闭。
    """
    timeline = _probe_video_timeline(probe, path)
    expected_s = asset.duration_90k / 90000
    video_duration = float(timeline.get("duration") or 0)
    if video_duration > 0:
        if abs(video_duration - expected_s) > 2.0:
            raise MatroskaBuildError(
                f"{label} 视频轨时长 {video_duration:.1f}s 与 MPLS "
                f"{expected_s:.1f}s 偏差超限，拒绝发布"
            )
    else:
        # stream.duration 缺失（Matroska 常态）：真实视频末端 = 末端视频包
        # 的 pts_time + duration*time_base（可审计，±2s 门槛，不覆盖容器时长）。
        end_pts = _probe_video_end(
            probe, path, expected_s, str(timeline.get("time_base") or ""),
        )
        if end_pts is None:
            raise MatroskaBuildError(
                f"{label} 视频轨末端无法确定（末端无视频包），拒绝发布"
            )
        if abs(end_pts - expected_s) > 2.0:
            raise MatroskaBuildError(
                f"{label} 视频轨末端 {end_pts:.1f}s 与 MPLS "
                f"{expected_s:.1f}s 偏差超限，拒绝发布"
            )
        timeline["duration"] = str(end_pts)
        video_duration = end_pts
    video_rate = (
        _parse_video_rate(timeline.get("avg_frame_rate"))
        or _parse_video_rate(timeline.get("r_frame_rate"))
    )
    if video_rate is None or abs(video_rate - fps) > 0.01:
        raise MatroskaBuildError(
            f"{label} 视频轨帧率 {video_rate} 与官方 BL {fps:.4f} 不一致，拒绝发布"
        )
    return timeline


def _probe_video_end(probe: str, path: Path, expected_s: float,
                     time_base: str = "") -> float | None:
    """真实视频末端（秒）：从期望末端前 8s 处探测视频 packet 到 EOF，取
    max(pts_time + duration * time_base)。`duration` 是流 timebase 单位
    （如 1/1000 → 41 = 0.041s），必须换算成秒。探测失败或无包返回 None
    （调用方失败关闭）。"""
    try:
        num, _, den = time_base.partition("/")
        tb = (float(num) / float(den)) if den else 0.0
    except (ValueError, ZeroDivisionError):
        tb = 0.0
    try:
        completed = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-read_intervals", f"{max(expected_s - 8.0, 0.0):.3f}%+30",
             "-show_entries", "packet=pts_time,duration",
             "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        packets = json.loads(completed.stdout).get("packets") or []
    except json.JSONDecodeError:
        return None
    ends = []
    for packet in packets:
        pts = _packet_pts_time(packet)
        if pts is None:
            continue
        try:
            dur_units = float(packet.get("duration") or 0)
        except (TypeError, ValueError):
            dur_units = 0.0
        ends.append(pts + dur_units * tb)
    return max(ends) if ends else None


def _packet_pts_time(packet: dict) -> float | None:
    """Extract a packet's pts_time from an ffprobe packet dict."""
    try:
        return float(packet.get("pts_time") or packet.get("dts_time") or 0)
    except (TypeError, ValueError):
        return None






def _dynamic_sei_payload(payload: bytes) -> bool:
    """SEI NAL 负载内是否含动态元数据消息（type 5 私有 或 HDR10+ type 4）。"""
    offset = 0
    while offset + 2 <= len(payload):
        payload_type = 0
        byte = payload[offset]
        offset += 1
        while byte == 0xFF and offset < len(payload):
            payload_type += 255
            byte = payload[offset]
            offset += 1
        payload_type += byte
        if offset >= len(payload):
            return False
        payload_size = 0
        byte = payload[offset]
        offset += 1
        while byte == 0xFF and offset < len(payload):
            payload_size += 255
            byte = payload[offset]
            offset += 1
        payload_size += byte
        if offset + payload_size > len(payload):
            return False
        if payload_type == 5:
            return True
        if payload_type == 4:
            message = payload[offset:offset + payload_size]
            if message.startswith(_HDR10PLUS_T35_PREFIX):
                return True
        offset += payload_size
        if byte == 0x80:  # rbsp_trailing_bits 起点
            break
    return False



@dataclass(frozen=True)
class IdentifiedTrack:
    id: int
    type: str
    codec: str
    stream_id: int | None
    language: str
    multiplexed_tracks: tuple[int, ...]


@dataclass(frozen=True)
class ResolvedTracks:
    video_ids: tuple[int, ...]
    audio_ids: tuple[int, ...]
    subtitle_ids: tuple[int, ...]
    audio_by_official_index: tuple[tuple[int, int], ...]
    subtitle_by_official_index: tuple[tuple[int, int], ...]


def parse_identification(payload: dict) -> tuple[IdentifiedTrack, ...]:
    tracks: list[IdentifiedTrack] = []
    for value in payload.get("tracks") or ():
        properties = value.get("properties") or {}
        stream_id = properties.get("stream_id")
        try:
            parsed_stream_id = (
                int(stream_id, 0) if isinstance(stream_id, str)
                else int(stream_id) if stream_id is not None else None
            )
        except (TypeError, ValueError):
            parsed_stream_id = None
        multiplexed = properties.get("multiplexed_tracks") or ()
        tracks.append(IdentifiedTrack(
            id=int(value["id"]),
            type=str(value.get("type") or ""),
            codec=str(value.get("codec") or ""),
            stream_id=parsed_stream_id,
            language=str(properties.get("language") or "und").lower(),
            multiplexed_tracks=tuple(int(item) for item in multiplexed),
        ))
    return tuple(tracks)


def _codec_matches(coding_type: int, codec: str) -> bool:
    normalized = codec.lower().replace("_", "-")
    if coding_type == 0x80:
        return "pcm" in normalized
    if coding_type == 0x81:
        return "ac-3" in normalized and "truehd" not in normalized
    if coding_type == 0x83:
        return "truehd" in normalized or "mlp" in normalized
    if coding_type in {0x84, 0xA1}:
        return "e-ac-3" in normalized or "eac3" in normalized
    if coding_type in {0x82, 0x85, 0x86, 0xA2}:
        return "dts" in normalized
    if coding_type == 0x90:
        return "pgs" in normalized
    return True


def _resolve_one(pid: int, coding_type: int, kind: str,
                 tracks: Iterable[IdentifiedTrack]) -> int:
    candidates = [
        track for track in tracks
        if track.type == kind and track.stream_id == pid and _codec_matches(coding_type, track.codec)
    ]
    if len(candidates) != 1:
        detail = ", ".join(f"{item.id}:{item.codec}" for item in candidates) or "无"
        raise MatroskaBuildError(
            f"无法唯一映射 MPLS PID {pid}（编码 0x{coding_type:02x}，候选：{detail}）"
        )
    return candidates[0].id


def _try_resolve_one(pid: int, coding_type: int, kind: str,
                     tracks: Iterable[IdentifiedTrack]) -> int | None:
    """Resolve a playlist entry when mkvmerge exposed that elementary stream.

    A few Blu-ray playlists contain stale/optional stream-table entries.  The
    title reader reports those entries from MPLS, while mkvmerge quite
    correctly omits them because no packets for the PID exist in the selected
    title.  Such an entry must not abort the whole lossless stream-copy job;
    the available tracks are still copied byte-for-byte.
    """
    candidates = [
        track for track in tracks
        if track.type == kind and track.stream_id == pid
        and _codec_matches(coding_type, track.codec)
    ]
    return candidates[0].id if len(candidates) == 1 else None


def resolve_official_tracks(asset: BlurayTitleAsset,
                            identified: Iterable[IdentifiedTrack]) -> ResolvedTracks:
    tracks = tuple(identified)
    videos = tuple(track.id for track in tracks if track.type == "video")
    if not videos:
        raise MatroskaBuildError("MPLS 主片没有可用视频轨")
    if not asset.audio_tracks:
        raise MatroskaBuildError("MPLS 主片没有提供官方音轨表，拒绝生成静音影片")
    audio_pairs = tuple(
        (official.index, track_id)
        for official in asset.audio_tracks
        if (track_id := _try_resolve_one(
            official.pid, official.coding_type, "audio", tracks
        )) is not None
    )
    subtitle_pairs = tuple(
        (official.index, track_id)
        for official in asset.subtitle_tracks
        if (track_id := _try_resolve_one(
            official.pid, official.coding_type, "subtitles", tracks
        )) is not None
    )
    if not audio_pairs:
        raise MatroskaBuildError("MPLS 主片没有可映射的音轨")
    return ResolvedTracks(
        video_ids=videos,
        audio_ids=tuple(value for _, value in audio_pairs),
        subtitle_ids=tuple(value for _, value in subtitle_pairs),
        audio_by_official_index=audio_pairs,
        subtitle_by_official_index=subtitle_pairs,
    )


def build_mkvmerge_command(asset: BlurayTitleAsset, resolved: ResolvedTracks,
                           input_path: Path, output_path: Path, *, title: str) -> list[str]:
    command = ["mkvmerge", "--output", str(output_path), "--title", title, "--gui-mode"]
    command += ["--video-tracks", ",".join(map(str, resolved.video_ids))]
    command += ["--audio-tracks", ",".join(map(str, resolved.audio_ids))]
    if resolved.subtitle_ids:
        command += ["--subtitle-tracks", ",".join(map(str, resolved.subtitle_ids))]
    else:
        command += ["--no-subtitles"]
    audio_by_index = {index: track_id for index, track_id in resolved.audio_by_official_index}
    available_audio = set(audio_by_index)
    default_audio_index = asset.default_audio_index
    if default_audio_index not in available_audio:
        default_audio_index = next(
            (official.index for official in asset.audio_tracks
             if official.index in available_audio
             and official.language.lower() in {"zho", "chi", "zh"}),
            None,
        )
    for official in asset.audio_tracks:
        track_id = audio_by_index.get(official.index)
        if track_id is None:
            continue
        command += ["--language", f"{track_id}:{official.language or 'und'}"]
        is_default = official.index == default_audio_index
        command += ["--default-track-flag", f"{track_id}:{'yes' if is_default else 'no'}"]
    subtitle_by_index = {index: track_id for index, track_id in resolved.subtitle_by_official_index}
    available_subtitles = set(subtitle_by_index)
    default_subtitle_index = asset.default_subtitle_index
    if default_subtitle_index not in available_subtitles:
        default_subtitle_index = next(
            (official.index for official in asset.subtitle_tracks
             if official.index in available_subtitles
             and official.language.lower() in {"zho", "chi", "zh"}),
            None,
        )
    for official in asset.subtitle_tracks:
        track_id = subtitle_by_index.get(official.index)
        if track_id is None:
            continue
        command += ["--language", f"{track_id}:{official.language or 'und'}"]
        is_default = official.index == default_subtitle_index
        command += ["--default-track-flag", f"{track_id}:{'yes' if is_default else 'no'}"]
    command.append(str(input_path))
    return command


def build_mkvmerge_command_dv(asset: BlurayTitleAsset, resolved: ResolvedTracks,
                              video_path: Path, source_path: Path,
                              output_path: Path, *, title: str) -> list[str]:
    """Two-input mkvmerge command: dovi_tool-merged video + original M2TS.

    mkvmerge options precede the input file they modify and use that file's
    local track IDs.  The merged HEVC is the first input so the video keeps
    track ID 0; audio and subtitle tracks come from the materialized M2TS,
    whose TrueHD tracks keep their interleaved AC-3 cores.
    """
    command = ["mkvmerge", "--output", str(output_path), "--title", title, "--gui-mode"]
    command += ["--video-tracks", "0", "--language", "0:und",
                "--default-track-flag", "0:yes"]
    command.append(str(video_path))
    command += ["--video-tracks", "!0"]
    if resolved.audio_ids:
        command += ["--audio-tracks", ",".join(map(str, resolved.audio_ids))]
    else:
        command += ["--no-audio"]
    if resolved.subtitle_ids:
        command += ["--subtitle-tracks", ",".join(map(str, resolved.subtitle_ids))]
    else:
        command += ["--no-subtitles"]
    audio_by_index = {index: track_id for index, track_id in resolved.audio_by_official_index}
    available_audio = set(audio_by_index)
    default_audio_index = asset.default_audio_index
    if default_audio_index not in available_audio:
        default_audio_index = next(
            (official.index for official in asset.audio_tracks
             if official.index in available_audio
             and official.language.lower() in {"zho", "chi", "zh"}),
            None,
        )
    for official in asset.audio_tracks:
        track_id = audio_by_index.get(official.index)
        if track_id is None:
            continue
        command += ["--language", f"{track_id}:{official.language or 'und'}"]
        is_default = official.index == default_audio_index
        command += ["--default-track-flag", f"{track_id}:{'yes' if is_default else 'no'}"]
    subtitle_by_index = {index: track_id for index, track_id in resolved.subtitle_by_official_index}
    available_subtitles = set(subtitle_by_index)
    default_subtitle_index = asset.default_subtitle_index
    if default_subtitle_index not in available_subtitles:
        default_subtitle_index = next(
            (official.index for official in asset.subtitle_tracks
             if official.index in available_subtitles
             and official.language.lower() in {"zho", "chi", "zh"}),
            None,
        )
    for official in asset.subtitle_tracks:
        track_id = subtitle_by_index.get(official.index)
        if track_id is None:
            continue
        command += ["--language", f"{track_id}:{official.language or 'und'}"]
        is_default = official.index == default_subtitle_index
        command += ["--default-track-flag", f"{track_id}:{'yes' if is_default else 'no'}"]
    command.append(str(source_path))
    return command


def require_dovi_side_data(payload: dict, dv_profile: str = "7") -> dict:
    """Extract the DOVI configuration record from ffprobe side data (fail closed).

    Profile 7 keeps the enhancement layer (BL + EL + RPU). Profile 8.1 is the
    single-layer form (BL + RPU, EL residual dropped) used when a player
    crashes on FEL content; the RPU metadata is preserved in both.
    """
    for side in (payload.get("streams") or [{}])[0].get("side_data_list") or []:
        if side.get("side_data_type") != "DOVI configuration record":
            continue
        if dv_profile == "81":
            if (side.get("dv_profile") == 8
                    and side.get("rpu_present_flag")
                    and side.get("bl_present_flag")):
                return side
        elif (side.get("dv_profile") == 7
                and side.get("rpu_present_flag")
                and side.get("el_present_flag")):
            return side
    raise MatroskaBuildError(
        f"最终 MKV 缺少要求的 Dolby Vision 配置（DV Profile {dv_profile}），拒绝降级输出"
    )


def _parse_video_rate(value) -> float | None:
    """Parse an ffprobe frame-rate string ('24000/1001', '25/1') into a
    positive rational seconds^-1, or None when missing/zero/implausible."""
    if not value or value in ("0/0", "N/A", ""):
        return None
    try:
        num, _, den = value.partition("/")
        numerator = float(num)
        denominator = float(den)
        if numerator <= 0 or denominator <= 0:
            return None
        rate = numerator / denominator
        if not (1.0 < rate < 120.0):
            return None
        return rate
    except (ValueError, ZeroDivisionError):
        return None


def _video_rate_facts(stream: dict, label: str) -> dict:
    """Require a parseable, positive, plausible video frame rate (Codex
    P1-A / round-4 P1-4). Returns the float rate (comparisons) and the
    source rational string (exact ffmpeg input clock). avg_frame_rate is
    authoritative; r_frame_rate is a fallback ONLY when avg is absent or
    invalid. Both present but conflicting beyond tolerance -> fail closed.
    """
    avg = _parse_video_rate(stream.get("avg_frame_rate"))
    rfr = _parse_video_rate(stream.get("r_frame_rate"))
    if avg is not None:
        if rfr is not None and abs(avg - rfr) > 0.01:
            raise MatroskaBuildError(
                f"{label} 视频帧率冲突（avg={stream.get('avg_frame_rate')} "
                f"r={stream.get('r_frame_rate')}），拒绝继续"
            )
        return {"rate": avg, "rational": str(stream.get("avg_frame_rate"))}
    if rfr is not None:
        return {"rate": rfr, "rational": str(stream.get("r_frame_rate"))}
    raise MatroskaBuildError(
        f"{label} 视频帧率缺失或不可解析（avg/r frame rate），拒绝继续"
    )


def _require_video_rate(stream: dict, label: str) -> float:
    """Back-compat helper returning only the float rate."""
    return _video_rate_facts(stream, label)["rate"]


def dual_hevc_video_streams(streams: list[dict] | None) -> bool:
    """Two HEVC video streams in one title mean a Dolby Vision BL + EL pair.

    Some UHD discs (e.g. 西线无战事 AQLJ) do not register the enhancement
    layer in the MPLS stream table; the PMT is the only place that declares
    the second HEVC PID. ffprobe enumerates PMT streams, so this check is the
    ground truth for "the transport really carries two HEVC video PIDs".
    """
    videos = [stream for stream in (streams or []) if stream.get("codec_type") == "video"]
    hevc = [
        stream for stream in videos
        if "hevc" in str(stream.get("codec_name", "")).lower()
        or "hevc" in str(stream.get("codec", "")).lower()
    ]
    return len(hevc) >= 2


def validate_finalized_identification(asset: BlurayTitleAsset, resolved: ResolvedTracks,
                                      payload: dict,
                                      dolby_vision: bool | None = None) -> dict:
    container = payload.get("container") or {}
    duration = int((container.get("properties") or {}).get("duration") or 0)
    if duration <= 0:
        raise MatroskaBuildError("最终 MKV 缺少有效时长/Cues 信息")
    tracks = parse_identification(payload)
    videos = [track for track in tracks if track.type == "video"]
    audio = [track for track in tracks if track.type == "audio"]
    subtitles = [track for track in tracks if track.type == "subtitles"]
    if len(audio) != len(resolved.audio_by_official_index):
        raise MatroskaBuildError("最终 MKV 音轨数量与可映射 MPLS 音轨不一致")
    if len(subtitles) != len(resolved.subtitle_by_official_index):
        raise MatroskaBuildError("最终 MKV 字幕数量与可映射 MPLS 字幕不一致")
    # P1-3：DV 事实必须来自构建实际检测（含 PMT-only DV，MPLS 可能只
    # 声明一条视频轨）；未传入时按旧规则（双视频轨）推断。
    if dolby_vision is None:
        dolby_vision = len(resolved.video_ids) > 1
    if dolby_vision:
        # BATCH-006 修复：DV 合并的完整性由后续 _verify_dolby_vision 用
        # ffprobe 的 DOVI configuration record（dv_profile==7 + rpu + el）
        # 权威验证。此处只要求成品是单视频轨——dovi_tool 合并 BL+EL 后
        # mkvmerge 输出单轨，但不一定报 multiplexed_tracks（对合并的
        # HEVC 输入 mkvmerge 不维护该标记），旧校验会误拒已正确合并的
        # 成品（劳伦斯 part-2 实测 Multiplexing took 16min 后被拒）。
        if len(videos) != 1:
            raise MatroskaBuildError("Dolby Vision 增强层没有完整合并，拒绝降级输出")
    elif len(videos) != 1:
        raise MatroskaBuildError("最终 MKV 视频轨数量无效")
    # P1-4：逐轨语言期望校验（构建命令按 MPLS 注入语言，成品必须一致）。
    _validate_track_languages(asset, resolved, audio, subtitles)
    return {
        "duration_ns": duration,
        "dolby_vision": dolby_vision,
        "audio_tracks": tuple({"index": index, "language": track.language}
                              for index, track in enumerate(audio)),
        "subtitle_tracks": tuple({"index": index, "language": track.language}
                                 for index, track in enumerate(subtitles)),
    }


# ISO 639-2 书目码（B）与术语码（T）的 20 组等价对。mkvmerge 写轨时会把
# 术语码归一化为书目码（实测 v92.0：--language 0:fra 产出 fre），而官方
# Blu-ray 元数据可能使用任一侧；语言校验按语义等价比较，不能逐字节比对。
_ISO639_2_BT_EQUIV = {
    "alb": "sqi", "arm": "hye", "baq": "eus", "bur": "mya", "chi": "zho",
    "cze": "ces", "dut": "nld", "fre": "fra", "geo": "kat", "ger": "deu",
    "gre": "ell", "ice": "isl", "mac": "mkd", "mao": "mri", "may": "msa",
    "per": "fas", "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym",
}


def _canonical_language(code: str | None) -> str:
    """把语言码归一为术语码（T）形式；B/T 视为同一语言，其余原样返回。"""
    code = (code or "und").strip().lower()
    return _ISO639_2_BT_EQUIV.get(code, code)


def _validate_track_languages(asset: BlurayTitleAsset, resolved: ResolvedTracks,
                             audio: list, subtitles: list) -> None:
    """逐轨校验输出语言与 MPLS 官方语言一致（构建注入后的期望值）。

    round-2 P1-2：mkvmerge 最终 MKV 的 track.id 会重新编号（video=0、
    audio=1..、subtitle=..），与输入识别的 track ID 不同域。这里按
    **输出顺序**与已选择的官方轨道顺序逐轨比较：resolved 的
    audio_ids/subtitle_ids 顺序即输出音轨/字幕顺序；官方轨道按
    BlurayTrack.index 匹配（不是 tuple 位置）。
    """
    audio_by_index = {track.index: track for track in asset.audio_tracks}
    subtitle_by_index = {track.index: track for track in asset.subtitle_tracks}
    for position, track in enumerate(audio):
        if position >= len(resolved.audio_by_official_index):
            continue
        official_index, _ = resolved.audio_by_official_index[position]
        official = audio_by_index.get(official_index)
        if official is None:
            continue
        expected = (official.language or "und").lower()
        if _canonical_language(track.language) != _canonical_language(expected):
            raise MatroskaBuildError(
                f"最终 MKV 音轨 {position} 语言 {track.language or 'und'} "
                f"与 MPLS 期望 {expected} 不一致"
            )
    for position, track in enumerate(subtitles):
        if position >= len(resolved.subtitle_by_official_index):
            continue
        official_index, _ = resolved.subtitle_by_official_index[position]
        official = subtitle_by_index.get(official_index)
        if official is None:
            continue
        expected = (official.language or "und").lower()
        if _canonical_language(track.language) != _canonical_language(expected):
            raise MatroskaBuildError(
                f"最终 MKV 字幕轨 {position} 语言 {track.language or 'und'} "
                f"与 MPLS 期望 {expected} 不一致"
            )


class BuildArtifacts:
    """一次构建的精确工件路径（BuildIdentity 的一部分）。

    BATCH-006 R2 P0：所有文件名推导集中在唯一入口，representation 与
    MatroskaBuilder 共用——禁止两边手写文件名（此前 staging 路径在
    service.py 与 matroska.py 各写一次导致不一致）。``output`` 是
    generation 化的 partial 路径（main-title.m2ts.partial.N）。
    """

    def __init__(self, output: Path):
        self.output: Path = Path(output)
        self.partial: Path = self.output
        self.source: Path = self.output.with_name(self.output.name + ".source.m2ts")
        self.staging: Path = self.output.with_name("main-title.materializing")
        self.log: Path = self.output.with_name(f"build-{self._generation_suffix()}.log")

    def _generation_suffix(self) -> str:
        name = self.output.name
        # main-title.m2ts.partial.N -> N
        marker = ".partial."
        if marker in name:
            return name.split(marker, 1)[1]
        return "1"

    def as_artifacts_list(self) -> list[str]:
        """精确工件清单（供 pending 持久化与失效清理）。"""
        return [
            str(self.partial),
            str(self.source),
            str(self.staging),
            str(self.log),
        ]

    def owning_directory(self) -> Path:
        return self.output.parent


def build_artifacts(output: Path) -> BuildArtifacts:
    """唯一构建工件计算入口（representation 与 MatroskaBuilder 共用）。"""
    return BuildArtifacts(output)


class MatroskaBuilder:
    """Finalize a virtual libbluray title through a seekable temporary M2TS.

    mkvmerge's MPEG transport-stream reader probes the input by seeking while
    it discovers all tracks.  A FIFO therefore makes some distro builds abort
    (SIGABRT) before generation starts.  We materialize the selected title to
    a temporary regular file, use it for both identification and muxing, and
    remove it immediately after the finalized MKV is published or fails.
    """

    def __init__(self, executable: str = "mkvmerge", ffmpeg: str = "ffmpeg",
                 dovi_tool: str = "dovi_tool", probe: str = "ffprobe",
                 dv_profile: str | None = None):
        self.executable = executable
        self.ffmpeg = ffmpeg
        self.dovi_tool = dovi_tool
        self.probe = probe
        # "7" 保留 EL（FEL 原样）；"81" 转单层 Profile 8.1（保留 RPU，
        # 丢弃 EL 残差）——Zidoo 对部分近空 EL 的 profile 7 流会崩溃。
        self.dv_profile = dv_profile or os.getenv("HDATHOME_DV_PROFILE", "7")

    def peak_space_bytes(self, asset: BlurayTitleAsset) -> int:
        """峰值临时空间：同一槽位同时存活文件的最坏情况总和。

        路线峰值（相对 asset.size）：
        - 普通：物化主片 1.0 + 部分 MKV ≈0.92 → ≈2×；
        - DV（含 PMT-only，物化后才可识别）：主片 1.0 + BL ≈0.82 +
          EL ≈0.01 + 合并 DV ≈0.82 + 部分 MKV ≈0.92 → ≈3.6×；
        - HDR10+：主片 1.0 + 抽取视频 ≈0.82 + 处理后视频 ≈0.82 +
          部分 MKV ≈0.92 → ≈3.6×。
        采用保守最坏情况 4× + 安全余量，PMT-only DV 无需二次准入
        （design P1-2：物化后才能分类时可用足够保守的预留）。
        """
        return 4 * int(asset.size) + 512 * 1024**2

    def _identify_path(self, source: Path, executable: str | None = None) -> dict:
        tool = executable or self.executable
        try:
            completed = subprocess.run(
                [tool, "--identification-format", "json", "--identify", str(source)],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
            raise MatroskaBuildError(f"mkvmerge 主片识别失败：{str(detail).strip()[-500:]}") from error
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise MatroskaBuildError(f"mkvmerge 主片识别返回无效 JSON：{error}") from error

    def _require_dovi_tool(self) -> str:
        for candidate in (self.dovi_tool, "/usr/local/bin/dovi_tool", "/opt/hdathome/bin/dovi_tool"):
            if candidate and shutil.which(candidate):
                return candidate
        raise MatroskaBuildError(
            "影片包含 Dolby Vision 增强层，但服务器未安装 dovi_tool；拒绝降级输出"
        )

    def _video_streams(self, source: Path) -> list[dict]:
        """ffprobe stream list (PMT enumeration) with PIDs and dimensions.

        Gate B round-3（Codex P1-A）：同时保留 avg/r frame rate、time_base、
        start_time、duration，供裸 HEVC 输入时钟与视频时间轴校验使用。
        """
        try:
            completed = subprocess.run(
                [self.probe, "-v", "error",
                 "-show_entries", ",".join([
                     "stream=codec_type", "codec_name", "id", "index",
                     "width", "height", "pix_fmt", "profile",
                     "color_space", "color_transfer", "color_primaries",
                     "avg_frame_rate", "r_frame_rate", "time_base",
                     "start_time", "duration",
                     "side_data_list",
                 ]) + ":stream_side_data",
                 "-of", "json", str(source)],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
            raise MatroskaBuildError(
                f"主片视频轨探测失败，无法判断 Dolby Vision：{str(detail).strip()[-300:]}"
            ) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise MatroskaBuildError(f"ffprobe 返回无效 JSON：{error}") from error
        return list(payload.get("streams") or [])

    def _dv_video_pids(self, asset: BlurayTitleAsset, streams: list[dict]) -> tuple[int, int]:
        """Resolve the (BL, EL) PID pair from the MPLS table or the PMT.

        Some UHD discs do not register the enhancement layer in the MPLS
        stream table; in that case the PMT enumeration is the only registry.
        ffprobe lists PMT streams in declaration order (BL before EL), and we
        sort by resolution descending as a second guard.
        """
        mpls_pids = [track.pid for track in asset.video_tracks]
        if len(mpls_pids) >= 2:
            return mpls_pids[0], mpls_pids[1]
        hevc = [
            stream for stream in streams
            if stream.get("codec_type") == "video"
            and (
                "hevc" in str(stream.get("codec_name", "")).lower()
                or "hevc" in str(stream.get("codec", "")).lower()
            )
        ]
        hevc.sort(key=lambda stream: -(int(stream.get("width") or 0)))
        if len(hevc) < 2:
            raise MatroskaBuildError("Dolby Vision 主片缺少第二视频轨（增强层）")
        pids = []
        for stream in hevc[:2]:
            raw_id = str(stream.get("id") or "")
            try:
                pids.append(int(raw_id, 16))
            except ValueError as error:
                raise MatroskaBuildError(f"视频轨 PID 无效：{raw_id}") from error
        return pids[0], pids[1]

    def _primary_video_pid(self, asset: BlurayTitleAsset, streams: list[dict]) -> int:
        """Return the authoritative base-video PID without inferring a DV EL.

        HDR10+ cleanup operates on one official video stream.  It must never
        reuse ``_dv_video_pids``: that helper deliberately requires a second
        HEVC stream and is only valid for a confirmed Profile-7 BL+EL title.
        """
        if asset.video_tracks:
            return int(asset.video_tracks[0].pid)
        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        if not videos:
            raise MatroskaBuildError("主片缺少可处理的视频轨")
        raw_id = str(videos[0].get("id") or "")
        try:
            return int(raw_id, 16)
        except ValueError as error:
            raise MatroskaBuildError(f"主片视频轨 PID 无效：{raw_id}") from error

    def _extract_dv_layer(self, source: Path, pid: int, target: Path) -> None:
        command = [
            self.ffmpeg, "-y", "-nostats", "-v", "error",
            "-i", str(source),
            "-map", f"0:i:0x{pid:x}",
            "-c", "copy", "-f", "hevc",
            str(target),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=14400)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise MatroskaBuildError(
                    f"ffmpeg 提取视频 PID 0x{pid:x} 失败（退出码 {completed.returncode}）"
                    f"{(': ' + detail[-500:]) if detail else ''}"
                )
        except Exception:
            # round-2 P1-3：半成品在 helper 内兜底清理（外层 temporaries
            # 也已在写入前登记，双保险）。
            target.unlink(missing_ok=True)
            raise

    def _extract_dv_layers(self, source: Path, base_pid: int, enhancement_pid: int,
                           progress: Callable[[int, str], None] | None,
                           base_path: Path, enhancement_path: Path) -> None:
        """Extract the two DV primary video PIDs as elementary HEVC streams.

        round-2 P1-3：目标路径由调用方在写入前登记并传入，helper 不再
        自行推导；任一步失败由 _extract_dv_layer 与外部 finally 清理。
        """
        if progress:
            progress(41, "正在提取 Dolby Vision 基础层（BL）")
        self._extract_dv_layer(source, base_pid, base_path)
        if progress:
            progress(48, "正在提取 Dolby Vision 增强层（EL）")
        self._extract_dv_layer(source, enhancement_pid, enhancement_path)

    def _merge_dv_layers(self, executable: str, base_path: Path, enhancement_path: Path,
                         merged_path: Path, progress: Callable[[int, str], None] | None) -> None:
        if progress:
            mode = "Profile 8.1（单层，保留 RPU）" if self.dv_profile == "81" else "Profile 7（保留增强层）"
            progress(56, f"正在合并 Dolby Vision 基础层与增强层（dovi_tool，{mode}）")
        if self.dv_profile == "81":
            # 单层化：先丢弃 EL 视频 NAL 只留 RPU，再转换 RPU 为
            # Profile 8.1 兼容（保留 luma/chroma mapping）。
            partial = merged_path.with_name(merged_path.name + ".intermediate.hevc")
            try:
                first = [
                    executable, "mux", "--discard",
                    "--bl", str(base_path),
                    "--el", str(enhancement_path),
                    "-o", str(partial),
                ]
                completed = subprocess.run(first, capture_output=True, text=True, timeout=14400)
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout or "").strip()
                    raise MatroskaBuildError(
                        f"dovi_tool 单层化失败（退出码 {completed.returncode}）"
                        f"{(': ' + detail[-500:]) if detail else ''}"
                    )
                second = [
                    executable, "-m", "5", "convert",
                    "-i", str(partial),
                    "-o", str(merged_path),
                ]
                completed = subprocess.run(second, capture_output=True, text=True, timeout=14400)
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout or "").strip()
                    raise MatroskaBuildError(
                        f"dovi_tool Profile 8.1 转换失败（退出码 {completed.returncode}）"
                        f"{(': ' + detail[-500:]) if detail else ''}"
                    )
            finally:
                # round-2 P1-3：intermediate 无论成败都清理。
                partial.unlink(missing_ok=True)
            return
        command = [
            executable, "mux",
            "--bl", str(base_path),
            "--el", str(enhancement_path),
            "-o", str(merged_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=14400)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise MatroskaBuildError(
                f"dovi_tool 合并失败（退出码 {completed.returncode}）"
                f"{(': ' + detail[-500:]) if detail else ''}"
            )

    @staticmethod
    def _capture_merged_rpu_evidence(
        dovi_executable: str,
        merged_video: Path,
        evidence_path: Path,
    ) -> dict:
        """Collect the evidence required by the common Profile-7 release gate."""
        return _raw_rpu_evidence(
            dovi_executable, merged_video, evidence_path, limit=240,
        )

    def _has_hdr10plus(self, source: Path) -> bool:
        """Detect HDR10+ T.35 SEI in a short frame window of the materialized TS."""
        try:
            completed = subprocess.run(
                [self.probe, "-v", "error", "-select_streams", "v:0",
                 "-read_intervals", "%+#200",
                 "-show_entries", "frame_side_data", "-of", "json",
                 str(source)],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
            raise MatroskaBuildError(
                f"HDR10+ 探测失败：{str(detail).strip()[-300:]}"
            ) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise MatroskaBuildError(f"ffprobe 返回无效 JSON：{error}") from error
        for frame in payload.get("frames") or []:
            for side in frame.get("side_data_list") or []:
                if "User Data Unregistered SEI" in str(side.get("side_data_type", "")):
                    return True
        return False

    def _verify_seekable(self, output: Path, dv_facts: dict | None = None) -> None:
        """可重复的 seek/Cues 自动证据（round-2 P1-4）。

        BATCH-006：DV Profile 7 成品必须传 dv_facts（含 dovi_executable）
        让 seek 校验走 DV 正交路线（stream-copy + BL 解码），否则合并流被
        通用 decoder 判"有包无帧"误拒（劳伦斯 part-2 实测 15min59s 封包后
        在 10% seek 失败）。
        """
        duration = _probe_duration(self.probe, output)
        if duration <= 0:
            raise MatroskaBuildError("成品时长无效，无法验证 seek")
        _verify_cues_and_seek(
            self.probe, output, int(duration * 1_000_000_000),
            dv_facts=dv_facts,
        )



    def _verify_dolby_vision(self, output: Path) -> dict:
        """Refuse a finalized MKV whose video track lost the DV enhancement layer."""
        try:
            completed = subprocess.run(
                [self.probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream_side_data", "-of", "json", str(output)],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
            raise MatroskaBuildError(f"Dolby Vision 核验失败：{str(detail).strip()[-300:]}") from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise MatroskaBuildError(f"ffprobe 返回无效 JSON：{error}") from error
        return require_dovi_side_data(payload, self.dv_profile)

    @staticmethod
    def _materialize_title(
        asset: BlurayTitleAsset,
        source: Path,
        progress: Callable[[int, str], None] | None = None,
        staging: Path | None = None,
    ) -> None:
        source.parent.mkdir(parents=True, exist_ok=True)
        total = max(1, int(asset.size))
        written = 0
        last_report = 0.0
        # BATCH-006 R2 P0：staging 路径由 build_artifacts 唯一推导（调用方
        # 传入），不再在此手写文件名——避免 representation 与 matroska 两处
        # 命名不一致导致恢复/清理记录错误路径。
        if staging is None:
            staging = source.with_name("main-title.materializing")
        # BATCH-006 修复：先写独立的 staging 临时文件，完成后原子 rename 到
        # source。staging 名字必须**完全脱离**清理 glob 范围——recover_pending
        # 用 glob("main-title.m2ts.partial*") 清理旧 partial，而 `*` 匹配任意
        # 字符（含点），会误删 main-title.m2ts.partial.1.source.m2ts 及其任何
        # 后缀文件（此前 .materializing 命名仍以 .partial 开头，同样被误删）。
        # staging 改用不带 ".partial" 的独立前缀，任何清理 glob 都匹配不到。
        # （R2 P0：staging 由调用方 build_artifacts 传入，此处不再手写。）
        staging.unlink(missing_ok=True)
        import logging as _logging
        _log = _logging.getLogger("hdathome.matroska")
        try:
            with staging.open("wb", buffering=1024 * 1024) as stream:
                try:
                    import os as _os
                    _ino = _os.fstat(stream.fileno()).st_ino
                except OSError:
                    _ino = "?"
                for chunk in iter_bluray_title(asset, chunk_size=1024 * 1024):
                    stream.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if progress and (now - last_report >= 0.5 or written >= total):
                        last_report = now
                        # Reserve the first 40% for extracting the selected MPLS
                        # title. mkvmerge uses the remaining range for muxing.
                        percent = min(40, int(written * 40 / total))
                        progress(
                            percent,
                            f"正在提取 MPLS 主片：{written / 1024**3:.1f} / "
                            f"{total / 1024**3:.1f} GB",
                        )
            # 原子发布：临时文件写完后 rename 到 source（读者只看到完整文件）
            os.replace(staging, source)
            _log.debug(
                "materialize done inode=%s written_gb=%.1f path=%s",
                locals().get("_ino", "?"), written / 1024**3, source,
            )
        finally:
            staging.unlink(missing_ok=True)

    @staticmethod
    def _source_checkpoint_is_valid(asset: BlurayTitleAsset, source: Path) -> bool:
        """Only atomically published, complete source files are reusable."""
        try:
            return source.is_file() and source.stat().st_size == int(asset.size)
        except OSError:
            return False

    def _materialize_or_reuse_title(
        self,
        asset: BlurayTitleAsset,
        source: Path,
        progress: Callable[[int, str], None] | None = None,
        staging: Path | None = None,
    ) -> bool:
        """Reuse a completed source stage; restart an interrupted one."""
        if self._source_checkpoint_is_valid(asset, source):
            if progress:
                progress(40, "复用已完成的 MPLS 主片检查点")
            return True
        # Never append to an interrupted transport stream.
        source.unlink(missing_ok=True)
        self._materialize_title(asset, source, progress=progress, staging=staging)
        return False

    def build(self, asset: BlurayTitleAsset, output: Path, *, title: str,
              progress: Callable[[int, str], None] | None = None) -> dict:
        if shutil.which(self.executable) is None:
            raise MatroskaBuildError("服务器未安装 mkvmerge")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # BATCH-006 R2 P0：唯一工件计算入口——source/staging 不再手写。
        artifacts = build_artifacts(output)
        source = artifacts.source
        temporaries: list[Path] = []
        try:
            self._materialize_or_reuse_title(
                asset, source, progress=progress, staging=artifacts.staging,
            )
            if progress:
                progress(40, "正在识别主片音视频轨")
            identified = parse_identification(self._identify_path(source))
            resolved = resolve_official_tracks(asset, identified)
            video_streams = self._video_streams(source)
            # MPLS stream table is the primary DV signal, but some discs keep
            # the enhancement layer out of it; the PMT-level ffprobe check
            # catches those too. Either way the EL must survive finalization.
            dolby_vision = asset.dolby_vision or dual_hevc_video_streams(video_streams)
            if dolby_vision:
                # mkvmerge cannot see a Dolby Vision enhancement layer whose
                # VPS carries no DV profile extension (several UHD encodes do
                # this). dovi_tool merges BL + EL into one profile-7 track;
                # mkvmerge then muxes that track with the M2TS audio/subs.
                dovi_executable = self._require_dovi_tool()
                base_pid, enhancement_pid = self._dv_video_pids(asset, video_streams)
                base_path = source.with_name(source.name + ".bl.hevc")
                enhancement_path = source.with_name(source.name + ".el.hevc")
                # round-2 P1-3：写入前登记，任何失败都在 finally 清理。
                temporaries += [base_path, enhancement_path]
                self._extract_dv_layers(
                    source, base_pid, enhancement_pid, progress,
                    base_path, enhancement_path,
                )
                merged_video = source.with_name(source.name + ".dv.hevc")
                temporaries.append(merged_video)
                self._merge_dv_layers(
                    dovi_executable, base_path, enhancement_path, merged_video, progress,
                )
                # 最终 Profile 7 发布门要求合并裸流存在可复核的 RPU 证据。
                # 这里必须在合并后、封装前由 dovi_tool 直接读取裸 HEVC；不能
                # 只在 FfmpegDvFileMatroskaBuilder 路径采集，否则默认 mkvmerge
                # 路径会在自身的最终校验中因证据缺失而必然失败。
                merged_rpu_path = source.with_name(source.name + ".rpu.bin")
                temporaries.append(merged_rpu_path)
                merged_rpu_evidence = self._capture_merged_rpu_evidence(
                    dovi_executable, merged_video, merged_rpu_path,
                )
                # BATCH-006：DV Profile 7 的 seek 校验需要 dv_facts 走 DV 正交
                # 路线（stream-copy + BL 解码），否则合并流被通用 decoder 判
                # "有包无帧"误拒。收集 dovi_tool 事实与帧率。
                #
                # round-9 P1（默认路径对齐）：最终 Profile 7 发布门
                # (_verify_dv_profile7_evidence) 强制要求 dv_facts 携带源 EL
                # VCL 序列摘要；否则"Dolby Vision EL 独立证据缺失"拒绝发布。
                # 复用实验路径 (FfmpegDvFileMatroskaBuilder) 的同一解析器与
                # 同一严格一致性语义（不复制另一套、不放宽）。源 EL 摘要只
                # 计算一次，供合并流/最终 MKV 两阶段复用；EL 临时文件此时
                # 仍存在（统一在 finally 清理）。
                src_el_digests, src_el_stats = _el_vcl_parse(enhancement_path)
                el_merged_evidence = _verify_el_order_consistency(
                    dovi_executable, self.ffmpeg, enhancement_path,
                    merged_video, "合并流", container=False,
                    src_digests=src_el_digests, src_stats=src_el_stats,
                )
                dv_facts = {
                    "dovi_executable": dovi_executable,
                    # The final Profile-7 RPU validator samples 10%/50%/90%
                    # of the completed title.  It must receive the source
                    # duration here; otherwise it treats the title as 0s
                    # after an otherwise successful mux and fails closed.
                    "duration_s": _probe_duration(self.probe, source),
                    "fps": _probe_video_fps(self.probe, source),
                    "bl_size": int(base_path.stat().st_size) if base_path.is_file() else 0,
                    "merged_size": int(merged_video.stat().st_size) if merged_video.is_file() else 0,
                    "merged_rpu_evidence": dict(merged_rpu_evidence),
                    "el_source_digests": src_el_digests,
                    "el_source_digest_stats": src_el_stats,
                    "el_merged_order": el_merged_evidence,
                }
                command = build_mkvmerge_command_dv(
                    asset, resolved, merged_video, source, output, title=title,
                )
            else:
                dv_facts = None
                if self._has_hdr10plus(source):
                    # Zidoo 固件对 HDR10+ 逐帧动态元数据（T.35 SEI）的处理约
                    # 数分钟后崩溃（阿波罗13 实测 ~7 分钟）。剥离 HDR10+ SEI
                    # 降为静态 HDR10（母版/亮度静态元数据保留），视频位流其余
                    # 部分不动。
                    if progress:
                        progress(41, "正在剥离 HDR10+ 动态元数据（保留静态 HDR10）")
                    # HDR10+ 是单层视频的动态元数据处理，不是 DV Profile 7。
                    # 只能取 MPLS 官方主视频 PID，绝不能要求/猜测 EL。
                    video_pid = self._primary_video_pid(asset, video_streams)
                    video_es = source.with_name(source.name + ".video.hevc")
                    stripped = source.with_name(source.name + ".video.hevc.nostatic-dyn.hevc")
                    # round-2 P1-3：写入前登记。
                    temporaries += [video_es, stripped]
                    self._extract_dv_layer(source, video_pid, video_es)
                    _strip_dynamic_sei(video_es)
                    command = build_mkvmerge_command_dv(
                        asset, resolved, stripped, source, output, title=title,
                    )
                else:
                    command = build_mkvmerge_command(asset, resolved, source, output, title=title)
            command[0] = self.executable
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                if progress:
                    # mkvmerge emits non-progress status lines after the final
                    # 100% update (for example "Multiplexing took ..." and
                    # cue-index messages).  Treating those lines as 0% made
                    # the external delivery projection jump backwards after
                    # a completed mux phase.  Only publish a progress value
                    # when the line actually contains a numeric percentage.
                    percent = None
                    if "#GUI#progress" in line and "%" in line:
                        try:
                            raw = int(line.split("#GUI#progress", 1)[1].split("%", 1)[0].strip())
                            percent = 58 + min(41, int(raw * 41 / 100))
                        except ValueError:
                            pass
                    elif "Progress:" in line and "%" in line:
                        try:
                            raw = int(line.split("Progress:", 1)[1].split("%", 1)[0].strip())
                            percent = 58 + min(41, int(raw * 41 / 100))
                        except ValueError:
                            pass
                    if percent is not None:
                        progress(percent, line.strip())
            code = process.wait()
            if code != 0:
                raise MatroskaBuildError(f"mkvmerge 生成失败（退出码 {code}）")
            completed = subprocess.run(
                [self.executable, "--identification-format", "json", "--identify", str(output)],
                check=True, capture_output=True, text=True, timeout=300,
            )
            result = validate_finalized_identification(
                asset, resolved, json.loads(completed.stdout),
                dolby_vision=dolby_vision,
            )
            self._verify_seekable(output, dv_facts=dv_facts)
            if dolby_vision:
                # BATCH-006 R4 对齐：与 FifoMatroskaBuilder 一致，DV 成品执行
                # 完整 Profile 7 证据链（RPU 载荷 + EL 融合大小 + dovi_tool
                # 命令事实），再由 ffprobe 验证 DOVI 配置记录。此前仅 seek
                # 正交 + _verify_dolby_vision，缺 RPU/EL 硬门。
                _verify_dv_profile7_evidence(self.ffmpeg, output, dv_facts)
                self._verify_dolby_vision(output)
            return result
        except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as error:
            raise MatroskaBuildError(f"最终 MKV 校验失败：{error}") from error
        finally:
            if not self._source_checkpoint_is_valid(asset, source):
                source.unlink(missing_ok=True)
            for path in temporaries:
                path.unlink(missing_ok=True)


class FifoMatroskaBuilder:
    """Stream-copy MPLS title into Matroska via a FIFO, no intermediate file.

    libbluray writes the selected title's raw M2TS packets into a named pipe;
    ffmpeg reads from the pipe and remuxes into a seekable MKV with stream
    copy (``-c copy``).  This avoids the 2× disk space and the extra full-file
    write of the materialise-then-mkvmerge path.

    ffmpeg performs a streaming probe of the TS input before muxing.  For
    Blu-ray M2TS it discovers every PMT-declared PID, including PGS subtitles
    that the Zidoo native player never enumerates from a raw TS transport.
    """

    def __init__(self, executable: str = "ffmpeg", probe: str = "ffprobe"):
        self.executable = executable
        self.probe = probe

    @staticmethod
    def _probe_tracks(probe_path: str, fifo: Path) -> list[dict]:
        """Run ffprobe on the FIFO to discover all elementary streams.

        ffprobe reads just enough data to parse PAT/PMT and enumerate streams,
        then exits.  The caller must keep the FIFO writer alive during the
        probe or ffprobe will block forever.
        """
        try:
            completed = subprocess.run(
                [probe_path, "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_entries",
                 "stream=index,codec_type,codec_name:stream_tags=language",
                 str(fifo)],
                check=True, capture_output=True, text=True, timeout=120,
            )
        except subprocess.SubprocessError as error:
            raise MatroskaBuildError(
                f"ffprobe 探测失败：{str(getattr(error, 'stderr', error))[:300]}"
            ) from error
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise MatroskaBuildError(f"ffprobe 返回无效 JSON：{error}") from error
        return data.get("streams") or []

    @staticmethod
    def _select_streams(
        streams: list[dict], asset: BlurayTitleAsset,
    ) -> list[str]:
        """Build ffmpeg ``-map`` arguments from probed streams and MPLS metadata.

        The MPLS track table tells us which PIDs are official.  ffprobe assigns
        sequential indices to every elementary stream it discovers; a TrueHD
        PID appears twice (TrueHD + AC-3 core).  We keep only the stream whose
        codec matches the MPLS coding type for each official PID.
        """
        maps: list[str] = []
        # Always keep the first video stream.
        for s in streams:
            if s.get("codec_type") == "video":
                maps.append(f"0:{s['index']}")
                break
        # Map official audio PIDs by codec match.
        codec_by_type = {
            0x80: "pcm",
            0x81: "ac-3",  # but NOT truehd
            0x83: "truehd",
            0x84: "eac3",
            0x85: "dts",  # matches dts expressions
            0x86: "dts",
            0xA1: "eac3",
            0xA2: "dts",
        }
        for official in asset.audio_tracks:
            for s in streams:
                if s.get("codec_type") != "audio":
                    continue
                # ffmpeg stores the TS PID under "id" as a hex string like "0x1100"
                sid = s.get("id")
                if sid is None:
                    continue
                try:
                    pid = int(sid, 0) if isinstance(sid, str) else int(sid)
                except (ValueError, TypeError):
                    continue
                if pid != official.pid:
                    continue
                codec = (s.get("codec_name") or "").lower().replace("_", "-")
                expected = codec_by_type.get(official.coding_type, "")
                if expected == "ac-3" and "truehd" in codec:
                    continue  # skip TrueHD when MPLS says AC-3
                if expected and expected not in codec and codec not in expected:
                    continue
                spec = f"0:{s['index']}"
                if spec not in maps:
                    maps.append(spec)
                break
        # Map official subtitle PIDs (PGS).
        for official in asset.subtitle_tracks:
            for s in streams:
                if s.get("codec_type") != "subtitle":
                    continue
                sid = s.get("id")
                if sid is None:
                    continue
                try:
                    pid = int(sid, 0) if isinstance(sid, str) else int(sid)
                except (ValueError, TypeError):
                    continue
                if pid != official.pid:
                    continue
                spec = f"0:{s['index']}"
                if spec not in maps:
                    maps.append(spec)
                break
        if len(maps) < 2:  # at least video + one audio
            raise MatroskaBuildError(
                f"ffmpeg 轨道映射不足：只找到 {maps}，PMT 可能不完整"
            )
        return maps

    @staticmethod
    def _probe_source_layout(asset: BlurayTitleAsset, probe_path: str) -> list[dict] | None:
        """探测磁盘主片流布局（index/pid/codec），用于按 PID 对齐注入。

        FIFO 不能被预探测（ffprobe 会消费管道数据、破坏流完整性），
        因此探测磁盘上的源主片：ffmpeg 的 TS demuxer 对同一内容
        按相同顺序发现流，输出顺序 = PMT 顺序。

        只有 BDMV 可探测（ISO 需要挂载读取，回退旧逻辑）。
        """
        if asset.source_kind != "bdmv":
            return None
        stream_dir = Path(asset.root) / "BDMV" / "STREAM"
        try:
            candidates = [p for p in stream_dir.glob("*.m2ts") if p.is_file()]
            if not candidates:
                return None
            largest = max(candidates, key=lambda p: p.stat().st_size)
            completed = subprocess.run(
                [probe_path, "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_entries",
                 "stream=index,codec_type,codec_name,id",
                 str(largest)],
                check=True, capture_output=True, text=True, timeout=120,
            )
            data = json.loads(completed.stdout)
            return data.get("streams") or []
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _pid_of(stream: dict) -> int | None:
        sid = stream.get("id")
        try:
            return int(sid, 0) if isinstance(sid, str) else int(sid)
        except (TypeError, ValueError):
            return None


    @staticmethod
    def _select_fifo_maps(
        streams: list[dict] | None,
        asset: BlurayTitleAsset,
    ) -> tuple[list[str], list[dict] | None]:
        """FIFO 路径的受控 -map：只输出 MPLS 官方 PID 的流。

        - 视频：仅第一个（DV 盘的 EL 辅助视频流被排除，不降级内容）；
        - 音频：官方 PID，TrueHD 的 AC-3 core（同 PID 第二流）跳过；
        - 字幕：官方 PGS。
        探测失败时回退 -map 0:v:0 / 0:a? / 0:s?。
        """
        if not streams:
            return (["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"], None)
        audio_pids = {t.pid for t in asset.audio_tracks}
        subtitle_pids = {t.pid for t in asset.subtitle_tracks}
        maps: list[str] = []
        selected: list[dict] = []
        seen_pids: set[int] = set()
        for s in streams:
            codec_type = s.get("codec_type")
            if codec_type == "video" and not selected:
                maps.extend(["-map", f"0:{s['index']}"])
                selected.append(s)
                continue
            if codec_type != "audio":
                continue
            pid = FifoMatroskaBuilder._pid_of(s)
            if pid is None or pid not in audio_pids or pid in seen_pids:
                continue
            seen_pids.add(pid)
            maps.extend(["-map", f"0:{s['index']}"])
            selected.append(s)
        for s in streams:
            if s.get("codec_type") != "subtitle":
                continue
            pid = FifoMatroskaBuilder._pid_of(s)
            if pid is not None and pid in subtitle_pids:
                maps.extend(["-map", f"0:{s['index']}"])
                selected.append(s)
        if len(maps) < 2:
            # 至少视频 + 一个音频；否则回退（探测不完整）
            return (["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"], None)
        return (maps, selected)
    @staticmethod
    def _inject_track_metadata(
        cmd: list[str],
        asset: BlurayTitleAsset,
        streams: list[dict] | None,
    ) -> list[str]:
        """按 PID 对齐注入语言/默认标记；LPCM 转码（Matroska 无 tag）。

        输出流顺序 = PMT 顺序（`-map 0:a?` 全选）。TrueHD PID 会展开为
        TrueHD + AC3 core 两个输出流；按 PID 映射可保证语言标签、
        默认音轨与实际流一一对应，不再依赖 MPLS 列表顺序。
        """
        if streams:
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
            audio_lang = {t.pid: (t.language or "und") for t in asset.audio_tracks}
            subtitle_lang = {t.pid: (t.language or "und") for t in asset.subtitle_tracks}

            # 语言 + LPCM 转码（pcm_bluray 无 Matroska codec tag）
            for i, s in enumerate(audio_streams):
                pid = FifoMatroskaBuilder._pid_of(s)
                lang = audio_lang.get(pid, "und") if pid is not None else "und"
                cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
                if (s.get("codec_name") or "").lower() == "pcm_bluray":
                    cmd += [f"-c:a:{i}", "pcm_s16le"]

            # 默认音轨：按 MPLS 首选索引对应的 PID 标记
            default_audio = asset.default_audio_index
            if (
                default_audio is not None and
                0 <= default_audio < len(asset.audio_tracks)
            ):
                default_pid = asset.audio_tracks[default_audio].pid
                for i, s in enumerate(audio_streams):
                    is_default = (FifoMatroskaBuilder._pid_of(s) == default_pid)
                    cmd += [f"-disposition:a:{i}", "default" if is_default else "none"]

            # 字幕语言 + 默认
            for i, s in enumerate(subtitle_streams):
                pid = FifoMatroskaBuilder._pid_of(s)
                lang = subtitle_lang.get(pid, "und") if pid is not None else "und"
                cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
            default_sub = asset.default_subtitle_index
            if (
                default_sub is not None and
                0 <= default_sub < len(asset.subtitle_tracks)
            ):
                default_pid = asset.subtitle_tracks[default_sub].pid
                for i, s in enumerate(subtitle_streams):
                    is_default = (FifoMatroskaBuilder._pid_of(s) == default_pid)
                    cmd += [f"-disposition:s:{i}", "default" if is_default else "none"]
            return cmd

        # 探测失败回退：按 MPLS 顺序注入（旧行为，流布局恰好一致时可用）
        for i, track in enumerate(asset.audio_tracks):
            lang = track.language or "und"
            cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
        default_audio = asset.default_audio_index
        if default_audio is not None:
            for i, track in enumerate(asset.audio_tracks):
                is_default = (track.index == default_audio)
                cmd += [f"-disposition:a:{i}", "default" if is_default else "none"]
        for i, track in enumerate(asset.subtitle_tracks):
            lang = track.language or "und"
            cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
        default_sub = asset.default_subtitle_index
        if default_sub is not None:
            for i, track in enumerate(asset.subtitle_tracks):
                is_default = (track.index == default_sub)
                cmd += [f"-disposition:s:{i}", "default" if is_default else "none"]
        return cmd

    def peak_space_bytes(self, asset: BlurayTitleAsset) -> int:
        """流式 FIFO 无物化主片，峰值只有 ffmpeg 正在写的部分 MKV。"""
        return int(asset.size) + 512 * 1024**2

    def build(
        self,
        asset: BlurayTitleAsset,
        output: Path,
        *,
        title: str,
        progress: Callable[[int, str], None] | None = None,
    ) -> dict:
        if shutil.which(self.executable) is None:
            raise MatroskaBuildError("服务器未安装 ffmpeg")
        if asset.dolby_vision:
            # The FIFO stream-copy path maps a single video PID; a Dolby
            # Vision enhancement layer would be silently dropped. Only the
            # mkvmerge + dovi_tool path may finalize DV titles.
            raise MatroskaBuildError(
                "Dolby Vision 影片需要 mkvmerge/dovi_tool 封装，FFmpeg 降级不可用"
            )
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.unlink(missing_ok=True)
        fifo = output.with_name(output.name + ".fifo")
        try:
            fifo.unlink(missing_ok=True)
            os.mkfifo(fifo)

            import threading

            written = 0
            total = max(1, int(asset.size))
            stop = threading.Event()
            feed_ended: list[str] = []

            def feed_fifo() -> None:
                nonlocal written
                try:
                    with open(fifo, "wb") as f:
                        last_report = 0.0
                        for chunk in iter_bluray_title(asset, chunk_size=1024 * 1024):
                            if stop.is_set():
                                break
                            f.write(chunk)
                            written += len(chunk)
                            if progress:
                                now = time.monotonic()
                                if now - last_report >= 0.5:
                                    last_report = now
                                    pct = min(95, int(written * 95 / total))
                                    progress(pct, f"正在封装：{written / 1024**3:.1f} / {total / 1024**3:.1f} GB")
                except (BrokenPipeError, OSError) as error:
                    # 正常断流（ffmpeg 完成/主动停止）或写入错误：
                    # 记录到全局，供失败诊断确认是否输入提前 EOF。
                    feed_ended.append(f"{type(error).__name__}")
                except Exception as error:
                    # 源读取（UDF/ISO）中途失败：必须记录，否则静默断流
                    feed_ended.append(f"{type(error).__name__}: {error}")
                    raise

            feeder = threading.Thread(target=feed_fifo, daemon=True)
            feeder.start()

            # Single-pass: ffmpeg probes the FIFO stream itself, discovers all
            # PMT-declared PIDs, and remuxes into Matroska with stream copy.
            # No separate ffprobe pass — ffmpeg's internal TS demuxer does a
            # deeper probe when it has the full stream available.
            #
            # ffmpeg's MPEG-TS reader does not carry PMT ISO 639 language codes
            # into the output container, so we inject them manually from the
            # MPLS track table.  The first Chinese track is also flagged as
            # default so the native player selects it automatically.
            # 受控轨道映射：蓝光源含辅助视频（DV EL）、TrueHD AC-3 core、
            # 解说轨等，盲目 -map 0 会全装进去；只输出 MPLS 官方 PID 的流。
            source_streams = self._probe_source_layout(asset, self.probe)
            if dual_hevc_video_streams(source_streams):
                # PMT-level DV detection: some discs keep the EL out of the
                # MPLS stream table. The FIFO path maps a single video PID
                # and would silently drop the enhancement layer.
                raise MatroskaBuildError(
                    "Dolby Vision 影片需要 mkvmerge/dovi_tool 封装，FFmpeg 降级不可用"
                )
            maps, selected_streams = self._select_fifo_maps(source_streams, asset)

            cmd = [
                self.executable, "-y",
                # 源流末尾音频包可能缺失时间戳（ISO 的 TrueHD 流），
                # muxer 缓存包在 trailer 写时 "unknown timestamp" EINVAL。
                # genpts 为缺失 PTS/DTS 的包生成时间戳；正常包不受影响。
                "-fflags", "+genpts",
                # 合理探测参数（FIFO 流发现无需巨大探测）
                "-probesize", "33554432",
                "-analyzeduration", "30000000",
                "-i", str(fifo),
                *maps,
                "-c", "copy",
                "-f", "matroska",
                "-metadata", f"title={title}",
            ]
            # 按输出顺序 + PID 对齐注入（受控映射后输出流 = selected_streams 顺序）
            cmd = self._inject_track_metadata(
                cmd,
                asset,
                selected_streams,
            )
            cmd.append(str(output))
            try:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                assert process.stdout is not None
                ffmpeg_tail: list[str] = []
                for line in process.stdout:
                    ffmpeg_tail.append(line.rstrip())
                    if len(ffmpeg_tail) > 200:
                        ffmpeg_tail.pop(0)
                    if progress and "time=" in line:
                        # ffmpeg progress lines: frame= ... time=00:12:34.56 ...
                        progress(95, line.strip()[-200:])
                code = process.wait()
            finally:
                stop.set()
                feeder.join(timeout=10)

            if code != 0:
                # 失败时带出完整命令与 stderr 尾部，避免诊断盲区
                detail = "\n".join(ffmpeg_tail[-200:])
                feed_note = f"\n[feeder] {feed_ended[-1]}" if feed_ended else ""
                raise MatroskaBuildError(
                    f"ffmpeg 封装失败（退出码 {code}）{feed_note}\n"
                    f"ffmpeg 命令：{' '.join(cmd)}\n{detail}"
                )

            # Validate the output.
            return self._validate_output(asset, output)

        finally:
            fifo.unlink(missing_ok=True)

    def _validate_output(
        self, asset: BlurayTitleAsset, output: Path,
    ) -> dict:
        try:
            completed = subprocess.run(
                [self.probe, "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_entries",
                 "stream=index,codec_type,codec_name:stream_tags=language",
                 str(output)],
                check=True, capture_output=True, text=True, timeout=300,
            )
            data = json.loads(completed.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise MatroskaBuildError(f"最终 MKV 校验失败：{error}") from error
        out_streams = data.get("streams") or []
        videos = [s for s in out_streams if s.get("codec_type") == "video"]
        audio = [s for s in out_streams if s.get("codec_type") == "audio"]
        subs = [s for s in out_streams if s.get("codec_type") == "subtitle"]
        if not videos:
            raise MatroskaBuildError("最终 MKV 没有视频轨")
        if not audio:
            raise MatroskaBuildError("最终 MKV 没有音轨")
        duration = 0
        try:
            dur_completed = subprocess.run(
                [self.probe, "-v", "quiet", "-print_format", "json",
                 "-show_format", str(output)],
                check=True, capture_output=True, text=True, timeout=60,
            )
            dur_data = json.loads(dur_completed.stdout)
            duration = float(dur_data.get("format", {}).get("duration") or 0)
        except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            pass
        return {
            "duration_ns": int(duration * 1_000_000_000) if duration else int(asset.duration_90k) * 1_000_000_000 // 90_000,
            "dolby_vision": len(videos) > 1,
            "audio_tracks": tuple(
                {"index": i, "language": (s.get("tags") or {}).get("language", "")}
                for i, s in enumerate(audio)
            ),
            "subtitle_tracks": tuple(
                {"index": i, "language": (s.get("tags") or {}).get("language", "")}
                for i, s in enumerate(subs)
            ),
        }

def _sei_has_hdr10plus(payload: bytes) -> bool:
    """SEI NAL 负载内是否含 HDR10+ 的 T.35 注册消息（仅 type 4 + 前缀）。

    round-4 P1-1：不得把任意 user_data_unregistered（type 5 厂商私有，
    如 ATEME）当作 HDR10+；ffprobe 帧 side data 只暴露类型名不暴露
    内容，无法区分，因此判定必须在 ES 的 NAL 语义层完成。
    """
    offset = 0
    while offset + 2 <= len(payload):
        payload_type = 0
        byte = payload[offset]
        offset += 1
        while byte == 0xFF and offset < len(payload):
            payload_type += 255
            byte = payload[offset]
            offset += 1
        payload_type += byte
        if offset >= len(payload):
            return False
        payload_size = 0
        byte = payload[offset]
        offset += 1
        while byte == 0xFF and offset < len(payload):
            payload_size += 255
            byte = payload[offset]
            offset += 1
        payload_size += byte
        if offset + payload_size > len(payload):
            return False
        if payload_type == 4:
            message = payload[offset:offset + payload_size]
            if message.startswith(_HDR10PLUS_T35_PREFIX):
                return True
        offset += payload_size
        if byte == 0x80:
            break
    return False


def _hdr10plus_in_es(data: bytes) -> bool:
    """按 Annex B NAL 遍历，判定 ES 是否含 HDR10+（仅 prefix/suffix SEI）。"""
    return _scan_annexb_for_hdr10plus(iter((data,)))


def _scan_annexb_for_hdr10plus(chunks) -> bool:
    """流式 Annex-B 扫描：跨 chunk 识别 start code 与完整 SEI NAL。

    round-5 P1：有界内存——缓冲区只保留当前未完结 NAL（上限
    _MAX_PROBE_NAL_BYTES）；SEI NAL 很小（KB 级），超限 NAL 直接
    跳过（按 start code 推进），不缓存整个流。
    """
    buffer = bytearray()
    read_ptr = 0
    for chunk in chunks:
        buffer.extend(chunk)
        while True:
            marker = buffer.find(b"\x00\x00\x01", read_ptr)
            if marker < 0:
                # 无新 start code：保留尾部两字节防跨界，其余可弃。
                if len(buffer) > 2:
                    del buffer[:-2]
                    read_ptr = 0
                break
            nal_start = marker + 3
            if nal_start + 2 > len(buffer):
                # NAL 头不完整，等下一个 chunk。
                read_ptr = marker
                del buffer[:marker]
                read_ptr = 0
                break
            next_marker = buffer.find(b"\x00\x00\x01", nal_start)
            if next_marker < 0:
                if len(buffer) - nal_start > _MAX_PROBE_NAL_BYTES:
                    # 超大 NAL：跳过（SEI 不可能这么大），推进到尾部。
                    del buffer[:nal_start]
                    read_ptr = 0
                    break
                read_ptr = marker
                if marker > 0:
                    del buffer[:marker]
                    read_ptr = 0
                break
            nal_type = (buffer[nal_start] >> 1) & 0x3F
            if nal_type in (39, 40):
                payload = bytes(buffer[nal_start + 2:next_marker])
                if _sei_has_hdr10plus(payload):
                    return True
            read_ptr = next_marker
            # 已处理部分可弃：保留从 read_ptr 起。
            del buffer[:read_ptr]
            read_ptr = 0
    # 流结束：缓冲区若含一个完整头部的 SEI NAL，处理它。
    if len(buffer) > 5:
        marker = buffer.find(b"\x00\x00\x01", 0)
        if marker >= 0 and marker + 5 <= len(buffer):
            nal_start = marker + 3
            nal_type = (buffer[nal_start] >> 1) & 0x3F
            if nal_type in (39, 40):
                payload = bytes(buffer[nal_start + 2:])
                if _sei_has_hdr10plus(payload):
                    return True
    return False


_MAX_PROBE_NAL_BYTES = 8 * 1024 * 1024
_PROBE_TIMEOUT_SECONDS = 300.0


def _has_hdr10plus_es(ffmpeg: str, path: Path,
                      timeout_seconds: float = _PROBE_TIMEOUT_SECONDS) -> bool:
    """源/输出对称的 HDR10+ 语义探测（round-6 P1：完整生命周期）。

    ffmpeg 将视频前 60 秒作为 HEVC ES 输出到管道，Python 端有界内存
    流式扫描——不落盘、无孤儿文件。生命周期约束：
    - 整体 deadline（默认 300s）：watchdog 到时 kill 子进程并失败关闭；
    - stderr 由后台线程排空到有界缓冲（避免双管道互等死锁）；
    - 早命中后主动 terminate（记录 killed_after_find）：仅"我方 SIGTERM
      终止"视为合法命中；ffmpeg 自身任何非零退出（即使已发现 HDR10+）
      一律失败关闭；
    - stdout 读取异常：先 kill 再回收，抛错。
    """
    import threading
    from collections import deque

    command = [
        ffmpeg, "-v", "error", "-i", str(path),
        "-map", "0:v:0", "-c", "copy", "-f", "hevc", "-t", "60",
        "pipe:1",
    ]
    try:
        # 独立进程组：超时必须杀整组——ffmpeg 的子进程（或异常 shell 的
        # 孤儿子进程）继承管道写端，只杀直接子进程会让读端永不 EOF。
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except OSError as error:
        raise TransientMatroskaBuildError(f"HDR10+ 探测启动失败：{error}") from error
    assert process.stdout is not None and process.stderr is not None

    timed_out = [False]
    watchdog_release = threading.Event()

    def _kill_group() -> None:
        """杀整个进程组（孤儿子进程也持有管道写端）。"""
        try:
            if os.name == "posix":
                import signal as _signal
                os.killpg(os.getpgid(process.pid), _signal.SIGKILL)
                return
        except (OSError, ProcessLookupError):
            pass
        try:
            process.kill()
        except OSError:
            pass

    def _watchdog() -> None:
        if not watchdog_release.wait(timeout_seconds):
            timed_out[0] = True
            _kill_group()

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()

    stderr_tail: deque[str] = deque(maxlen=16)

    def _drain_stderr() -> None:
        try:
            for line in process.stderr:
                stderr_tail.append(line.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass

    drain = threading.Thread(target=_drain_stderr, daemon=True)
    drain.start()

    found = False
    # round-7 P1：只记录"我方明确发出的受控终止信号"，不再用
    # killed_after_find（只证明扫描命中）接受任意负退出码。
    _termination_signal: int | None = None
    failure: Exception | None = None
    try:
        def _chunks():
            while True:
                # read1：有数据立即返回（read(n) 会等待凑满 n 字节或 EOF，
                # 早命中场景子进程不退出时将永久阻塞——round-6 P1）。
                piece = process.stdout.read1(1024 * 1024)
                if not piece:
                    break
                yield piece
        found = _scan_annexb_for_hdr10plus(_chunks())
    except OSError as error:
        failure = error
        _kill_group()
    else:
        if found:
            # 早命中：先给短宽限期等子进程自然退出（真实 ffmpeg 收到
            # stdout EOF/SIGPIPE 后立即结束）。宽限期内自行结束的，
            # 退出码就是它自己的（0 接受、非零/自杀信号拒绝）——
            # 立即 SIGTERM 会与自杀信号赛跑，无法归属（round-7 P1）。
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                # 宽限后仍在运行：我方受控终止（SIGTERM 整组），
                # 只有与我方信号严格对应的死亡才算合法早命中。
                import signal as _signal
                _termination_signal = _signal.SIGTERM
                try:
                    if os.name == "posix":
                        os.killpg(os.getpgid(process.pid), _signal.SIGTERM)
                    else:
                        process.terminate()
                except (OSError, ProcessLookupError):
                    pass
    finally:
        watchdog_release.set()
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass
        # 顺序很关键：先回收子进程（进程死后 stderr EOF，drain 线程的
        # readline 自然返回、锁释放），再 join/close——在 readline 阻塞
        # 时 close stderr 会等内部锁，形成主线程死锁（round-6 P1）。
        try:
            code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            # SIGTERM 后仍未退出（异常 ffmpeg）：升级 SIGKILL 整组回收，
            # 并记录升级信号（与自杀崩溃区分）。
            import signal as _signal2
            _termination_signal = _signal2.SIGKILL
            _kill_group()
            try:
                code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                code = -99  # 不可回收：按失败关闭处理。
        drain.join(timeout=10)
        try:
            process.stderr.close()
        except (OSError, ValueError):
            pass
        watchdog.join(timeout=5)

    if timed_out[0]:
        raise MatroskaBuildError(
            f"HDR10+ 探测超时（>{timeout_seconds:.0f}s），子进程已终止，拒绝发布"
        )
    if failure is not None:
        raise MatroskaBuildError(
            f"HDR10+ 探测读取失败（子进程已终止回收）：{failure}"
        ) from failure
    if code != 0:
        import signal as _signal3
        if (_termination_signal is not None
                and code == -int(_termination_signal)):
            # 与我方明确发出的受控终止严格对应：合法早命中。
            return found
        detail = "".join(stderr_tail).strip()
        signal_note = (
            f"信号 {_code_signal_name(code)}" if code and code < 0
            else f"退出码 {code}"
        )
        raise MatroskaBuildError(
            f"HDR10+ 探测失败（{signal_note}）"
            f"{(': ' + detail[-200:]) if detail else ''}"
        )
    return found


def _code_signal_name(code: int) -> str:
    import signal
    try:
        return signal.Signals(-code).name
    except ValueError:
        return str(-code)


def _verify_cues_and_seek(probe: str, output: Path, duration_ns: int,
                          diagnostics: dict | None = None,
                          *,
                          dv_facts: dict | None = None,
                          ffmpeg: str = "ffmpeg") -> dict:
    """结构化 Cues + 多点时间窗口 seek（Codex P1-B / round-17 B）。

    - 结构化 EBML Cues（>=1 个完整 CuePoint，非裸字节搜索）；
    - 10%/50%/90%（按 MPLS 期望时长）各用 `target%+10` 时间窗口读取视频
      packet 与 decoded frame。

    非 DV 路线（dv_facts=None）保持：「有包无帧」即失败。

    Profile 7 路线（round-17 B/C）：完整合并双层流交给通用 decoder 可能解不出
    帧（容器内 libavcodec 对双层 HEVC 不支持），这不能单独判成品不可播放。改为
    每个目标点拆分正交证据：
      1. Cues/packet 落到目标附近且窗口含关键访问单元；
      2. stream-copy 提取有界合并 HEVC，`dovi_tool demux -b` 分离同窗 BL；
      3. 该 BL 必须实际解出帧，首帧时间与目标容差一致；
      4. 现有最终 MKV 全片 EL 严格相等、0/40/80 RPU、Profile 7/SPS 继续作为
         不可替代的硬门（在 _verify_dv_profile7_evidence 已先执行并通过）。
    完整合并流的 ffprobe frame 结果记录为诊断（可为 0）。失败分类见代码内注释。
    """
    cue_points = _count_cue_points(output)
    if diagnostics is not None:
        diagnostics["cue_points"] = cue_points
    if cue_points <= 0:
        raise MatroskaBuildError("成品缺少结构化 Cues 索引，拒绝发布")
    duration = duration_ns / 1_000_000_000
    if duration <= 0:
        raise MatroskaBuildError("成品时长无效，无法验证 seek")
    dv = bool(dv_facts) and bool(dv_facts.get("dovi_executable"))
    dovi_executable = str((dv_facts or {}).get("dovi_executable") or "")
    seek_points: list[dict] = []
    if diagnostics is not None:
        diagnostics["seek_points"] = seek_points
    results: list[dict] = []
    for frac in _SEEK_TARGETS:
        target = duration * frac
        point: dict = {"frac": frac, "target_seconds": round(target, 3)}
        seek_points.append(point)
        packets = _probe_interval_packets(probe, output, target, diag=point)
        point["packet_count"] = len(packets)
        point["first_packet"] = packets[0] if packets else None
        point["last_packet"] = packets[-1] if packets else None
        if not packets:
            point["failure"] = "no_packets"
            raise MatroskaBuildError(
                f"成品 seek {frac:.0%}（{target:.1f}s）索引/时间轴定位失败："
                f"窗口内无视频包，拒绝发布"
            )
        # round-17 B.1 / round-18 P1-2：窗口必须含关键访问单元（K 包）且首包
        # PTS 覆盖/接近目标点（DV 路线硬门；非 DV 仍以原解码帧门为准）。
        has_key_au = any(
            ("K" in (p.get("flags") or "")) for p in packets
        )
        point["window_has_key_au_packet"] = has_key_au
        first_pts = _packet_pts(packets[0])
        point["first_packet_pts"] = first_pts

        if dv:
            # round-18 P1-2 / round-19 P1-3：DV 路线强制关键 AU + 首包 PTS 对齐，
            # PTS 缺失或越界即失败关闭。
            if not has_key_au:
                point["failure"] = "no_key_au"
                raise MatroskaBuildError(
                    f"成品 seek {frac:.0%}（{target:.1f}s）窗口内无关键访问单元（K 包），"
                    f"拒绝发布"
                )
            if first_pts is None:
                point["failure"] = "packet_pts_missing"
                raise MatroskaBuildError(
                    f"成品 seek {frac:.0%}（{target:.1f}s）首包 PTS 缺失，拒绝发布"
                )
            if not (target - _SEEK_FRAME_TOLERANCE <= first_pts <= target + _SEEK_FRAME_TOLERANCE):
                point["failure"] = "packet_out_of_range"
                point["first_packet_pts"] = round(first_pts, 3)
                raise MatroskaBuildError(
                    f"成品 seek {frac:.0%} 首包 PTS 越界（期望 {target:.1f}s，"
                    f"实际 {first_pts:.1f}s），拒绝发布"
                )
            # ===== Profile 7 路线：正交 BL 解码门（round-17 B / round-18 B） =====
            full_frames = _probe_interval_frames(probe, output, target, diag=point)
            point["full_profile7_frame_count"] = len(full_frames)
            point["full_profile7_first_frame_seconds"] = (
                full_frames[0] if full_frames else None
            )
            fps = float((dv_facts or {}).get("fps") or 0)
            code = _verify_dv_seek_window(
                probe, ffmpeg, dovi_executable, output, target, point, fps=fps,
                temp_dir=None,
            )
            # 失败分类 C（BL 不可解等已在 _verify_dv_seek_window 抛错）：
            point["classification"] = code
            frames = full_frames  # 记录用；通过与否由 BL 门决定
            point["frame_count"] = len(full_frames)
            point["first_frame_seconds"] = full_frames[0] if full_frames else None
            point["last_frame_seconds"] = full_frames[-1] if full_frames else None
            results.append({
                "frac": frac, "target_seconds": round(target, 3),
                "packet_count": len(packets),
                "full_profile7_frame_count": len(full_frames),
                "bl_decoded_frames": point.get("bl_decoded_frames"),
                "bl_coverage_required_frames": point.get("bl_required_frames"),
                "classification": code,
            })
            continue

        # ===== 非 DV 路线（原逻辑不变）=====
        frames = _probe_interval_frames(probe, output, target, diag=point)
        if not frames:
            # Retry with a wider but bounded decoder window before rejecting.
            point["fallback_frame_probe"] = "widened_60s"
            frames = _probe_interval_frames(
                probe, output, target, diag=point,
                lookback_seconds=_SEEK_FALLBACK_LOOKBACK_SECONDS,
            )
            point["fallback_frame_count"] = len(frames)
        point["frame_count"] = len(frames)
        point["first_frame_seconds"] = frames[0] if frames else None
        point["last_frame_seconds"] = frames[-1] if frames else None
        if not frames:
            point["failure"] = "packets_but_no_frames"
            raise MatroskaBuildError(
                f"成品 seek {frac:.0%}（{target:.1f}s）关键帧/解码窗口失败："
                f"有视频包但窗口内无解码帧，拒绝发布"
            )
        first = min(frames)
        if not (target - _SEEK_FRAME_TOLERANCE <= first
                <= target + _SEEK_FRAME_TOLERANCE):
            point["failure"] = "out_of_range"
            point["first_frame_seconds"] = round(first, 3)
            raise MatroskaBuildError(
                f"成品 seek {frac:.0%} 时间越界（期望 {target:.1f}s，"
                f"实际 {first:.1f}s），拒绝发布"
            )
        point["status"] = "ok"
        results.append({
            "frac": frac, "target_seconds": round(target, 3),
            "packet_count": len(packets), "frame_count": len(frames),
            "first_frame_seconds": round(first, 3),
        })
    return {"cues": results}


def _verify_dv_seek_window(probe: str, ffmpeg: str, dovi: str, output: Path,
                           target: float, point: dict, fps: float,
                           temp_dir: Path | None) -> str:
    """round-19 B/C：对 Profile 7 成品在某目标点做正交 BL 解码门。

    步骤：
      1. 定位目标前一关键访问单元（有界窗口，非全片扫描）；
      2. 从该关键 AU 起按需提取合并 HEVC 窗口（**动态帧数** = 覆盖目标所需，带硬上限）；
      3. `dovi_tool remove` 分离同窗 BL（BL-only，无共享 EL 输出）；
      4. 对 BL 用 ffprobe `-count_frames` 计数实际解码帧，必须 >= 覆盖目标所需。
    绝对目标对齐由调用方 packet 探测（首包 PTS 覆盖目标）与目标前一关键 AU 建立。

    round-19 简化的失败语义（Codex）：不再试图区分"容器/参数集缺陷"与"真实 BL 损坏"
    ——那两项在工具链下无法用两条真正不同的输入可靠构造，属虚假细分类。统一按
    「BL 窗口未证明覆盖目标」失败关闭（保留完整合并流 0 帧时的
    full_profile7_decoder_unsupported 通过分类）。
    """
    from tempfile import mkdtemp
    import shutil as _st
    workdir = Path(temp_dir) if temp_dir is not None else Path(mkdtemp(prefix="hda_dvseek_"))
    own = temp_dir is None
    merged = workdir / f"win_{point['frac']:.0f}.merged.hevc"
    bl = workdir / f"win_{point['frac']:.0f}.bl.hevc"
    try:
        start = _prior_key_au_seconds(output, probe, target)
        point["prior_key_au_seconds"] = round(start, 3) if start is not None else None
        if start is None:
            point["classification"] = "no_prior_key_au"
            raise MatroskaBuildError(
                f"DV seek {point['frac']:.0%}（{point['target_seconds']}s）目标前一有界"
                f" 窗口内无关键访问单元，拒绝发布"
            )
        # round-21 P1：分离「覆盖门槛」与「抽取预算」——不得再混为同一 need。
        span = max(0.0, target - start)
        coverage = _dv_coverage_required_frames(fps, start, target)
        extract = _dv_extract_requested_frames(coverage)
        point["fps"] = fps
        point["span_seconds"] = round(span, 3)
        point["coverage_required_frames"] = coverage
        point["extract_requested_frames"] = extract
        point["extract_start_seconds"] = round(start, 3)
        if extract > _DV_EXTRACT_FRAME_CAP:
            point["classification"] = "window_span_exceeds_cap"
            raise MatroskaBuildError(
                f"DV seek {point['frac']:.0%}（{point['target_seconds']}s）抽取预算"
                f" {extract} 超过有界窗口上限 {_DV_EXTRACT_FRAME_CAP}，无法在有界窗口"
                f" 证明，拒绝发布"
            )
        _extract_window_merged(ffmpeg, output, start, extract, merged)
        # round-19 P1-2：目标窗口必须含 IRAP（HEVC 16..23）；参数集来源如实记录。
        au = _hevc_au_ir_and_params(merged)
        point["window_au"] = au
        if not au.get("first_au_is_ir"):
            point["classification"] = "no_ir_in_window"
            raise MatroskaBuildError(
                f"DV seek {point['frac']:.0%}（{point['target_seconds']}s）目标窗口"
                f" 缺失 IRAP（HEVC 16..23），拒绝发布"
            )
        _demux_bl_only(dovi, merged, bl)
        # P1-1：以实际解码帧数计（不依赖裸 ES 时间戳）。
        bl_count = _count_decoded_frames(probe, bl)
        point["bl_decoded_frames"] = bl_count
        point["bl_required_frames"] = coverage  # 通过门槛=覆盖门槛，非抽取预算
        # round-21 P1：通过条件 = decoded >= coverage_required_frames
        # （CEIL(span*fps)+2，含起始帧与一目标后样本），且要求至少一个目标后样本
        # 已被当前 span 覆盖——45>=39 即通过；47 含余量不作为通过门槛。
        if bl_count < coverage:
            point["classification"] = "bl_window_not_proven_cover_target"
            raise MatroskaBuildError(
                f"DV seek {point['frac']:.0%}（{point['target_seconds']}s）BL 窗口未证明覆盖目标"
                f"（解码帧 {bl_count} < 覆盖门槛 {coverage}），拒绝发布"
            )
        full = point.get("full_profile7_frame_count")
        code = "full_profile7_decoder_unsupported" if not full else "decodable"
        point["classification"] = code
        return code
    finally:
        if own:
            _st.rmtree(workdir, ignore_errors=True)


def _dv_coverage_required_frames(fps: float, start: float, target: float) -> int:
    """round-21 P1：从 prior key（start）解码到目标时刻并再保留一个目标后样本所需的
    最低帧数（真实覆盖门槛）。语义：ceil((target-start)*fps) + 2（含起始帧与一目标后
    样本），最小不低于 2。只能作解码通过门槛，不能与抽取余量混用。"""
    if fps <= 0:
        raise MatroskaBuildError("DV seek 帧率非法，无法推导覆盖门槛")
    span = max(0.0, target - start)
    import math as _m
    coverage = _m.ceil(span * fps) + 2
    return max(2, coverage)


def _dv_extract_requested_frames(coverage: int) -> int:
    """round-21 P1：抽取预算 = 覆盖门槛 + 有界尾部抽取余量（可复用原 10 帧余量），
    仅影响抽取数量，不影响解码通过门槛。由调用方以 _DV_EXTRACT_FRAME_CAP 约束。"""
    return coverage + _DV_EXTRACT_FRAME_TAIL


def _dv_window_needed_frames(fps: float, start: float, target: float) -> int:
    """[已弃用拆分前的合并语义——保留为兼容别名，返回抽取预算；通过门槛见
    _dv_coverage_required_frames]。round-22 后仅测试/诊断引用。"""
    coverage = _dv_coverage_required_frames(fps, start, target)
    return _dv_extract_requested_frames(coverage)




def _prior_key_au_seconds(output: Path, probe: str, target: float) -> float | None:
    """定位目标点之前的最近关键访问单元（容器 K 包）的秒数，作为提取起点。

    round-19 P1-1：只在目标附近的**有界窗口**读取（target-N%+N），不从片头扫到
    目标；找不到时返回 None（调用方失败关闭），与"片头关键包"明确区分。
    """
    lo = max(0.0, target - _SEEK_WINDOW_SECONDS)
    interval = f"{lo:.3f}%+{_SEEK_WINDOW_SECONDS}"
    cmd = [
        probe, "-v", "error", "-select_streams", "v:0",
        "-read_intervals", interval,
        "-show_packets",
        "-show_entries", "packet=pts_time,flags",
        "-of", "json", str(output),
    ]
    point = {"key_au_cmd": " ".join(cmd), "key_au_interval": interval}
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as error:
        raise MatroskaBuildError(
            f"关键 AU 定位失败：{str(getattr(error, 'stderr', '') or error)[-300:]}"
        ) from error
    if completed.returncode != 0:
        raise MatroskaBuildError(
            f"关键 AU 定位失败（退出码 {completed.returncode}）"
        )
    try:
        packets = json.loads(completed.stdout).get("packets") or []
    except json.JSONDecodeError as error:
        raise MatroskaBuildError(f"关键 AU 定位返回无效 JSON：{error}") from error
    best: float | None = None
    for p in packets:
        if "K" not in (p.get("flags") or ""):
            continue
        try:
            pts = float(p.get("pts_time") or 0)
        except (TypeError, ValueError):
            continue
        if pts <= target and (best is None or pts > best):
            best = pts
    return best








def _demux_bl_only(dovi: str, merged: Path, bl_out: Path) -> None:
    """Extract the base layer from a merged HEVC ES using dovi_tool `remove`
    (official BL-only semantics: outputs only the BL, no implicit shared
    EL.hevc — avoiding cross-slot leakage). round-17 B.3 / P1-3."""
    cmd = [dovi, "remove", "-i", str(merged), "-o", str(bl_out)]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as error:
        raise MatroskaBuildError(
            f"seek 窗口 BL 分离失败：{str(getattr(error, 'stderr', '') or error)[-300:]}"
        ) from error
    if completed.returncode != 0:
        raise MatroskaBuildError(
            f"seek 窗口 BL 分离失败（退出码 {completed.returncode}）："
            f"{(completed.stderr or '')[-300:]}"
        )
    if not bl_out.is_file() or bl_out.stat().st_size == 0:
        raise MatroskaBuildError("seek 窗口 BL 分离输出为空，拒绝继续")


def _count_decoded_frames(probe: str, es_path: Path) -> int:
    """Count actually-decoded video frames of a standalone HEVC ES (e.g. a
    separated BL). Uses ffprobe `-count_frames` → nb_read_frames, so it is
    independent of the presence/absence of absolute PTS in a raw ES
    (round-18 P1-1: a decodable raw BL that has no timestamps must count as
    frames, not as 0)."""
    cmd = [
        probe, "-v", "error", "-select_streams", "v:0",
        "-count_frames",
        "-show_entries", "stream=nb_read_frames,nb_read_packets",
        "-of", "json", str(es_path),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as error:
        raise MatroskaBuildError(
            f"DV BL 解码帧数探测失败：{str(getattr(error, 'stderr', '') or error)[-300:]}"
        ) from error
    if completed.returncode != 0:
        raise MatroskaBuildError(
            f"DV BL 解码帧数探测失败（退出码 {completed.returncode}）："
            f"{(completed.stderr or '')[-300:]}"
        )
    try:
        streams = json.loads(completed.stdout).get("streams") or []
    except json.JSONDecodeError as error:
        raise MatroskaBuildError(f"DV BL 解码帧数返回无效 JSON：{error}") from error
    if not streams:
        raise MatroskaBuildError("DV BL 解码帧数探测空流，拒绝继续")
    try:
        return int(streams[0].get("nb_read_frames") or 0)
    except (TypeError, ValueError):
        raise MatroskaBuildError("DV BL 解码帧数探测返回非法值，拒绝继续")


_SEEK_TARGETS: tuple[float, ...] = (0.10, 0.50, 0.90)
_SEEK_WINDOW_SECONDS = 10.0
_SEEK_FALLBACK_LOOKBACK_SECONDS = 60.0
_SEEK_FRAME_TOLERANCE = 20.0
_DV_EXTRACT_FRAME_CAP = 2000  # round-21：DV 窗口抽取预算的有界硬上限
_DV_EXTRACT_FRAME_TAIL = 10   # round-21：覆盖门槛之外的尾部抽取余量（仅抽取，非通过门槛）


def _packet_pts(packet: dict | None) -> float | None:
    """Extract a packet's PTS (seconds) or None."""
    if not packet:
        return None
    try:
        pts = packet.get("pts_time")
        if pts is None:
            return None
        return float(pts)
    except (TypeError, ValueError):
        return None


def _probe_interval_packets(probe: str, path: Path, target: float,
                            diag: dict | None = None) -> list[dict]:
    """读目标时间窗口内选中视频流的 packet（PTS/DTS/flags/pos）。
    子进程 rc/stderr/命令写入 diag（Codex P1-3 增量诊断）。"""
    command = [
        probe, "-v", "error", "-select_streams", "v:0",
        "-read_intervals", f"{target:.3f}%+{_SEEK_WINDOW_SECONDS}",
        "-show_packets",
        "-show_entries", "packet=pts_time,dts_time,flags,pos",
        "-of", "json", str(path),
    ]
    if diag is not None:
        diag["packet_command"] = " ".join(command)
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
        if diag is not None:
            diag["packet_rc"] = getattr(error, "returncode", None)
            diag["packet_stderr"] = str(detail)[-300:]
        raise MatroskaBuildError(
            f"成品 seek packet 探测失败：{str(detail).strip()[-300:]}"
        ) from error
    if diag is not None:
        diag["packet_rc"] = completed.returncode
        diag["packet_stderr"] = (completed.stderr or "")[-300:]
    try:
        packets = json.loads(completed.stdout).get("packets") or []
    except json.JSONDecodeError as error:
        raise MatroskaBuildError(f"成品 seek packet 探测返回无效 JSON：{error}") from error
    return packets


def _probe_interval_frames(probe: str, path: Path, target: float,
                           diag: dict | None = None,
                           *, lookback_seconds: float = _SEEK_WINDOW_SECONDS) -> list[float]:
    """读目标时间窗口内选中视频流的 decoded frame 时间（best_effort）。
    子进程 rc/stderr/命令写入 diag（Codex P1-3 增量诊断）。"""
    # ffprobe's interval seek is allowed to start exactly at ``target`` for
    # packet inspection, but that is too strict for decoded frames: when the
    # nearest preceding keyframe is outside the interval, ffprobe can return
    # packets yet decode zero frames.  Decode from a bounded keyframe-friendly
    # window around the target and let the caller enforce the timestamp
    # tolerance.  This keeps the check seekable while avoiding false failures
    # on long Blu-ray streams with sparse keyframes.
    interval_start = max(0.0, target - lookback_seconds)
    interval_duration = lookback_seconds * 2
    command = [
        probe, "-v", "error", "-select_streams", "v:0",
        # Seek validation only needs decodable keyframe anchors inside the
        # bounded tolerance window.  Decoding every frame in a 120-second
        # UHD window can exceed the watchdog on low-power NAS CPUs even
        # when packet/Cue evidence is valid.
        "-skip_frame", "nokey",
        "-read_intervals", f"{interval_start:.3f}%+{interval_duration}",
        "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time",
        "-of", "csv=p=0", str(path),
    ]
    if diag is not None:
        diag["frame_command"] = " ".join(command)
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
        if diag is not None:
            diag["frame_rc"] = getattr(error, "returncode", None)
            diag["frame_stderr"] = str(detail)[-300:]
        raise MatroskaBuildError(
            f"成品 seek frame 探测失败：{str(detail).strip()[-300:]}"
        ) from error
    if diag is not None:
        diag["frame_rc"] = completed.returncode
        diag["frame_stderr"] = (completed.stderr or "")[-300:]
    times: list[float] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # ffprobe's CSV writer appends frame side-data fields after the
        # timestamp (for example HDR mastering-display/content-light-level
        # metadata).  The timestamp remains the first CSV field; parsing the
        # whole line made valid HDR10 frames look non-numeric and caused a
        # false "packets but no frames" rejection.
        timestamp = line.split(",", 1)[0].strip()
        try:
            times.append(float(timestamp))
        except ValueError:
            continue
    # Some remuxed UHD streams carry valid packet/Cue key access units but do
    # not expose them as decoded ``nokey`` frames through ffprobe.  A bounded
    # ordinary-frame retry avoids rejecting an otherwise seekable output while
    # retaining a hard time/CPU bound (and never falls back to a full scan).
    if not times:
        fallback_command = [
            probe, "-v", "error", "-select_streams", "v:0",
            "-read_intervals", f"{interval_start:.3f}%+{interval_duration}",
            "-show_frames",
            "-show_entries", "frame=best_effort_timestamp_time",
            "-of", "csv=p=0", str(path),
        ]
        if diag is not None:
            diag["frame_fallback_command"] = " ".join(fallback_command)
        try:
            fallback = subprocess.run(
                fallback_command, check=True, capture_output=True,
                text=True, timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as error:
            if diag is not None:
                diag["frame_fallback_rc"] = getattr(error, "returncode", None)
                diag["frame_fallback_stderr"] = str(
                    getattr(error, "stderr", None) or error
                )[-300:]
            return []
        if diag is not None:
            diag["frame_fallback_rc"] = fallback.returncode
            diag["frame_fallback_stderr"] = (fallback.stderr or "")[-300:]
        for line in fallback.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            timestamp = line.split(",", 1)[0].strip()
            try:
                times.append(float(timestamp))
            except ValueError:
                continue
    return times


def _extract_window_merged(ffmpeg: str, media: Path, start: float,
                           frames: int, out: Path) -> None:
    """Stream-copy a bounded window of the video track to a raw merged HEVC ES.
    round-17 B.2: from `start` (a prior key AU / seek anchor), extract the merged
    (BL+EL) HEVC so dovi_tool can separate the same-window BL. round-18 P1-2:
    the start is passed explicitly by the caller (prior key AU boundary), never
    a fixed subtraction."""
    cmd = [
        ffmpeg, "-v", "error", "-nostats",
        "-ss", f"{start:.3f}",
        "-i", str(media),
        "-map", "0:v:0", "-c", "copy", "-f", "hevc",
        "-frames:v", str(frames), str(out),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as error:
        raise MatroskaBuildError(
            f"seek 窗口合并 HEVC 提取失败：{str(getattr(error, 'stderr', '') or error)[-300:]}"
        ) from error
    if completed.returncode != 0:
        raise MatroskaBuildError(
            f"seek 窗口合并 HEVC 提取失败（退出码 {completed.returncode}）："
            f"{(completed.stderr or '')[-300:]}"
        )


def _hevc_au_ir_and_params(path: Path) -> dict:
    """Record whether the first video access unit in `path` is an IRAP and the
    presence of in-stream VPS/SPS/PPS. round-18 P1-2: IRAP NAL types are HEVC
    16..23 (BLA/IDR/CRA + reserved IRAP). Honest reporting: this samples the ES
    head for in-stream VPS/SPS/PPS; it does NOT inspect the container CodecPrivate,
    so the param source is stated as observed in_stream or unknown — never
    claimed as CodecPrivate-checked."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4 * 1024 * 1024)
    except OSError:
        return {"error": "read_failed"}
    vps = sps = pps = aud = False
    first_au_ir_present = False
    seen_any_vcl = False
    i = 0
    n = len(head)
    while i < n - 3:
        if head[i] == 0 and head[i + 1] == 0 and head[i + 2] == 1:
            j = i + 3
            while j < n - 3 and not (head[j] == 0 and head[j + 1] == 0 and head[j + 2] == 1):
                j += 1
            nal = head[i + 3:j]
            if nal:
                t = (nal[0] >> 1) & 0x3F
                if t == 32:
                    vps = True
                elif t == 33:
                    sps = True
                elif t == 34:
                    pps = True
                elif t == 35:
                    aud = True
                elif 16 <= t <= 23:  # HEVC IRAP: BLA(16-18)/IDR(19-20)/CRA(21)/RSV(22-23)
                    if not seen_any_vcl:
                        first_au_ir_present = True
                    seen_any_vcl = True
                elif t <= 31:  # 其余 VCL（trailing/RADL/RASL 等）
                    seen_any_vcl = True
            i = j
        else:
            i += 1
    return {
        "has_vps_in_stream": vps, "has_sps_in_stream": sps, "has_pps_in_stream": pps,
        "has_aud": aud,
        "first_au_is_ir": first_au_ir_present,
        "saw_any_vcl": seen_any_vcl,
        "ir_types_covered": "16..23",
    }


def _probe_output_streams(probe: str, path: Path) -> list[dict]:
    """ffprobe 全字段流探测（语言/默认标志/分辨率/DOVI side data）。"""
    try:
        completed = subprocess.run(
            [probe, "-v", "error", "-print_format", "json", "-show_streams",
             str(path)],
            check=True, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
        raise MatroskaBuildError(
            f"成品流探测失败：{str(detail).strip()[-300:]}"
        ) from error
    try:
        return json.loads(completed.stdout).get("streams") or []
    except json.JSONDecodeError as error:
        raise MatroskaBuildError(f"ffprobe 返回无效 JSON：{error}") from error


def _probe_duration_ns(probe: str, path: Path) -> int:
    """ffprobe 容器时长（纳秒）；无效返回 0。"""
    seconds = _probe_duration(probe, path)
    return int(seconds * 1_000_000_000)


def _resolve_official_streams(asset: BlurayTitleAsset,
                               streams: list[dict]) -> dict:
    """按官方轨一次解析全部唯一 stream（round-2 P1-2）。

    map 构造、metadata 注入与输出校验必须复用同一组解析结果，
    禁止后半程再次用 PID setdefault 推断（TrueHD/core 顺序会导致
    分叉：map 选 TrueHD 而校验误用 core）。
    """
    video = None
    if asset.video_tracks:
        video = _resolve_official_stream(
            streams, asset.video_tracks[0].pid, "video",
            asset.video_tracks[0].coding_type,
        )
    audio = [
        (track, _resolve_official_stream(streams, track.pid, "audio", track.coding_type))
        for track in asset.audio_tracks
    ]
    subtitles = [
        (track, _resolve_official_stream(streams, track.pid, "subtitle", track.coding_type))
        for track in asset.subtitle_tracks
    ]
    return {"video": video, "audio": audio, "subtitle": subtitles}


def _resolve_official_stream(streams: list[dict], pid: int, kind: str,
                             coding_type: int) -> dict:
    """为官方 PID 解析唯一 stream（round-1 P1-3）。

    同一 PID 可能暴露多个流（TrueHD 与其内嵌 AC-3 core 同 PID 双流）；
    PID selector（-map i:0x<pid>）会同时命中两者。这里按 PID + 类型筛选
    候选，唯一时取之；多候选时按官方 coding type 唯一判定；仍无法唯一
    判定时失败关闭，禁止静默选取。
    """
    candidates = [
        stream for stream in streams
        if FifoMatroskaBuilder._pid_of(stream) == pid
        and stream.get("codec_type") == kind
    ]
    if not candidates:
        raise MatroskaBuildError(
            f"官方{ '视频' if kind == 'video' else '音轨' if kind == 'audio' else '字幕' }"
            f" PID 0x{pid:x} 未在物化 PMT 中找到，失败关闭"
        )
    if len(candidates) == 1:
        return candidates[0]
    matched = [
        stream for stream in candidates
        if _codec_matches(coding_type, str(stream.get("codec_name") or ""))
    ]
    if len(matched) == 1:
        return matched[0]
    detail = ", ".join(
        f"index={s.get('index')} codec={s.get('codec_name')}" for s in candidates
    )
    raise MatroskaBuildError(
        f"官方 PID 0x{pid:x} 存在 {len(candidates)} 个 {kind} 候选流"
        f"（{detail}）且无法按 coding type 0x{coding_type:02x} 唯一判定，失败关闭"
    )


def _exact_pid_maps(asset: BlurayTitleAsset, streams: list[dict],
                     input_index: int = 0) -> list[str]:
    """官方 PID 与物化 PMT 交集的精确 -map（BATCH-002 §5.2）。

    视频/音轨/PGS 逐 PID 选择（TrueHD 同 PID 的 AC-3 core 是第二流，
    按 PID 映射天然只取第一个）；任一官方 PID 在物化 PMT 中缺失时
    失败关闭，禁止宽泛 map 兜底。
    """
    if not asset.video_tracks:
        raise MatroskaBuildError("MPLS 未声明官方视频轨，失败关闭")
    resolved = _resolve_official_streams(asset, streams)
    maps = ["-map", f"{input_index}:{resolved['video']['index']}"]
    for _track, stream in resolved["audio"]:
        maps += ["-map", f"{input_index}:{stream['index']}"]
    for _track, stream in resolved["subtitle"]:
        maps += ["-map", f"{input_index}:{stream['index']}"]
    return maps


def _inject_official_metadata(cmd: list[str], asset: BlurayTitleAsset,
                              streams: list[dict]) -> list[str]:
    """按官方轨顺序注入语言与默认标志（BATCH-002 §5.2）。

    输出顺序 = -map 顺序 = 官方顺序；语言直接来自 MPLS 官方语言，
    不允许退化为 und。LPCM（pcm_bluray）按既有规则转码 pcm_s16le。
    """
    resolved = _resolve_official_streams(asset, streams)
    for index, (track, source) in enumerate(resolved["audio"]):
        language = (track.language or "und").lower()
        cmd += [f"-metadata:s:a:{index}", f"language={language}"]
        if (source.get("codec_name") or "").lower() == "pcm_bluray":
            cmd += [f"-c:a:{index}", "pcm_s16le"]
    default_audio = asset.default_audio_index
    for index, track in enumerate(asset.audio_tracks):
        is_default = default_audio is not None and track.index == default_audio
        cmd += [f"-disposition:a:{index}", "default" if is_default else "none"]
    for index, track in enumerate(asset.subtitle_tracks):
        language = (track.language or "und").lower()
        cmd += [f"-metadata:s:s:{index}", f"language={language}"]
    default_sub = asset.default_subtitle_index
    for index, track in enumerate(asset.subtitle_tracks):
        is_default = default_sub is not None and track.index == default_sub
        cmd += [f"-disposition:s:{index}", "default" if is_default else "none"]
    return cmd


# ---------------------------------------------------------------------------
# BATCH-002 Gate B：Dolby Vision Profile 7 的字节级证据（ffmpeg 路线）。
#
# 真盘事实（Stand.by.Me 主片 + dovi_tool 2.3.3 mux 实测）：dovi_tool 合并
# 输出的 BL+EL+RPU 流的 SPS **不含** Dolby Vision VUI 扩展，而 ffmpeg 的
# h2645 parser 只从该扩展产生 AV_PKT_DATA_DOVI_CONF——因此 ffmpeg 封装
# 产物（无论是否经 mp4 中转）在 ffprobe/mkvinfo 层面都看不到 DOVI 配置
# （mkvmerge 的 dvcC/hvcE 是它自行从 RPU/EL 推导写出的容器元素，不是读
# 自 SPS）。这不影响播放器：Profile 7 的 RPU/EL 载荷内嵌在每个访问单元。
#
# 修复策略：合并后把 DV VUI 扩展写进 SPS（等价于新版 dovi_tool 的
# --dolby-vision-vui），使交付物携带符合规范的 Profile 7 配置；校验改为
# 直接解析产物视频轨 SPS 的 DV 配置 + 窗口扫描 RPU NAL（字节级、失败关闭）。
# ---------------------------------------------------------------------------


_EL_FP_WINDOWS: tuple[float, ...] = (0.0, 0.1, 0.5, 0.9)
_EL_FP_PER_WINDOW = 3
_EL_FP_SEGMENT_BYTES = 1 << 22
_EL_FP_TIMEOUT_SECONDS = 720.0


def _require_el_window_integrity(fingerprints_by_window: dict, label: str) -> None:
    """Codex round-6 P1-2：0/10/50/90 窗口必须齐全且每窗达到最小指纹数；
    缺失/不足 → evidence-not-enough 失败关闭。"""
    if set(fingerprints_by_window.keys()) != set(_EL_FP_WINDOWS):
        missing = sorted(set(_EL_FP_WINDOWS) - set(fingerprints_by_window.keys()))
        raise MatroskaBuildError(
            f"{label} EL 窗口不齐全（缺失 {missing}），evidence-not-enough，拒绝继续"
        )
    for frac in _EL_FP_WINDOWS:
        if len(fingerprints_by_window[frac]) < _EL_FP_PER_WINDOW:
            raise MatroskaBuildError(
                f"{label} EL 窗口 {frac:.0%} 指纹不足 "
                f"{len(fingerprints_by_window[frac])} < {_EL_FP_PER_WINDOW}，"
                f"evidence-not-enough，拒绝继续"
            )


def _extract_el_window_fingerprints(el_path: Path,
                                    windows: tuple[float, ...] = _EL_FP_WINDOWS,
                                    per_window: int = _EL_FP_PER_WINDOW,
                                    max_nal_bytes: int = 256 * 1024) -> dict:
    """Codex round-5 P1-1：从 EL 原始流按 0/10/50/90% 字节窗口各取前
    `per_window` 个 slice NAL 作为有来源标识的字节指纹。"""
    out: dict[float, list[bytes]] = {}
    try:
        size = el_path.stat().st_size
    except OSError:
        return out
    for frac in windows:
        offset = int(size * frac)
        try:
            with open(el_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(_EL_FP_SEGMENT_BYTES)
        except OSError:
            continue
        fps: list[bytes] = []
        i = 0
        n = len(data)
        while i < n - 3 and len(fps) < per_window:
            if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
                j = i + 3
                while j < n - 3 and not (data[j] == 0 and data[j + 1] == 0 and data[j + 2] == 1):
                    j += 1
                nal = data[i + 3:j]
                if 2 <= len(nal) <= max_nal_bytes and ((nal[0] >> 1) & 0x3F) <= 31:
                    fps.append(bytes(nal))
                i = j
            else:
                i += 1
        if fps:
            out[frac] = fps
    return out


def _bl_hit_indices(bl_path: Path, fingerprints: list[bytes]) -> set[int]:
    """Codex round-6 P1-3：对 BL 做一次有界内存完整流式多模式扫描，返回
    命中指纹的下标集合（片头与后半命中都会被捕获）。"""
    if not fingerprints:
        return set()
    keep = max(len(fp) for fp in fingerprints) - 1
    buffer = bytearray()
    hits: set[int] = set()
    try:
        with open(bl_path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                buffer.extend(chunk)
                for idx, fp in enumerate(fingerprints):
                    if idx not in hits and buffer.find(fp) >= 0:
                        hits.add(idx)
                if len(buffer) > keep:
                    del buffer[:-keep]
    except OSError as error:
        # round-7 P1：BL 打开/中途读取失败必须失败关闭，不得当作"零命中"。
        raise MatroskaBuildError(
            f"BL 负向扫描失败（无法读取 {bl_path}）：{error}，拒绝继续"
        ) from error
    return hits


def _exclude_bl_hits(fingerprints_by_window: dict, bl_path: Path) -> dict:
    """Codex round-5 P1-1 / round-6 P1-3：排除也出现在 BL 中的指纹（EL
    专属证据）。BL 负向排除用一次完整流式扫描（非片头 4 MiB）。任一窗口
    被排除后为空 → 失败关闭。"""
    all_fps = [fp for fps in fingerprints_by_window.values() for fp in fps]
    hits = _bl_hit_indices(bl_path, all_fps)
    hit_set: set[int] = hits
    cleaned: dict[float, list[bytes]] = {}
    offset = 0
    for frac, fps in fingerprints_by_window.items():
        distinct = [
            fp for k, fp in enumerate(fps)
            if (offset + k) not in hit_set
        ]
        offset += len(fps)
        if not distinct:
            raise MatroskaBuildError(
                f"EL 指纹 BL 负向排除失败：窗口 {frac:.0%} 的切片全部命中 BL"
                f"（无 EL 专属证据），拒绝继续"
            )
        cleaned[frac] = distinct
    return cleaned




def _fingerprint_hashes(fingerprints_by_window: dict) -> dict:
    import hashlib as _hashlib
    return {
        f"{frac:.1f}": [_hashlib.sha256(fp).hexdigest()[:16] for fp in fps]
        for frac, fps in fingerprints_by_window.items()
    }


def _el_vcl_parse(el_path: Path) -> tuple[list[str], dict]:
    """Stream an EL raw HEVC stream and return (ordered VCL slice digest list,
    compact stats). See _el_vcl_slice_digests for coordinate/streaming notes.

    Round-11 P2: the returned stats are compact — vcl_count, payload_bytes
    hashed, and a summary SHA-256 of the whole digest sequence — so failure
    diagnostics never need to persist the full digest array."""
    import hashlib
    out: list[str] = []
    payload_bytes = 0
    buffer = bytearray()
    cur_hasher = None
    cur_nal_type: int | None = None

    def _absorb(piece: bytes) -> None:
        nonlocal payload_bytes
        cur_hasher.update(piece)
        if cur_nal_type is not None and cur_nal_type <= 21:
            payload_bytes += len(piece)

    try:
        with open(el_path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    if cur_hasher is None:
                        marker = buffer.find(b"\x00\x00\x01")
                        if marker < 0:
                            if len(buffer) > 2:
                                del buffer[:-2]
                            break
                        nal_start = marker + 3
                        if nal_start + 1 > len(buffer):
                            del buffer[: marker - 1 if marker > 0 else marker]
                            break
                        cur_hasher = hashlib.sha256()
                        cur_nal_type = (buffer[nal_start] >> 1) & 0x3F
                        del buffer[:nal_start]
                    end = buffer.find(b"\x00\x00\x01")
                    if end < 0:
                        if len(buffer) > 3:
                            _absorb(bytes(buffer[:-3]))
                            del buffer[:-3]
                        break
                    payload_end = end - 1 if end > 0 and buffer[end - 1] == 0 else end
                    _absorb(bytes(buffer[:payload_end]))
                    digest = cur_hasher.hexdigest()
                    if cur_nal_type is not None and cur_nal_type <= 21:
                        out.append(digest)
                    del buffer[:payload_end]
                    cur_hasher = None
                    cur_nal_type = None
        if cur_hasher is not None:
            _absorb(bytes(buffer))
            if cur_nal_type is not None and cur_nal_type <= 21:
                out.append(cur_hasher.hexdigest())
    except OSError as error:
        raise TransientMatroskaBuildError(f"EL 流读取失败（{el_path}）：{error}") from error
    return out, {
        "vcl_count": len(out),
        "payload_bytes": payload_bytes,
        "sequence_sha256": (
            hashlib.sha256("\n".join(out).encode()).hexdigest() if out else ""
        ),
    }


def _el_vcl_stats(digests: list[str]) -> dict:
    """Compact EL stats derived only from a digest list: VCL slice count and a
    summary SHA-256 of the whole sequence.

    round-12 P1: this helper does NOT fabricate a `payload_bytes` value (the
    real byte count can only come from _el_vcl_parse while streaming). Callers
    with only the digest list must not claim a payload byte count."""
    import hashlib
    return {
        "vcl_count": len(digests),
        "sequence_sha256": hashlib.sha256(
            "\n".join(digests).encode()
        ).hexdigest() if digests else "",
    }


def _el_vcl_slice_digests(el_path: Path) -> list[str]:
    """Return ordered VCL slice SHA-256 digest list of an EL raw HEVC stream."""
    return _el_vcl_parse(el_path)[0]


def _demux_el_only(dovi: str, ffmpeg: str, media: Path, el_out: Path, *,
                   container: bool, timeout_seconds: float = _EL_FP_TIMEOUT_SECONDS) -> None:
    """Demux the enhancement layer from `media` (a raw merged `.dv.hevc` when
    `container=False`, or the video track of an MP4/MKV container when True)
    into `el_out` using dovi_tool `demux --el-only`.

    Round-9 P1: this is how we obtain an EL in the SAME logical coordinate as
    the source EL — dovi_tool demux is the well-defined inverse of dovi_tool
    mux, restored from the merged stream or from the container's video track.
    Fail closed on non-zero exit, timeout, spawn/read failure."""
    import threading
    import signal as _signal
    if container:
        ffmpeg_cmd = [
            ffmpeg, "-v", "error", "-nostats", "-i", str(media),
            "-map", "0:v:0", "-c", "copy", "-f", "hevc", "-",
        ]
        dovi_cmd = [dovi, "demux", "--el-only", "-i", "-", "-e", str(el_out)]
    else:
        ffmpeg_cmd = None
        dovi_cmd = [dovi, "demux", "--el-only", "-i", str(media), "-e", str(el_out)]
    ffmpeg_proc = None
    dovi_proc = None
    try:
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        ) if ffmpeg_cmd else None
        dovi_proc = subprocess.Popen(
            dovi_cmd,
            stdin=ffmpeg_proc.stdout if ffmpeg_proc else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        if ffmpeg_proc is not None and ffmpeg_proc.stdout is not None:
            ffmpeg_proc.stdout.close()  # let the pipe flow through dovi
    except OSError as error:
        # round-10 P1-3：任一 spawn 失败都必须终止并 wait 已启动的子进程、
        # 删除 el_out，避免在容器回拆（先启动 ffmpeg 后启动 dovi_tool）时
        # 遗留后台读盘进程。
        import signal as _s2
        for p in (dovi_proc, ffmpeg_proc):
            if p is None or p.poll() is not None:
                continue
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(p.pid), _s2.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                p.kill()
            except OSError:
                pass
        for p in (dovi_proc, ffmpeg_proc):
            if p is not None:
                try:
                    p.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        p.kill()
                    except OSError:
                        pass
                    p.wait()
        el_out.unlink(missing_ok=True)
        raise TransientMatroskaBuildError(f"dovi_tool EL 回拆启动失败：{error}") from error
    assert dovi_proc is not None and dovi_proc.stdout is not None
    fed = [False]
    release = threading.Event()

    def _kill_all() -> None:
        for p in (dovi_proc, ffmpeg_proc):
            if p is None or p.poll() is not None:
                continue
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(p.pid), _signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                p.kill()
            except OSError:
                pass

    def _watchdog() -> None:
        if not release.wait(timeout_seconds):
            fed[0] = True
            _kill_all()

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()

    def _drain(pipe) -> None:
        try:
            for _ in pipe:
                pass
        except (OSError, ValueError):
            pass

    drains = [threading.Thread(target=_drain, args=(p.stderr,), daemon=True)
              for p in (dovi_proc, ffmpeg_proc) if p is not None and p.stderr is not None]
    for d in drains:
        d.start()

    read_failed = [False]
    try:
        for _ in dovi_proc.stdout:
            pass
    except OSError:
        read_failed[0] = True
        _kill_all()
    finally:
        release.set()

    def _wait(p) -> None:
        if p is None:
            return
        try:
            p.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            _kill_all()
            p.wait()

    _wait(dovi_proc)
    _wait(ffmpeg_proc)
    for d in drains:
        d.join(timeout=2.0)

    dovi_rc = dovi_proc.returncode if dovi_proc.returncode is not None else -1
    ffmpeg_rc = ffmpeg_proc.returncode if (ffmpeg_proc and ffmpeg_proc.returncode is not None) else 0
    # round-12 P2：所有失败出口（超时/读取异常/非零退出）统一删除 el_out，
    # 不依赖调用方外部 finally——工具可能已写出部分文件。
    if fed[0]:
        el_out.unlink(missing_ok=True)
        raise TransientMatroskaBuildError("dovi_tool EL 回拆超时，失败关闭")
    if read_failed[0]:
        el_out.unlink(missing_ok=True)
        raise TransientMatroskaBuildError("dovi_tool EL 回拆读取异常，失败关闭")
    if dovi_rc != 0 or ffmpeg_rc != 0:
        el_out.unlink(missing_ok=True)
        raise MatroskaBuildError(
            f"dovi_tool EL 回拆失败（dovi rc={dovi_rc}, ffmpeg rc={ffmpeg_rc}），拒绝继续"
        )


def _verify_el_order_consistency(dovi: str, ffmpeg: str, source_el: Path,
                                 target: Path, label: str, *, container: bool,
                                 src_digests: list[str] | None = None,
                                 src_stats: dict | None = None
                                 ) -> dict:
    """Round-9/10 P1-1: prove the enhancement layer present in `target`
    (merged, MP4 or MKV) is the SAME EL as `source_el`, in the correct frame
    order, covering the whole stream and containing no extra/reordered/duplicate
    VCL slices.

    Method: demux `target`'s EL back out (dovi_tool demux --el-only), then
    require the recovered ordered VCL slice digest sequence to be STRICTLY
    EQUAL (count, order, and every digest) to the source EL's sequence.
    This is a single logical coordinate (frame/AU order) for both sampling
    and checking, eliminating the byte/time fraction mismatch flagged in
    round-9 and the loose ordered-subsequence acceptance flagged in round-10
    (which would let inserted/duplicate/foreign VCL slices pass).

    `src_digests` / `src_stats` may be pre-computed once by the caller to
    avoid re-parsing the large source EL stream at every stage. round-12 P1:
    returned stats use the REAL payload byte count (from _el_vcl_parse) when
    available; stats derived only from a digest list omit `payload_bytes`."""
    if src_digests is None:
        if not source_el.is_file():
            raise MatroskaBuildError(f"EL 源流缺失（{source_el}），拒绝继续")
        src_digests, parsed_stats = _el_vcl_parse(source_el)
        if src_stats is None:
            src_stats = parsed_stats
    if len(src_digests) < _EL_FP_PER_WINDOW:
        raise MatroskaBuildError(
            f"{label} 源 EL 无足够 VCL 切片（{len(src_digests)}），拒绝继续"
        )
    el_out = target.with_name(target.name + ".elonly.hevc")
    try:
        _demux_el_only(dovi, ffmpeg, target, el_out, container=container)
        rec_digests, rec_stats = _el_vcl_parse(el_out)
    except Exception:
        raise
    finally:
        el_out.unlink(missing_ok=True)
    # round-10 P1-1：回拆 EL 必须与源 EL 严格相等（数量、顺序、每项摘要），
    # 不得用有序子序列放行插入/重复/混入的外来 slice。
    if rec_digests != src_digests:
        extra = len(rec_digests) - len(src_digests)
        raise MatroskaBuildError(
            f"{label} EL 一致性失败：回拆 EL 与源 EL 的 VCL 切片序列不严格相等"
            f"（source={len(src_digests)} recovered={len(rec_digests)} "
            f"delta={extra:+d}），拒绝继续"
        )
    return {
        "source_vcl_slices": len(src_digests),
        "recovered_vcl_slices": len(rec_digests),
        "source_stats": src_stats if src_stats is not None else _el_vcl_stats(src_digests),
        "recovered_stats": rec_stats,
        "order_ok": True,
        "temporal": "frame-order (AU/vcl sequence), strict equality",
    }


def _verify_dv_profile7_evidence(ffmpeg: str, mkv_path: Path,
                                   dv_facts: dict | None) -> dict:
    """Codex P1-C：Profile 7 的**独立**证据链（不依赖本程序自写的 SPS DV
    VUI 标志——该标志只作为容器/播放器信号）。

    证据链：
    1. RPU 载荷：对产物 10%/50%/90% 窗口扫描内嵌 RPU NAL（type 62/63），
       每窗口都必须存在（播放器实际消费的 DV 元数据）；
    2. 合并阶段一致性：合并流首个窗口 RPU 采样 > 0；合并文件大小必须
       严格大于 BL（EL 数据确实被融合；--discard 单层化时≈BL）；
    3. dovi_tool 合并命令事实（Profile 7，无 --discard，代码路径强制）。
    任一缺失失败关闭。EL 无法被当前工具在比特流层独立识别（真盘 EL NAL
    与 BL 同 layer_id、合并流不带 EL 参数集）——该边界在报告中明确写
    "证据不足"，不用自写 el_present 标志代替。
    """
    if dv_facts is None:
        raise MatroskaBuildError(
            "Dolby Vision 校验缺少合并阶段证据（dv_facts），拒绝发布"
        )
    bl_size = int(dv_facts.get("bl_size") or 0)
    merged_size = int(dv_facts.get("merged_size") or 0)
    duration_s = float(dv_facts.get("duration_s") or 0)
    if bl_size <= 0 or merged_size <= bl_size:
        raise MatroskaBuildError(
            "Dolby Vision 合并产物大小未显著大于 BL（EL 融合证据缺失），拒绝发布"
        )
    # round-15 P1：合并阶段 RPU 证据必须是结构化 tool-native 证据（不是布尔哨兵）。
    merged_rpu_evidence = _verify_merged_rpu_evidence(
        dv_facts.get("merged_rpu_evidence"), limit=_DV_RPU_FRAMES,
    )
    # round-14 P1-2：最终 MKV 的 RPU 时间窗用 duration_s 把 fraction 换算为秒
    # （0/0.4/0.8 是全片比例，不是 0/0.4/0.8 秒——否则三窗都在片头一秒内）。
    rpu = _dv_rpu_evidence(
        ffmpeg, mkv_path, windows=_DV_RPU_WINDOWS, duration_s=duration_s,
    )
    if any(count <= 0 for count in rpu["rpu_per_window"]):
        raise MatroskaBuildError(
            "最终 MKV 视频轨缺少 Dolby Vision RPU 载荷，拒绝降级输出"
        )
    # round-9 P1：EL 独立阶段一致性——最终 MKV 视频轨用 dovi_tool demux
    # --el-only 回拆 EL，并与源 EL 做有序 VCL 切片序列一致性（统一
    # frame/AU 坐标）。替换原"播放时间比例 × EL 字节采样指纹"门。任一
    # 缺失/顺序错/覆盖不足失败关闭，不以自写标志/文件大小/命令文本代替。
    src_digests = dv_facts.get("el_source_digests") or []
    dovi_executable = str(dv_facts.get("dovi_executable") or "")
    if not src_digests or not dovi_executable:
        raise MatroskaBuildError(
            "Dolby Vision EL 独立证据缺失（无源 EL 序列摘要/工具路径），拒绝发布"
        )
    _verify_el_order_consistency(
        dovi_executable, ffmpeg, Path(), mkv_path, "最终 MKV", container=True,
        src_digests=src_digests,
        src_stats=dv_facts.get("el_source_digest_stats"),
    )
    fingerprints = dv_facts.get("el_fingerprints") or {}
    duration_s = float(dv_facts.get("duration_s") or 0)
    # Codex round-6 P1-2：validator 保留窗口完整性检查（聚合诊断，非发布门）。
    if fingerprints and duration_s > 0:
        _require_el_window_integrity(fingerprints, "最终 MKV")
    # DV 点亮修复（2026-08-18）：Profile 7 容器/播放器信号门改用 **ffprobe
    # 权威解析器**读容器级 dvcC（DOVIDecoderConfigurationRecord）。原
    # _verify_dv_profile7_sps 使用自写 SPS 解析器（_HevcBitReader.bit() 是
    # peek 不消费位，把 rbsp_stop_one_bit 误判成 sps_dv_info_present_flag），
    # 对普通 SPS 也假阳性报 Profile 7，且 patch 产物 ffmpeg 不认——真机
    # 因此显示 HDR10 并在数分钟后崩溃。mkvmerge 从码流自动扫描 RPU/EL
    # 生成正确 dvcC；此门直接校验该容器记录（与独立 EL 证据同时通过，
    # 不能互相替代）。
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_side_data", "-of", "json", str(mkv_path)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
        raise MatroskaBuildError(f"DV 配置记录核验失败：{str(detail).strip()[-300:]}") from error
    except json.JSONDecodeError as error:
        raise MatroskaBuildError(f"ffprobe 返回无效 JSON：{error}") from error
    signal = require_dovi_side_data(payload, "7")
    return {
        "rpu_per_window": rpu["rpu_per_window"],
        "rpu_seek_seconds": rpu["seek_seconds"],
        "merged_rpu_evidence": merged_rpu_evidence,
        "bl_size": bl_size,
        "merged_size": merged_size,
        "dovi_command": str(dv_facts.get("dovi_command") or ""),
        "el_fingerprint_hashes": _fingerprint_hashes(fingerprints) if fingerprints else {},
        "el_source_digest_stats": dv_facts.get("el_source_digest_stats")
        or _el_vcl_stats(src_digests),
        "el_evidence": "disc-EL VCL slice sequence preserved in output video track (demux round-trip)",
        "profile7_signal": signal,
    }


def _count_rpu_in_annexb(chunks) -> int:
    """Streaming Annex-B scan counting in-band Dolby Vision RPU NAL units
    (HEVC NAL type 62/63). Bounded memory: only the current NAL is buffered."""
    buffer = bytearray()
    read_ptr = 0
    rpu = 0
    for chunk in chunks:
        buffer.extend(chunk)
        while True:
            marker = buffer.find(b"\x00\x00\x01", read_ptr)
            if marker < 0:
                if len(buffer) > 2:
                    del buffer[:-2]
                    read_ptr = 0
                break
            nal_start = marker + 3
            if nal_start + 2 > len(buffer):
                read_ptr = marker
                del buffer[:marker]
                read_ptr = 0
                break
            next_marker = buffer.find(b"\x00\x00\x01", nal_start)
            if next_marker < 0:
                if len(buffer) - nal_start > _MAX_PROBE_NAL_BYTES:
                    del buffer[:nal_start]
                    read_ptr = 0
                    break
                read_ptr = marker
                if marker > 0:
                    del buffer[:marker]
                    read_ptr = 0
                break
            nal_type = (buffer[nal_start] >> 1) & 0x3F
            if nal_type in (62, 63):
                rpu += 1
            read_ptr = next_marker
            del buffer[:read_ptr]
            read_ptr = 0
    # 流结束：缓冲区若含完整头部的 RPU NAL，处理它。
    if len(buffer) > 5:
        marker = buffer.find(b"\x00\x00\x01", 0)
        if marker >= 0 and marker + 5 <= len(buffer):
            nal_type = (buffer[marker + 3] >> 1) & 0x3F
            if nal_type in (62, 63):
                rpu += 1
    return rpu


_DV_RPU_WINDOWS: tuple[float, ...] = (0.0, 0.4, 0.8)
_DV_RPU_FRAMES = 240
_DV_RPU_TIMEOUT_SECONDS = 300.0


def _raw_rpu_evidence(dovi: str, merged_path: Path, out: Path, *,
                      limit: int = _DV_RPU_FRAMES,
                      timeout_seconds: float = _DV_RPU_TIMEOUT_SECONDS) -> dict:
    """round-14 P1-1: extract in-band Dolby Vision RPU from a RAW merged HEVC
    stream using dovi_tool's native `extract-rpu` (which parses the raw
    bitstream directly, requiring no container timing). This replaces the
    previous approach of piping the raw stream through ffmpeg `-ss`/`-frames:v`
    (which yielded 0 on a timestamp-less raw stream and cannot distinguish
    'no RPU' from 'no usable sample').

    Fail closed on spawn failure, timeout, non-zero exit, empty RPU output, or
    an unparseable RPU file; the partial RPU output is always removed."""
    import threading
    import signal as _signal

    out.unlink(missing_ok=True)
    command = [
        dovi, "extract-rpu",
        "-i", str(merged_path),
        "-o", str(out),
        "-l", str(limit),
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except OSError as error:
        raise TransientMatroskaBuildError(f"dovi_tool extract-rpu 启动失败：{error}") from error
    assert process.stdout is not None and process.stderr is not None

    timed_out = [False]
    release = threading.Event()

    def _kill() -> None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), _signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
        try:
            process.kill()
        except OSError:
            pass

    def _watchdog() -> None:
        if not release.wait(timeout_seconds):
            timed_out[0] = True
            _kill()

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()

    def _drain(pipe) -> list[str]:
        tail: list[str] = []
        try:
            for line in pipe:
                tail.append(line.decode("utf-8", "replace"))
                if len(tail) > 8:
                    tail.pop(0)
        except (OSError, ValueError):
            pass
        return tail

    stderr_tail: list[str] = []
    _err_capture = [stderr_tail]

    def _capture_stderr() -> None:
        _err_capture[0] = _drain(process.stderr)

    drain = threading.Thread(target=_capture_stderr, daemon=True)
    drain.start()

    read_failed = [False]
    try:
        for _ in process.stdout:
            pass
    except OSError:
        read_failed[0] = True
        _kill()
    finally:
        release.set()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        _kill()
        process.wait()
    drain.join(timeout=2.0)

    try:
        if timed_out[0]:
            raise TransientMatroskaBuildError("dovi_tool extract-rpu 超时，失败关闭")
        if read_failed[0]:
            raise TransientMatroskaBuildError("dovi_tool extract-rpu 读取异常，失败关闭")
        if process.returncode != 0:
            detail = "".join(_err_capture[0])
            raise MatroskaBuildError(
                f"dovi_tool extract-rpu 失败（退出码 {process.returncode}）"
                f"{(': ' + detail[-300:]) if detail else ''}"
            )
        if not out.is_file() or out.stat().st_size == 0:
            raise MatroskaBuildError(
                "dovi_tool extract-rpu 输出 RPU 为空（merged 无 RPU），拒绝继续"
            )
        # 确认输出是可解析的有效 RPU（dovi_tool info -s）。
        # round-15 P2：info rc=0 但 stdout 为空不算可解析；spawn/timeout
        # 都转成明确的 MatroskaBuildError，并清理临时 RPU 文件。
        try:
            info = subprocess.run(
                [dovi, "info", "-s", "-i", str(out)],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired as error:
            raise TransientMatroskaBuildError("dovi_tool extract-rpu info 超时，失败关闭") from error
        except OSError as error:
            raise MatroskaBuildError(
                f"dovi_tool extract-rpu info 启动失败：{error}"
            ) from error
        if info.returncode != 0:
            detail = (info.stderr or "").strip()
            raise MatroskaBuildError(
                f"dovi_tool extract-rpu 输出 RPU 不可解析"
                f"{(': ' + detail[-300:]) if detail else ''}"
            )
        summary = info.stdout.strip()
        if not summary:
            raise MatroskaBuildError(
                "dovi_tool extract-rpu info -s 输出为空（RPU 不可解析），拒绝继续"
            )
        return {
            "method": "dovi_tool extract-rpu",
            "limit_frames": int(limit),
            "rpu_file_size": int(out.stat().st_size),
            "rpu_summary": summary[-500:],
        }
    finally:
        out.unlink(missing_ok=True)


def _verify_merged_rpu_evidence(evidence, *, limit: int) -> dict:
    """round-15 P1: validate the structured merged-stage RPU evidence produced
    by _raw_rpu_evidence. Binds the tool-native method to the final release
    gate (no boolean sentinel substitution for DV evidence). Requires:
    - method is the frozen tool-native marker;
    - limit_frames is positive and == the requested limit;
    - rpu_file_size > 0;
    - rpu_summary is a non-empty string.
    Any missing/wrong-typed/empty field fails closed."""
    if not isinstance(evidence, dict):
        raise MatroskaBuildError(
            "Dolby Vision 合并流缺少结构化 RPU 证据（merged_rpu_evidence），拒绝发布"
        )
    method = evidence.get("method")
    limit_frames = evidence.get("limit_frames")
    rpu_file_size = evidence.get("rpu_file_size")
    rpu_summary = evidence.get("rpu_summary")
    if method != "dovi_tool extract-rpu":
        raise MatroskaBuildError(
            f"Dolby Vision 合并流 RPU 证据方法非法（{method!r}），拒绝发布"
        )
    if not isinstance(limit_frames, int) or limit_frames <= 0 or limit_frames != int(limit):
        raise MatroskaBuildError(
            f"Dolby Vision 合并流 RPU limit_frames 非法（{limit_frames!r}），拒绝发布"
        )
    if not isinstance(rpu_file_size, int) or rpu_file_size <= 0:
        raise MatroskaBuildError(
            f"Dolby Vision 合并流 RPU 文件大小非法（{rpu_file_size!r}），拒绝发布"
        )
    if not isinstance(rpu_summary, str) or not rpu_summary.strip():
        raise MatroskaBuildError(
            "Dolby Vision 合并流 RPU summary 为空（不可解析），拒绝发布"
        )
    return {
        "method": method,
        "limit_frames": limit_frames,
        "rpu_file_size": rpu_file_size,
        "rpu_summary": rpu_summary,
    }


def _dv_rpu_evidence(ffmpeg: str, mkv_path: Path,
                     windows: tuple[float, ...] = _DV_RPU_WINDOWS,
                     frames: int = _DV_RPU_FRAMES,
                     timeout_seconds: float = _DV_RPU_TIMEOUT_SECONDS,
                     duration_s: float | None = None) -> dict:
    """Sample N time windows of the output video track and require in-band
    RPU NALs in each. Profile 7 embeds one RPU per access unit, so every valid
    window must contain RPU NALs. Bounded memory per window; fail closed on
    probe errors, non-zero exits or timeouts.

    round-14 P1-2: `windows` are FRACTIONS of the media duration. When
    `duration_s` is provided (verified from a known container/MKV timeline),
    each fraction is converted to an actual seek time via `fraction * duration`
    before being passed to ffmpeg `-ss`. Without this, (0.0, 0.4, 0.8) were
    being treated as 0/0.4/0.8 SECONDS — all inside the first second. The
    returned evidence records the real per-window seek seconds and scanned
    frames."""
    import threading
    from collections import deque

    if not windows:
        raise MatroskaBuildError("DV RPU 证据扫描未配置抽样窗口")
    rpu_per_window: list[int] = []
    seek_seconds: list[float] = []
    scanned_frames: list[int] = []
    for frac in windows:
        if not 0.0 <= frac <= 1.0:
            raise MatroskaBuildError(
                f"DV RPU 抽样窗口 fraction 越界（{frac}），失败关闭"
            )
        if duration_s is not None:
            if duration_s <= 0:
                raise MatroskaBuildError(
                    f"DV RPU 抽样时长非法（{duration_s}），失败关闭"
                )
            seek_s = frac * duration_s
            seek_arg = f"{seek_s:.3f}"
        else:
            seek_s = float(frac)
            # 无时长时禁止把 fraction 当秒（round-14 P1-2）；必须显式给时长。
            raise MatroskaBuildError(
                "DV RPU 时间窗需要已验证的 duration_s（不得把 fraction 当秒），拒绝继续"
            )
        command = [
            ffmpeg, "-v", "error", "-nostats",
            "-ss", seek_arg, "-i", str(mkv_path),
            "-map", "0:v:0", "-c", "copy", "-f", "hevc",
            "-frames:v", str(frames), "-",
        ]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except OSError as error:
            raise TransientMatroskaBuildError(f"DV RPU 证据扫描启动失败：{error}") from error
        assert process.stdout is not None and process.stderr is not None

        timed_out = [False]
        watchdog_release = threading.Event()

        def _kill_group() -> None:
            try:
                if os.name == "posix":
                    import signal as _signal
                    os.killpg(os.getpgid(process.pid), _signal.SIGKILL)
                    return
            except (OSError, ProcessLookupError):
                pass
            try:
                process.kill()
            except OSError:
                pass

        def _watchdog() -> None:
            if not watchdog_release.wait(timeout_seconds):
                timed_out[0] = True
                _kill_group()

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()

        stderr_tail: deque[str] = deque(maxlen=8)

        def _drain_stderr() -> None:
            try:
                for line in process.stderr:
                    stderr_tail.append(line.decode("utf-8", "replace"))
            except (OSError, ValueError):
                pass

        drain = threading.Thread(target=_drain_stderr, daemon=True)
        drain.start()

        count = 0
        failure: Exception | None = None
        try:
            def _chunks():
                while True:
                    piece = process.stdout.read1(1 << 20)
                    if not piece:
                        break
                    yield piece
            count = _count_rpu_in_annexb(_chunks())
        except OSError as error:
            failure = error
            _kill_group()
        finally:
            watchdog_release.set()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            _kill_group()
            failure = failure or MatroskaBuildError("DV RPU 证据扫描超时后未退出")
            process.wait()
        drain.join(timeout=2.0)
        if timed_out[0]:
            raise MatroskaBuildError(
                f"DV RPU 证据扫描超时（窗口 {frac:.1%}），失败关闭"
            )
        if process.returncode != 0:
            detail = "".join(stderr_tail)
            raise MatroskaBuildError(
                f"DV RPU 证据扫描失败（退出码 {process.returncode}）"
                f"{(': ' + detail[-300:]) if detail else ''}"
            )
        if failure is not None:
            raise TransientMatroskaBuildError(f"DV RPU 证据扫描读取异常：{failure}")
        rpu_per_window.append(count)
        seek_seconds.append(seek_s)
        scanned_frames.append(frames)
    return {
        "rpu_per_window": tuple(rpu_per_window),
        "seek_seconds": tuple(seek_seconds),
        "scanned_frames": tuple(scanned_frames),
        "windows_fraction": windows,
    }


def _validate_ffmpeg_output(asset: BlurayTitleAsset, streams: list[dict],
                            output: Path, probe: str, *, dolby_vision: bool,
                            source_path: Path,
                            ffmpeg_executable: str = "ffmpeg",
                            dv_facts: dict | None = None) -> dict:
    """BATCH-002 §6 统一输出校验（FFmpeg-file 非 DV / DV 共用）。

    校验：单一主视频、容器时长 + 视频轨时间轴（Codex P1-A）、音轨/PGS
    数量顺序语言、codec、默认标志、分辨率、DV Profile 7 独立证据（DV 时）
    /无 DV（非 DV 时）、结构化 Cues + 10/50/90% 时间窗口 seek（P1-B）。
    任一不符失败关闭，不留 ready。失败前把小型诊断写入 output.parent 的
    build-diagnostics.json（Codex P1-D），供事后定位。
    """
    diagnostics: dict = {
        "output": str(output),
        "dolby_vision": dolby_vision,
        "dv_facts": dv_facts,
        "format_duration_s": None,
        "video_timeline": {},
        "seek": None,
        "dv_evidence": None,
    }
    _record_tool_versions(
        diagnostics, probe, ffmpeg_executable,
        dovi=(dv_facts or {}).get("dovi_executable"),
    )
    try:
        return _validate_ffmpeg_output_inner(
            asset, streams, output, probe, dolby_vision=dolby_vision,
            source_path=source_path, ffmpeg_executable=ffmpeg_executable,
            dv_facts=dv_facts, diagnostics=diagnostics,
        )
    except MatroskaBuildError as error:
        _write_build_diagnostics(output, diagnostics, error)
        raise


def _record_tool_versions(diagnostics: dict, probe: str,
                          ffmpeg: str, dovi: str | None = None) -> None:
    """Record sanitized ffmpeg/ffprobe/dovi_tool version. ffmpeg/ffprobe use
    `-version`; dovi_tool uses `--version`. Non-zero exit / empty output are
    recorded honestly as a failure marker, not a fabricated version line."""
    for tool, key, flag in ((ffmpeg, "ffmpeg_version", "-version"),
                            (probe, "ffprobe_version", "-version"),
                            (dovi, "dovi_tool_version", "--version")):
        if not tool or tool == "None":
            continue
        try:
            completed = subprocess.run(
                [tool, flag], capture_output=True, text=True, timeout=30,
            )
            if completed.returncode != 0:
                diagnostics[key] = f"<version query failed rc={completed.returncode}>"
                continue
            first = (completed.stdout or "").splitlines()[:1]
            diagnostics[key] = first[0] if first else "<empty output>"
        except (OSError, subprocess.SubprocessError) as error:
            diagnostics[key] = f"<version query failed: {str(error)[-80:]}"


def _write_build_diagnostics(output: Path, diagnostics: dict,
                             error: Exception) -> None:
    """Codex P1-D：失败前把小型完整证据写入 generation 目录，与 build log
    同生命周期保留（不默认保留 50GB 成品）。"""
    try:
        import datetime as _dt
        diagnostics["failed_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        diagnostics["error"] = f"{type(error).__name__}: {error}"
        dv_facts = diagnostics.get("dv_facts") or {}
        if isinstance(dv_facts, dict):
            if dv_facts.get("el_fingerprints"):
                dv_facts["el_fingerprints"] = _fingerprint_hashes(
                    dv_facts["el_fingerprints"]
                )
            # round-11 P2：持久化紧凑 EL 统计，不写完整 VCL 摘要数组。
            if isinstance(dv_facts.get("el_source_digests"), list):
                stats = dv_facts.get("el_source_digest_stats") or _el_vcl_stats(
                    dv_facts["el_source_digests"]
                )
                dv_facts["el_source_digest_stats"] = stats
                dv_facts["el_source_digests"] = "<compact stats, not persisted>"
        target = output.parent / "build-diagnostics.json"
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(diagnostics, fh, ensure_ascii=False, indent=1, default=str)
    except OSError:
        pass


def _validate_ffmpeg_output_inner(
    asset: BlurayTitleAsset, streams: list[dict], output: Path, probe: str, *,
    dolby_vision: bool, source_path: Path, ffmpeg_executable: str = "ffmpeg",
    dv_facts: dict | None = None, diagnostics: dict | None = None,
) -> dict:
    """_validate_ffmpeg_output 的实际主体（诊断收集由外层包装注入）。"""
    out_streams = _probe_output_streams(probe, output)
    videos = [s for s in out_streams if s.get("codec_type") == "video"]
    audio = [s for s in out_streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in out_streams if s.get("codec_type") == "subtitle"]

    # §6-2 有且只有预期主视频。
    if len(videos) != 1:
        raise MatroskaBuildError(f"ffmpeg 产物视频轨数量无效：{len(videos)}")

    # §6-3 容器时长与 MPLS 容差（±2 秒）+ 视频轨时间轴（Codex P1-A）。
    duration_ns = _probe_duration_ns(probe, output)
    expected_ns = int(asset.duration_90k / 90000 * 1_000_000_000)
    if diagnostics is not None:
        diagnostics["format_duration_s"] = duration_ns / 1e9
    if duration_ns <= 0 or abs(duration_ns - expected_ns) > 2_000_000_000:
        raise MatroskaBuildError(
            f"ffmpeg 产物时长 {duration_ns / 1e9:.1f}s 与 MPLS "
            f"{expected_ns / 1e9:.1f}s 偏差超限，拒绝发布"
        )
    resolved = _resolve_official_streams(asset, streams)
    source_video = resolved.get("video")
    video_fps = (dv_facts or {}).get("fps")
    if video_fps is None and source_video is not None:
        video_fps = _require_video_rate(source_video, "官方源视频")
    timeline = _verify_video_timeline(
        probe, output, asset, video_fps,
        label="ffmpeg 产物", ffmpeg=ffmpeg_executable,
    )
    if diagnostics is not None:
        diagnostics["video_timeline"] = timeline

    # §6-4/5 音轨与 PGS 数量、顺序、语言。
    if len(audio) != len(asset.audio_tracks):
        raise MatroskaBuildError(
            f"ffmpeg 产物音轨数量 {len(audio)} 与官方 {len(asset.audio_tracks)} 不一致"
        )
    if len(subtitles) != len(asset.subtitle_tracks):
        raise MatroskaBuildError(
            f"ffmpeg 产物字幕数量 {len(subtitles)} 与官方 {len(asset.subtitle_tracks)} 不一致"
        )
    for index, ((official, source), out) in enumerate(zip(resolved["audio"], audio)):
        language = str((out.get("tags") or {}).get("language") or "und").lower()
        expected = (official.language or "und").lower()
        if _canonical_language(language) != _canonical_language(expected):
            raise MatroskaBuildError(
                f"ffmpeg 产物音轨 {index} 语言 {language} 与官方 {expected} 不一致"
            )
        out_codec = str(out.get("codec_name") or "").lower()
        src_codec = str(source.get("codec_name") or "").lower()
        # Blu-ray LPCM（pcm_bluray）写入 MKV 时按位深标准映射为
        # pcm_s16le / pcm_s24le（英雄本色 UHD 含 24 位 LPCM 轨），
        # 这是合法的容器转码，不是音轨内容不一致。
        pcm_transcode = (
            src_codec == "pcm_bluray"
            and out_codec in {"pcm_s16le", "pcm_s24le"}
        )
        if not pcm_transcode and out_codec != src_codec:
            raise MatroskaBuildError(
                f"ffmpeg 产物音轨 {index} codec {out_codec} 与源 {src_codec} 不一致"
            )
    for index, (out, official) in enumerate(zip(subtitles, asset.subtitle_tracks)):
        language = str((out.get("tags") or {}).get("language") or "und").lower()
        expected = (official.language or "und").lower()
        if _canonical_language(language) != _canonical_language(expected):
            raise MatroskaBuildError(
                f"ffmpeg 产物字幕 {index} 语言 {language} 与官方 {expected} 不一致"
            )

    # §6-6 默认标志与官方元数据一致。
    default_audio = asset.default_audio_index
    for index, track in enumerate(asset.audio_tracks):
        wants = default_audio is not None and track.index == default_audio
        got = bool((audio[index].get("disposition") or {}).get("default"))
        if wants != got:
            raise MatroskaBuildError(
                f"ffmpeg 产物音轨 {index} 默认标志 {got} 与官方期望 {wants} 不一致"
            )
    default_sub = asset.default_subtitle_index
    for index, track in enumerate(asset.subtitle_tracks):
        wants = default_sub is not None and track.index == default_sub
        got = bool((subtitles[index].get("disposition") or {}).get("default"))
        if wants != got:
            raise MatroskaBuildError(
                f"ffmpeg 产物字幕 {index} 默认标志 {got} 与官方期望 {wants} 不一致"
            )

    # §6-7 分辨率、视频 codec、位深（pix_fmt）、色彩参数与 HDR side data
    # 无意外变化（round-1 P1-2）。
    source_video = resolved["video"]
    if source_video is not None:
        if (int(videos[0].get("width") or 0) != int(source_video.get("width") or 0)
                or int(videos[0].get("height") or 0) != int(source_video.get("height") or 0)):
            raise MatroskaBuildError(
                f"ffmpeg 产物分辨率 {videos[0].get('width')}x{videos[0].get('height')} "
                f"与源 {source_video.get('width')}x{source_video.get('height')} 不一致"
            )
        for field, label in (("codec_name", "视频 codec"),
                             ("pix_fmt", "位深/像素格式"),
                             ("color_space", "color space"),
                             ("color_transfer", "color transfer"),
                             ("color_primaries", "color primaries")):
            src_value = str(source_video.get(field) or "").lower()
            out_value = str(videos[0].get(field) or "").lower()
            # round-2 P1-1：源已知而输出缺失/不同都失败关闭，
            # 不允许只在双方非空时比较的静默通过。
            if src_value and src_value != out_value:
                raise MatroskaBuildError(
                    f"ffmpeg 产物{label} {out_value or '缺失'} 与源 {src_value} 不一致，拒绝发布"
                )
        # 静态 HDR side data 按类型配对比较字段值（round-2 P1-1）。
        # round-4 P1-2：只比较明确的静态 HDR 类型（mastering display /
        # content light level）；DOVI configuration record 等非静态 HDR
        # 条目交 DV 专用校验，不得被通用比较以"意外新增"误拒。
        _STATIC_HDR_SIDE_TYPES = {
            "Mastering display metadata",
            "Content light level metadata",
        }
        src_side = {
            str(s.get("side_data_type")): s
            for s in (source_video.get("side_data_list") or [])
            if str(s.get("side_data_type")) in _STATIC_HDR_SIDE_TYPES
        }
        out_side_static = {
            str(s.get("side_data_type")): s
            for s in (videos[0].get("side_data_list") or [])
            if str(s.get("side_data_type")) in _STATIC_HDR_SIDE_TYPES
        }
        for side_type, source_entry in src_side.items():
            output_entry = out_side_static.get(side_type)
            if output_entry is None:
                raise MatroskaBuildError(
                    f"ffmpeg 产物丢失源静态 HDR 元数据 {side_type}，拒绝发布"
                )
            for key, value in source_entry.items():
                if key in ("side_data_type",):
                    continue
                if output_entry.get(key) != value:
                    raise MatroskaBuildError(
                        f"ffmpeg 产物 HDR 元数据 {side_type}.{key} "
                        f"{output_entry.get(key)} 与源 {value} 不一致，拒绝发布"
                    )
        for side_type in out_side_static:
            if side_type not in src_side:
                raise MatroskaBuildError(
                    f"ffmpeg 产物出现源不存在的静态 HDR 元数据 {side_type}，拒绝发布"
                )
        # 动态 HDR10+：源/输出对称 ES 语义探测（round-4 P1-1——T.35
        # type 4 + 前缀，仅 prefix/suffix SEI，前 60 秒抽样；探测失败
        # 即抛错失败关闭）。抽样一致是必要条件，非全片证明。
        src_dynamic = _has_hdr10plus_es(ffmpeg_executable, source_path)
        out_dynamic = _has_hdr10plus_es(ffmpeg_executable, output)
        if src_dynamic != out_dynamic:
            raise MatroskaBuildError(
                f"HDR10+（T.35 SEI 抽样）源={src_dynamic} 输出={out_dynamic} "
                f"不一致，拒绝发布"
            )

    # §6-5 字幕逐官方轨验证输出 codec 仍为 PGS（round-1 P1-2）。
    for index, (_official, source) in enumerate(resolved["subtitle"]):
        out_codec = str(subtitles[index].get("codec_name") or "").lower()
        src_codec = str(source.get("codec_name") or "").lower()
        if src_codec != out_codec:
            raise MatroskaBuildError(
                f"ffmpeg 产物字幕 {index} codec {out_codec} 与源 {src_codec} 不一致，拒绝发布"
            )
        if out_codec != "hdmv_pgs_subtitle":
            raise MatroskaBuildError(
                f"ffmpeg 产物字幕 {index} codec {out_codec} 不是 PGS，拒绝发布"
            )

    # §6-8 DV 事实（Codex P1-C：独立证据链，不依赖自写 SPS 标志）。
    try:
        if dolby_vision:
            dv_evidence = _verify_dv_profile7_evidence(
                ffmpeg_executable, output, dv_facts,
            )
            if diagnostics is not None:
                diagnostics["dv_evidence"] = dv_evidence
        else:
            for side in (videos[0].get("side_data_list") or []):
                if side.get("side_data_type") == "DOVI configuration record":
                    raise MatroskaBuildError(
                        "非 DV 实验产物出现 DOVI 配置记录，媒体类别与实验值不匹配"
                    )
    except MatroskaBuildError:
        raise
    except Exception as error:
        raise MatroskaBuildError(f"DV 事实校验异常：{error}") from error

    # §6-9 结构化 Cues + 10/50/90% 时间窗口 seek（Codex P1-B / round-17 B；
    # 目标时间由 MPLS 期望时长确定，先与输出视频时间轴交叉核对——已在上方完成）。
    # round-17：Profile 7 路线由 dv_facts 提供 dovi 工具，按正交 BL 解码门校验。
    seek_result = _verify_cues_and_seek(
        probe, output, expected_ns, diagnostics=diagnostics,
        dv_facts=dv_facts if dolby_vision else None,
        ffmpeg=ffmpeg_executable,
    )
    if diagnostics is not None:
        diagnostics["seek"] = seek_result

    return {
        "duration_ns": duration_ns,
        "dolby_vision": dolby_vision,
        "audio_tracks": tuple(
            {"index": i, "language": (t.language or "und").lower()}
            for i, t in enumerate(asset.audio_tracks)
        ),
        "subtitle_tracks": tuple(
            {"index": i, "language": (t.language or "und").lower()}
            for i, t in enumerate(asset.subtitle_tracks)
        ),
    }



class FfmpegFileMatroskaBuilder(MatroskaBuilder):
    """非 DV FFmpeg-file 实验候选（BATCH-002 §5.3）。

    显式实验入口 HDATHOME_FINALIZE_MUXER=ffmpeg。物化确切的 MPLS 主片，
    从该物化源探测 PMT，官方 PID 精确映射 stream copy 生成 MKV，
    并通过统一输出校验（语言/默认/时长/codec/分辨率/Cues/中点 seek）
    后才返回 metadata。遇到 DV 媒体时失败关闭（类别与实验值不匹配）。
    """

    def __init__(self, *args, **kwargs):
        # FFmpeg-file 实验用 ffmpeg 封装（不是继承默认的 mkvmerge）。
        kwargs.setdefault("executable", "ffmpeg")
        super().__init__(*args, **kwargs)

    def peak_space_bytes(self, asset: BlurayTitleAsset) -> int:
        """物化主片 + ffmpeg 正在写的部分 MKV 同时存活。"""
        return 2 * int(asset.size) + 512 * 1024**2

    def build(
        self,
        asset: BlurayTitleAsset,
        output: Path,
        *,
        title: str,
        progress: Callable[[int, str], None] | None = None,
    ) -> dict:
        if shutil.which(self.executable) is None:
            raise MatroskaBuildError("服务器未安装 ffmpeg")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        source = output.with_name(output.name + ".source.m2ts")
        try:
            self._materialize_or_reuse_title(asset, source, progress=progress)
            if progress:
                progress(45, "正在从物化主片探测 PMT 流布局")
            # §5.2 流布局判断只来自这份确切的物化主片。
            streams = self._video_streams(source)
            dolby_vision = asset.dolby_vision or dual_hevc_video_streams(streams)
            if progress:
                progress(46, f"DV 探测事实：dual_hevc={dual_hevc_video_streams(streams)} "
                             f"mpls_dv={asset.dolby_vision}")
            if dolby_vision:
                raise MatroskaBuildError(
                    "非 DV FFmpeg 实验值遇到 Dolby Vision 媒体，媒体类别与实验值"
                    "不匹配，失败关闭（请使用 DV 保真实验值）"
                )
            maps = _exact_pid_maps(asset, streams)
            cmd = [
                self.executable, "-y",
                "-fflags", "+genpts",
                "-probesize", "33554432",
                "-analyzeduration", "30000000",
                "-i", str(source),
                *maps,
                "-c", "copy",
                "-f", "matroska",
                "-metadata", f"title={title}",
            ]
            cmd = _inject_official_metadata(cmd, asset, streams)
            cmd.append(str(output))
            if progress:
                progress(50, "ffmpeg 封装中（stream copy）")
            ffmpeg_tail: list[str] = []
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                ffmpeg_tail.append(line.rstrip())
                if len(ffmpeg_tail) > 200:
                    ffmpeg_tail.pop(0)
                if progress and "time=" in line:
                    progress(90, line.strip()[-200:])
            code = process.wait()
            if code != 0:
                detail = "\n".join(ffmpeg_tail[-200:])
                raise MatroskaBuildError(
                    f"ffmpeg 封装失败（退出码 {code}）\nffmpeg 命令：{' '.join(cmd)}\n{detail}"
                )
            if progress:
                progress(96, "统一输出校验（语言/时长/codec/分辨率/Cues/seek）")
            return _validate_ffmpeg_output(
                asset, streams, output, self.probe, dolby_vision=False,
                source_path=source, ffmpeg_executable=self.executable,
            )
        finally:
            # Keep a complete source M2TS as the restart checkpoint.  Any
            # interrupted downstream output is replaced on the next attempt.
            if not self._source_checkpoint_is_valid(asset, source):
                source.unlink(missing_ok=True)


class FfmpegDvFileMatroskaBuilder(MatroskaBuilder):
    """DV 保真 FFmpeg-file 实验候选（BATCH-002 §5.4）。

    显式实验入口 HDATHOME_FINALIZE_MUXER=ffmpeg-dv。流程：物化确切主片
    → PMT 确认 BL/EL（PMT-only DV 不得误入非 DV）→ dovi_tool 合并保留
    Profile 7（不 --discard、不 -m 5、不丢 EL/RPU）→ 合并裸 HEVC 经
    MP4 中转补时间戳（stream copy，NAL/RPU 无损）→ FFmpeg 双输入封装
    （输入 0 = 时间戳化合并 DV，输入 1 = 同一物化源的官方音轨/PGS）→
    统一校验 + DOVI Profile 7 强校验。任一环节缺证据即失败关闭。
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("executable", "ffmpeg")
        # BATCH-002 §5.4：DV 保真实验固定 Profile 7；环境变量不得将其
        # 切到 8.1（本批次禁止单层化、禁止丢 EL）。
        kwargs["dv_profile"] = "7"
        super().__init__(*args, **kwargs)

    def peak_space_bytes(self, asset: BlurayTitleAsset) -> int:
        """物化主片 + BL + EL + 合并视频 + 时间戳中转 + 部分 MKV 同时存活（保守 5×）。

        5× 覆盖 dovi_tool 合并完成后 mp4 中转期间「合并视频 + 中转文件」
        短暂共存，以及最终 mux 时「源主片 + 中转视频 + 部分 MKV」并存。
        """
        return 5 * int(asset.size) + 512 * 1024**2

    def build(
        self,
        asset: BlurayTitleAsset,
        output: Path,
        *,
        title: str,
        progress: Callable[[int, str], None] | None = None,
    ) -> dict:
        if shutil.which(self.executable) is None:
            raise MatroskaBuildError("服务器未安装 ffmpeg")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        source = output.with_name(output.name + ".source.m2ts")
        temporaries: list[Path] = []
        try:
            self._materialize_or_reuse_title(asset, source, progress=progress)
            if progress:
                progress(45, "正在从物化主片探测 PMT 流布局")
            streams = self._video_streams(source)
            dolby_vision = asset.dolby_vision or dual_hevc_video_streams(streams)
            if progress:
                progress(46, f"DV 探测事实：dual_hevc={dual_hevc_video_streams(streams)} "
                             f"mpls_dv={asset.dolby_vision}")
            if not dolby_vision:
                raise MatroskaBuildError(
                    "DV 保真实验值遇到非 Dolby Vision 媒体，媒体类别与实验值"
                    "不匹配，失败关闭（请使用非 DV 实验值）"
                )
            dovi_executable = self._require_dovi_tool()
            base_pid, enhancement_pid = self._dv_video_pids(asset, streams)
            # Codex P1-A / round-4 P1-4：官方 BL 必须按已解析的确切 PID 取得
            # （复用 _resolve_official_stream 语义），找不到即失败关闭；
            # 禁止静默回退到"第一条视频流"（可能误取 EL/辅助视频帧率）。
            base_video = _resolve_official_stream(
                streams, base_pid, "video",
                next((t.coding_type for t in asset.video_tracks
                      if t.pid == base_pid), 0xEA),
            )
            fps_facts = _video_rate_facts(base_video, "官方 BL 视频")
            fps = fps_facts["rate"]
            base_path = source.with_name(source.name + ".bl.hevc")
            enhancement_path = source.with_name(source.name + ".el.hevc")
            merged_video = source.with_name(source.name + ".dv.hevc")
            temporaries += [base_path, enhancement_path, merged_video]
            if progress:
                progress(50, "正在提取 Dolby Vision BL/EL（保留 Profile 7）")
            self._extract_dv_layer(source, base_pid, base_path)
            self._extract_dv_layer(source, enhancement_pid, enhancement_path)
            # Codex round-5 P1-1：光盘真实 EL 多窗口切片指纹 + BL 负向排除。
            el_fp_by_window = _exclude_bl_hits(
                _extract_el_window_fingerprints(enhancement_path), base_path,
            )
            # Codex round-6 P1-2：0/10/50/90 窗口必须齐全且每窗≥最小指纹数。
            _require_el_window_integrity(el_fp_by_window, "EL 提取")
            self._merge_dv_layers(
                dovi_executable, base_path, enhancement_path, merged_video, progress,
            )
            # DV 点亮修复（2026-08-18）：删除 SPS DV VUI 补丁（自写解析器假阳性，
            # ffmpeg 不认，真机显示 HDR10）。改由 mkvmerge 封装——mkvmerge 从
            # 码流自动扫描 RPU/EL 生成正确的容器级 dvcC（已实测）。
            # DV 阶段证据（Codex P1-C）：合并文件必须显著大于 BL（EL 数据
            # 确实被融合；--discard 单层化时合并≈BL），并记录合并流 RPU
            # 采样，供校验与最终产物做阶段一致性比较。
            bl_size = base_path.stat().st_size
            merged_size = merged_video.stat().st_size
            # round-14 P1-1：merged 裸 HEVC 无容器时序，改用 dovi_tool 原生
            # extract-rpu（直接解析裸流，不依赖 ffmpeg 时序采样）。
            merged_rpu_path = source.with_name(source.name + ".rpu.bin")
            temporaries.append(merged_rpu_path)
            merged_rpu_evidence = _raw_rpu_evidence(
                dovi_executable, merged_video, merged_rpu_path, limit=240,
            )
            # round-15 P1：不再用 `merged_rpu_first_window=1` 哨兵占位；结构化
            # merged_rpu_evidence 由最终 validator 强制校验（见 _verify_dv_profile7_evidence）。
            # round-9 P1：用 dovi_tool demux --el-only 回拆 merged 并与源 EL
            # 做有序 VCL 切片序列一致性（统一 frame/AU 坐标），证明合并流中
            # EL 存在、顺序正确、覆盖全片。替换原"字节比例 max_frac>=0.4 门"
            # （该门混淆了 EL 文件字节比例与合并流字节比例，误杀完整 DV）。
            # 源 EL 摘要预计算一次，供 merged/MP4/MKV 三阶段复用，避免反复
            # 全量解析 1.39GB 源流。同时记录紧凑覆盖统计（round-11 P2）。
            src_el_digests, src_el_stats = _el_vcl_parse(enhancement_path)
            el_merged_evidence = _verify_el_order_consistency(
                dovi_executable, self.ffmpeg, enhancement_path,
                merged_video, "合并流", container=False,
                src_digests=src_el_digests, src_stats=src_el_stats,
            )
            dv_facts = {
                "bl_size": int(bl_size),
                "merged_size": int(merged_size),
                "merged_rpu_evidence": dict(merged_rpu_evidence),
                "fps": fps,
                "duration_s": asset.duration_90k / 90000,
                "dovi_command": "dovi_tool mux --bl --el（Profile 7，无 --discard）",
                "el_fingerprints": el_fp_by_window,
                "el_fp_windows": sorted(el_fp_by_window.keys()),
                "el_merged_order": el_merged_evidence,
                "el_source_digests": src_el_digests,
                "el_source_digest_stats": src_el_stats,
                "dovi_executable": dovi_executable,
            }
            # BL/EL 合并后不再需要，立即释放（避免峰值叠加）。
            base_path.unlink(missing_ok=True)
            enhancement_path.unlink(missing_ok=True)
            # DV 点亮修复（2026-08-18）：不再走 ffmpeg mp4 中转 + ffmpeg 封装
            # （ffmpeg stream-copy 不认 SPS patch，产物无 dvcC）。改用
            # mkvmerge 直接封装裸 merged HEVC（--default-duration 0:<fps>p
            # 提供时间戳），mkvmerge 自动扫描码流 RPU/EL 生成 dvcC。
            if progress:
                progress(80, "mkvmerge DV 保真封装中（裸 HEVC + 音轨/PGS）")
            mkvmerge_executable = shutil.which("mkvmerge")
            if mkvmerge_executable is None:
                raise MatroskaBuildError(
                    "服务器未安装 mkvmerge；DV 保真封装需要 mkvmerge，拒绝降级输出"
                )
            identified = parse_identification(
                self._identify_path(source, executable=mkvmerge_executable)
            )
            resolved = resolve_official_tracks(asset, identified)
            command = build_mkvmerge_command_dv(
                asset, resolved, merged_video, source, output, title=title,
            )
            # 显式帧率（裸 HEVC 无容器时序）——mkvmerge 需要 default-duration。
            command[0] = mkvmerge_executable
            command[3:3] = ["--default-duration",
                            f"0:{fps_facts['rational']}p"]
            ffmpeg_tail: list[str] = []
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                ffmpeg_tail.append(line.rstrip())
                if len(ffmpeg_tail) > 200:
                    ffmpeg_tail.pop(0)
                if progress and "%" in line:
                    progress(92, line.strip()[-200:])
            code = process.wait()
            if code != 0:
                detail = "\n".join(ffmpeg_tail[-200:])
                raise MatroskaBuildError(
                    f"mkvmerge DV 保真封装失败（退出码 {code}）\n"
                    f"mkvmerge 命令：{' '.join(command)}\n{detail}"
                )
            if progress:
                progress(96, "统一输出校验 + DOVI Profile 7 强校验")
            return _validate_ffmpeg_output(
                asset, streams, output, self.probe, dolby_vision=True,
                source_path=source, ffmpeg_executable=self.executable,
                dv_facts=dv_facts,
            )
        finally:
            if not self._source_checkpoint_is_valid(asset, source):
                source.unlink(missing_ok=True)
            for path in temporaries:
                path.unlink(missing_ok=True)
