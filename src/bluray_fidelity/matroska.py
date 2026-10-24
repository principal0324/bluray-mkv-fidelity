"""Exact-track, stream-copy Matroska finalization for MPLS-selected titles."""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .bluray import (
    BlurayTitleAsset,
    BlurayTitleError,
    TransientBlurayReadError,
    bluray_item_bounds,
    iter_bluray_item,
    iter_bluray_title,
)


class MatroskaBuildError(RuntimeError):
    """Raised when a lossless finalized representation cannot be guaranteed."""


class TransientMatroskaBuildError(MatroskaBuildError):
    """A build failure that is transient and worth a bounded automatic retry.

    Only process-spawn failures, explicit timeouts, transient I/O/OS read
    errors, and explicitly listed tool faults are marked transient. Everything
    else (codec/PID/language mismatches, DV/BL/EL/RPU/Cues/seek gate failures,
    deterministic source errors, unclassified non-zero tool exits) stays plain
    MatroskaBuildError so it fails closed without wasting a full rebuild."""


logger = logging.getLogger("hdathome.matroska")

# 操作者可读失败详情的统一边界：原因在头部，超长时保留头部并以省略号标记
# （执行账本 detail 列上限 500 字符；工具转储的尾部不代表原因）。
_DETAIL_LIMIT = 300
# 工具输出的有界尾部行数（仅用于退出码失败时的瞬时/永久分类）。
_TOOL_TAIL_LINES = 40


def _process_error_detail(error: BaseException) -> str:
    """子进程/超时异常的 stderr/stdout 文本（已解码为 str）。

    ``subprocess.run(..., text=True)`` 抛出的 ``TimeoutExpired`` 仍把
    stdout/stderr 暴露为 bytes，直接 str() 只会得到 b'...' repr（2026-09-14
    P1：探测超时的失败详情整段是 b'...' 编码后的 CSV 转储）。
    """
    for attribute in ("stderr", "stdout", "output"):
        raw = getattr(error, attribute, None)
        if not raw:
            continue
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", errors="replace").strip()
        return str(raw).strip()
    return str(error).strip()


def _bounded_detail(value, limit: int = _DETAIL_LIMIT) -> str:
    """保留头部原因的有界文本（截断处标记省略号，便于确认被裁剪）。"""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _start_progress_ticker(progress, percent: int, label: str,
                           interval: float = 30.0):
    """校验阶段的进度/心跳 ticker：每 interval 秒回调一次 progress。

    2026-09-15 越南战争：封装后的校验（成品识别 + 时长门 + 拼接门 + seek 门）
    在 28 GB 成品上可持续数分钟，而这一阶段没有 progress 回调 → execution_work
    的 last_heartbeat_at 变旧，于是**播放器的周期重交付**（或失联判定）会把正在
    校验的构建重置为 accepted 并从头重建（实测 03:31:24 校验开始、03:32:57 又
    从第 1 段重建）。ticker 让心跳在整个校验期保持新鲜，构建得以跑完。
    返回 threading.Event（set() 即停止）。
    """
    stop = threading.Event()
    if progress is None:
        return stop

    def loop():
        while not stop.wait(interval):
            try:
                progress(percent, label)
            except Exception:
                logger.warning("校验阶段进度回调失败（不影响校验）", exc_info=False)

    threading.Thread(target=loop, daemon=True,
                     name="hdathome-gate-heartbeat").start()
    return stop


def _timeout_seconds(error: BaseException) -> float | None:
    """TimeoutExpired 的超时秒数；非超时返回 None。"""
    if not isinstance(error, subprocess.TimeoutExpired):
        return None
    try:
        return float(getattr(error, "timeout", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _tool_failure(label: str, error: BaseException,
                  *, limit: int = _DETAIL_LIMIT) -> MatroskaBuildError:
    """工具/探测失败的统一异常构造（所有探测点复用，避免分类与截断漂移）。

    显式超时按瞬时（Transient）分类：探测超时不代表媒体缺陷，整流程重跑
    一次即可恢复（2026-09-14 P1：300s 探测超时曾被当永久媒体缺陷收口）。
    非超时详情保留头部原因；尾部工具转储不再覆盖前缀原因。
    """
    seconds = _timeout_seconds(error)
    if seconds is not None:
        return TransientMatroskaBuildError(
            f"{label}超时（>{seconds:.0f}s），按瞬时故障重试"
        )
    detail = _bounded_detail(_process_error_detail(error), limit)
    return MatroskaBuildError(f"{label}失败：{detail}" if detail else f"{label}失败")


_OUT_OF_SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})
# mkvmerge 写盘失败原文（本机 v92 实测为 "No space left to write to"、
# "Not enough space"）；ffmpeg 为 "No space left on device"。
_OUT_OF_SPACE_MARKERS = (
    "no space left to write",
    "no space left on device",
    "not enough space",
    "disk full",
    "disk quota exceeded",
)


def _is_out_of_space(error: BaseException | str) -> bool:
    """临时空间耗尽的统一判定（errno 或工具明文）。

    空间耗尽不是媒体缺陷：当作永久失败会烧掉重试预算并把可恢复的磁盘压力
    误报为坏源（2026-09-14 生产确认）。命中后按 TransientMatroskaBuildError
    收口，由上层有界重试。
    """
    if isinstance(error, OSError) and error.errno in _OUT_OF_SPACE_ERRNOS:
        return True
    text = (error if isinstance(error, str) else _process_error_detail(error)).lower()
    return any(marker in text for marker in _OUT_OF_SPACE_MARKERS)


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


_CUES_PARSE_CHUNK_BYTES = 64 * 1024


class CuesParseError(ValueError):
    """Structured Cues could not be safely classified or counted."""

    def __init__(self, status: str, detail: str):
        self.status = status
        super().__init__(f"{status}: {detail}")


def _read_cues_bytes(stream, size: int) -> bytes:
    """Read a small EBML header across bounded chunks; never read a Cues body."""
    pieces: list[bytes] = []
    remaining = size
    while remaining:
        piece = stream.read(min(remaining, _CUES_PARSE_CHUNK_BYTES))
        if not piece:
            raise CuesParseError("truncated", "element header ended at EOF")
        pieces.append(piece)
        remaining -= len(piece)
    return b"".join(pieces)


def _read_cues_element_header(stream, element_limit: int):
    """Read one EBML element header, stitching bytes split across chunks."""
    position = stream.tell()
    if position >= element_limit:
        raise CuesParseError("truncated", "expected an element before its parent ended")
    first_id = _read_cues_bytes(stream, 1)
    id_length = _vint_length(first_id[0])
    if id_length == 0 or id_length > 4:
        raise CuesParseError("parse_failed", "invalid EBML element ID")
    elem_id = first_id + _read_cues_bytes(stream, id_length - 1)
    if stream.tell() > element_limit:
        raise CuesParseError("truncated", "element ID crosses parent boundary")
    size_first = _read_cues_bytes(stream, 1)
    size_length = _vint_length(size_first[0])
    if size_length == 0 or size_length > 8:
        raise CuesParseError("parse_failed", "invalid EBML size VINT")
    size_bytes = size_first + _read_cues_bytes(stream, size_length - 1)
    parsed_size = _vint(size_bytes)
    if parsed_size is None:
        raise CuesParseError("parse_failed", "invalid EBML size VINT")
    if stream.tell() > element_limit:
        raise CuesParseError("truncated", "element size crosses parent boundary")
    return elem_id, id_length, parsed_size[0], size_length


def _read_cues_size(stream) -> tuple[int, int]:
    first = _read_cues_bytes(stream, 1)
    length = _vint_length(first[0])
    if length == 0 or length > 8:
        raise CuesParseError("parse_failed", "invalid EBML size VINT")
    encoded = first + _read_cues_bytes(stream, length - 1)
    parsed = _vint(encoded)
    if parsed is None:
        raise CuesParseError("parse_failed", "invalid EBML size VINT")
    return parsed[0], parsed[1]


def _vint_length(first: int) -> int:
    """Return an EBML VINT's encoded byte length, or zero for an invalid lead."""
    if first == 0:
        return 0
    return 9 - first.bit_length()


def _count_cue_points(path: Path) -> int:
    """Stream Segment elements and count complete, top-level Cues CuePoints.

    Returns zero only for a structurally valid Segment without Cues. Malformed,
    truncated, and unknown-size elements raise ``CuesParseError`` so they cannot
    be mistaken for a valid absence of an index. Cues payloads are never loaded
    into memory; element bodies are skipped by seeking to their declared ends.
    """
    segment_id = b"\x18\x53\x80\x67"
    cues_id = b"\x1c\x53\xbb\x6b"
    cue_point_id = b"\xbb"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            file_size = stream.tell()
            stream.seek(0)
            head = stream.read(256 * 1024)
            index = head.find(segment_id)
            if index < 0:
                raise CuesParseError("parse_failed", "Segment element not found")
            stream.seek(index + len(segment_id))
            segment_size, segment_size_len = _read_cues_size(stream)
            cursor = index + len(segment_id) + segment_size_len
            if segment_size < 0:
                # unknown-size Segment：安全遍历到文件边界。
                segment_end = file_size
            else:
                segment_end = cursor + segment_size
                if segment_end > file_size:
                    raise CuesParseError("truncated", "Segment exceeds file length")
            while cursor < segment_end:
                stream.seek(cursor)
                elem_id, _, size, _ = _read_cues_element_header(
                    stream, segment_end,
                )
                if size < 0:
                    raise CuesParseError("unknown_size", "unknown-size Segment child")
                payload_offset = stream.tell()
                element_end = payload_offset + size
                if element_end > segment_end:
                    raise CuesParseError("truncated", "Segment child exceeds parent boundary")
                if elem_id == cues_id:
                    count = 0
                    cues_end = element_end
                    while stream.tell() < cues_end:
                        child_id, _, child_size, _ = _read_cues_element_header(
                            stream, cues_end,
                        )
                        if child_size < 0:
                            raise CuesParseError(
                                "unknown_size", "unknown-size Cues child",
                            )
                        child_end = stream.tell() + child_size
                        if child_end > cues_end:
                            raise CuesParseError(
                                "truncated", "Cues child exceeds Cues boundary",
                            )
                        if child_id == cue_point_id:
                            count += 1
                        stream.seek(child_end)
                    if count == 0:
                        raise CuesParseError(
                            "parse_failed", "Cues contains no CuePoint elements",
                        )
                    return count
                cursor = element_end
    except OSError as error:
        raise CuesParseError("parse_failed", f"could not read Matroska file: {error}") from error
    return 0


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
    except subprocess.TimeoutExpired as error:
        raise _tool_failure("ffprobe 容器时长探测", error) from error
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
    except subprocess.TimeoutExpired as error:
        raise _tool_failure("ffprobe 帧率探测", error) from error
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
    except subprocess.TimeoutExpired as error:
        raise _tool_failure("ffprobe 视频时间轴探测", error) from error
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
    except subprocess.TimeoutExpired as error:
        raise _tool_failure("ffprobe 视频末端探测", error) from error
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


def _resolve_official_pairs(asset_tracks, tracks: tuple[IdentifiedTrack, ...],
                            kind: str, label: str) -> tuple[tuple[int, int], ...]:
    """官方轨表 → 输入轨的映射，保证**一条输出轨只被一个官方条目占用**。

    MPLS 可能把同一个 PID 声明多次：2026-09-14 越南战争实证（19 个 play item
    里 17 段共用一个 clip），PID 4354 在官方表里同时以 `spa` 与 `eng` 出现。
    mkvmerge 按 PID 只写出**一条**轨，若两个官方条目都映射到它，成品条数就比
    "可映射"少一条而在数量门被拒——来源完好，失败却落在计数上。

    去重规则：同一输入轨只保留一个官方条目；优先保留语言与输入轨一致的声明
    （否则语言门会拿另一条声明去比对而误拒），无一致者保留首个。被丢弃的声明
    作为冲突事实记入日志，不静默。
    """
    by_id = {track.id: track for track in tracks}
    by_index = {entry.index: entry for entry in asset_tracks}
    pairs: list[tuple[int, int]] = []
    owners: dict[int, int] = {}
    conflicts: list[str] = []
    for official in asset_tracks:
        track_id = _try_resolve_one(official.pid, official.coding_type, kind, tracks)
        if track_id is None:
            continue
        owner = owners.get(track_id)
        if owner is not None:
            identified_track = by_id.get(track_id)
            identified_language = _canonical_language(
                (identified_track.language if identified_track else "") or "")
            new_language = _canonical_language(official.language or "")
            old_language = _canonical_language(
                (by_index[owner].language if owner in by_index else "") or "")
            replace = bool(identified_language) and \
                new_language == identified_language and \
                old_language != identified_language
            conflicts.append(
                f"pid=0x{int(official.pid):x} official={official.index}"
                f"/{official.language or 'und'} vs official={owner}"
                f"/{(by_index[owner].language if owner in by_index else '') or 'und'}"
                f"（{'保留后到' if replace else '丢弃'}）"
            )
            if not replace:
                continue
            # 就地替换占用者：输出轨顺序必须保持官方表顺序，否则成品音轨次序
            # 会与官方 index 次序错位（语言门按位置比对）。替换即已登记，不再
            # 追加，否则同一轨会出现两次。
            pairs = [
                (official.index, track_id) if index == owner else (index, value)
                for index, value in pairs
            ]
            owners[track_id] = official.index
            continue
        owners[track_id] = official.index
        pairs.append((official.index, track_id))
    if conflicts:
        logger.warning(
            "%s官方表重复声明同一 PID：%s", label,
            _bounded_detail("; ".join(conflicts), _MISMATCH_LIST_WIDTH),
        )
    return tuple(pairs)


def resolve_official_tracks(asset: BlurayTitleAsset,
                            identified: Iterable[IdentifiedTrack]) -> ResolvedTracks:
    tracks = tuple(identified)
    videos = tuple(track.id for track in tracks if track.type == "video")
    if not videos:
        raise MatroskaBuildError("MPLS 主片没有可用视频轨")
    if not asset.audio_tracks:
        raise MatroskaBuildError("MPLS 主片没有提供官方音轨表，拒绝生成静音影片")
    audio_pairs = _resolve_official_pairs(
        asset.audio_tracks, tracks, "audio", "音轨")
    subtitle_pairs = _resolve_official_pairs(
        asset.subtitle_tracks, tracks, "subtitles", "字幕")
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
                           input_path: Path, output_path: Path, *, title: str,
                           append_inputs: Sequence[Path] = ()) -> list[str]:
    """mkvmerge 命令；``append_inputs`` 非空时按 ``input + a + b`` 追加。

    追加段与首段共用同一组逐文件选项（轨 ID 是各输入文件的局部编号，段间
    轨道对应关系已由 require_segment_track_correspondence 核对：新增/漂移
    已失败关闭，纯子集缺失按记录事实放行）；时间戳由 mkvmerge 按前一段末端
    续接，本管线不做字节拼接。
    """
    command = ["mkvmerge", "--output", str(output_path), "--title", title, "--gui-mode"]
    file_options: list[str] = []
    file_options += ["--video-tracks", ",".join(map(str, resolved.video_ids))]
    file_options += ["--audio-tracks", ",".join(map(str, resolved.audio_ids))]
    if resolved.subtitle_ids:
        file_options += ["--subtitle-tracks", ",".join(map(str, resolved.subtitle_ids))]
    else:
        file_options += ["--no-subtitles"]
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
        file_options += ["--language", f"{track_id}:{official.language or 'und'}"]
        is_default = official.index == default_audio_index
        file_options += ["--default-track-flag", f"{track_id}:{'yes' if is_default else 'no'}"]
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
        file_options += ["--language", f"{track_id}:{official.language or 'und'}"]
        is_default = official.index == default_subtitle_index
        file_options += ["--default-track-flag", f"{track_id}:{'yes' if is_default else 'no'}"]
    # 追加段：**逐轨选择 + 显式追加映射**（2026-09-16 修正）。
    #
    # 旧做法只给类型级开关（`--no-video/--no-audio/--no-subtitles`），把逐轨对应
    # 交给 mkvmerge 的**默认按位映射**。这在首段排除过某条轨时必然崩：mkvmerge
    # 的逐文件选项只作用于**紧随其后的那个文件**，所以 `--audio-tracks 1,2,4`
    # 只让首段排除 track 3，**追加段仍在复制它的全部轨**；默认映射于是给出
    # `1:3:0:3`，指向首段**未被复制**的 track 3 →
    # `#GUI#error … does not contain a track with the ID 3 … --append-to was invalid`
    # （生产标本 dlv_93c13946d7c0eac7c0b9 地球脉动.第三季 Disc.3：mkvmerge 1.3 秒
    # 报错退出，该交付因此永远无法完成）。手工复现验证的修法即：给追加段**同样**
    # 发逐轨选择，并显式给出**只含被复制轨**的 `--append-to`（实测 exit=0）。
    #
    # 安全性：进入封装的追加段**必然与首段同轨布局**——子集/漂移段被
    # `_prepare_items` 记入 violations、由 `_trailing_exclusions` 排除出封装
    # （中段违规直接失败关闭），故这里复用首段的逐轨 ID 不会引用不存在的轨。
    # 若将来有子集段进入封装，本段必须改为按段取轨 ID 交集。
    #
    # 2026-09-17 修正（3 段以上）：mkvmerge 的 `--append-to` 语法是
    # `<源文件>:<源轨>:<目标文件>:<目标轨>`，且**每一段只追加到它的前一段**
    # （file N → file N-1），不是全部追加到首段 file 0。旧实现把目标文件号
    # 硬编码成 0，只有单段追加（2 输入）时才恰好正确；第 3 个输入起给出
    # `2:<轨>:0:<轨>` 与 `1:<轨>:0:<轨>` 冲突，mkvmerge 直接报
    # `More than one track is to be appended to the track N from file no. 0`
    # 并**以退出码 2（错误）中止、不产出任何文件**（本地 mkvmerge v100 实测；
    # 也是 mkvmerge 自己默认映射 `2:0:1:0,2:1:1:1` 的形态）。目标轨号与源轨号
    # 相同：进入封装的段轨道布局已被逐段核对为首段前缀，段内局部编号一致。
    if append_inputs:
        copied_ids = sorted({*resolved.video_ids, *resolved.audio_ids,
                             *resolved.subtitle_ids})
        mapping = [f"{offset}:{track_id}:{offset - 1}:{track_id}"
                   for offset in range(1, len(append_inputs) + 1)
                   for track_id in copied_ids]
    else:
        mapping = []
    command += file_options
    if mapping:
        # 全局选项，必须出现在**输入文件之前**（与手工复现的可用形态一致：
        # mkvmerge 按位置解析选项），且必须一次给全"所有要复制的轨"——
        # 只给一部分会被拒（`Only partial append mappings were given …`）。
        command += ["--append-to", ",".join(mapping)]
    command.append(str(input_path))
    for extra in append_inputs:
        # 追加段必须带**同一组逐轨选择**：逐文件选项只作用于紧随其后的文件，
        # 不重复就会让追加段复制它自己的全部轨，与首段的排除集不一致。
        command.append("+")
        command += file_options
        command.append(str(extra))
    return command


def build_dv_segment_command(asset: BlurayTitleAsset, merged_video: Path,
                             audio_ts: Path, subtitle_ts: Path,
                             output_path: Path, *, title: str,
                             resolved: ResolvedTracks) -> list[str]:
    """多段合辑的**段级** DV 封装：合并视频 + 官方音轨中间件 + 官方字幕中间件。

    两个承重事实（2026-09-24 生产实测，用户裁定「这条也能做」）：

    * **DV 侧**：多段合辑用 mkvmerge 的 append 语法按 play item 续接，而 DV P7 是
      BL+EL 两条 HEVC 流，append 表达不了逐段 BL+EL 合并 ⇒ 每段先用 `dovi_tool` 把
      BL+EL 合成**单条** Profile 7 轨，再让 mkvmerge 追加。真机（Zidoo Z10 原生
      播放器）已证该形态可点亮 Dolby Vision。
    * **音频侧**：这类 DIY 盘的 TS，**mkvmerge 读不到音频**（`mkvmerge -J` 只有视频
      与 PGS，而 ffprobe/ffmpeg 能读到 8 条音轨、字节层 PID 亦在），管线又要求
      「没有可映射音轨即失败关闭」。故音轨/字幕由 **ffmpeg 按官方 PID 重封成标准
      TS 中间件**（`-mpegts_start_pid` 对齐官方 PID），mkvmerge 读中间件、按官方表
      注入语言与默认轨——两个工具各用自己读得动的部分，不猜、不降级。

    轨数**逐段等于官方条目数**（同 PID 的第二条流不另立轨）：成品门
    `validate_finalized_identification` 要求「成品音轨数 == 可映射官方条目数」，
    多带同 PID 的 AC-3 core 会变成 9≠8 而被拒（`dlv_5105a3a361c39b28d76f` 实测）。
    """
    command = ["mkvmerge", "--output", str(output_path), "--title", title, "--gui-mode"]
    command += ["--video-tracks", "0", "--language", "0:und",
                "--default-track-flag", "0:yes", str(merged_video)]
    audio_by_index = {index: track_id for index, track_id in resolved.audio_by_official_index}
    if resolved.audio_ids:
        if not audio_ts.is_file():
            raise MatroskaBuildError(
                "多段 DV 段级封装缺少音轨中间件（ffmpeg 未产出），拒绝生成静音片段")
        command += ["--audio-tracks", ",".join(map(str, resolved.audio_ids))]
        default_audio_index = asset.default_audio_index
        for official in asset.audio_tracks:
            track_id = audio_by_index.get(official.index)
            if track_id is None:
                continue
            command += ["--language", f"{track_id}:{official.language or 'und'}"]
            command += ["--default-track-flag",
                        f"{track_id}:{'yes' if official.index == default_audio_index else 'no'}"]
        command.append(str(audio_ts))
    else:
        command += ["--no-audio"]
    subtitle_by_index = {index: track_id
                         for index, track_id in resolved.subtitle_by_official_index}
    if resolved.subtitle_ids:
        if not subtitle_ts.is_file():
            raise MatroskaBuildError(
                "多段 DV 段级封装缺少字幕中间件（ffmpeg 未产出），拒绝丢字幕")
        command += ["--subtitle-tracks", ",".join(map(str, resolved.subtitle_ids))]
        default_subtitle_index = asset.default_subtitle_index
        for official in asset.subtitle_tracks:
            track_id = subtitle_by_index.get(official.index)
            if track_id is None:
                continue
            command += ["--language", f"{track_id}:{official.language or 'und'}"]
            command += ["--default-track-flag",
                        f"{track_id}:{'yes' if official.index == default_subtitle_index else 'no'}"]
        command.append(str(subtitle_ts))
    else:
        command += ["--no-subtitles"]
    return command


def build_append_only_command(first: Path, rest: Sequence[Path],
                              output_path: Path, *, title: str) -> list[str]:
    """多段 DV 成品的**纯追加**命令（每段已是自洽的单视频轨 DV MKV）。

    段级封装已把语言/默认轨/轨道布局定好；追加只做时间轴续接（mkvmerge 按前
    一段末端续接，非字节拼接）。轨布局由段级构造保证一致，故无需 `--append-to`
    显式映射（该映射只在"首段被逐轨裁剪过"时才必需，见 build_mkvmerge_command）。
    """
    command = ["mkvmerge", "--output", str(output_path), "--title", title, "--gui-mode",
               str(first)]
    for path in rest:
        command += ["+", str(path)]
    return command


def _aggregate_multiclip_dv_facts(dv_evidence: list[dict]) -> dict:
    """把逐段 DV 证据按播放序聚合成单条 ``dv_facts``（统一 Profile 7 成品门的入参）。

    为什么必须聚合而不是逐段过关（2026-09-24 实现口径）：

    * ``el_source_digests``：成品是各段**按播放序追加**出来的，其 EL 有序 VCL
      序列正是各段序列的连接——成品门 `_verify_el_order_consistency` 做的是
      "回拆成品 EL 与源序列**严格相等**"，因此源序列必须按同一顺序拼接，否则
      门形同虚设（或永远假失败）。
    * ``bl_size`` / ``merged_size``：求和，门要求 merged > bl（EL 确实被融合）。
    * ``merged_rpu_evidence``：工具原生标记与 limit_frames 沿用，``rpu_file_size``
      为各段求和，summary 记段数与逐段计数——任一段缺证据即失败关闭。
    * ``duration_s`` 求和（门据此把 10/50/90% 比例换算成秒），``fps`` 取首段实测。
    """
    if not dv_evidence:
        raise MatroskaBuildError("多段 DV 证据为空，拒绝发布")
    digests: list[str] = []
    bl_size = 0
    merged_size = 0
    duration_s = 0.0
    rpu_sizes: list[int] = []
    for item in dv_evidence:
        digests.extend(str(value) for value in (item.get("el_digests") or []))
        bl_size += int(item.get("bl_size") or 0)
        merged_size += int(item.get("merged_size") or 0)
        duration_s += float(item.get("duration_s") or 0.0)
        evidence = item.get("merged_rpu_evidence") or {}
        rpu_sizes.append(int(evidence.get("rpu_file_size") or 0))
    if any(size <= 0 for size in rpu_sizes):
        raise MatroskaBuildError("多段 DV 某段缺少合并期 RPU 证据，拒绝发布")
    first_rpu = dv_evidence[0].get("merged_rpu_evidence") or {}
    dovi_executable = str(dv_evidence[0].get("dovi_executable") or "")
    if not dovi_executable:
        raise MatroskaBuildError("多段 DV 缺少 dovi_tool 路径事实，拒绝发布")
    return {
        "dovi_executable": dovi_executable,
        "duration_s": duration_s,
        "fps": dv_evidence[0].get("fps"),
        "bl_size": bl_size,
        "merged_size": merged_size,
        "merged_rpu_evidence": {
            "method": first_rpu.get("method"),
            "limit_frames": first_rpu.get("limit_frames"),
            "rpu_file_size": sum(rpu_sizes),
            "rpu_summary": (
                f"多段合辑逐段合并流 RPU 证据：{len(dv_evidence)} 段，"
                f"逐段 rpu_file_size={rpu_sizes}，合计 {sum(rpu_sizes)} 字节"
            ),
        },
        "el_source_digests": digests,
        "el_source_digest_stats": {
            "vcl_count": len(digests),
            "payload_bytes": sum(int((item.get("el_stats") or {}).get("payload_bytes") or 0)
                                 for item in dv_evidence),
        },
        "multiclip_segments": [
            {"label": item.get("label"), "audio_tracks": item.get("audio_tracks"),
             "subtitle_tracks": item.get("subtitle_tracks"),
             "source_pids": item.get("source_pids")}
            for item in dv_evidence
        ],
    }


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


_MISMATCH_TRACK_LIMIT = 4
_MISMATCH_LIST_WIDTH = 120


def _identified_track_facts(tracks: Iterable[IdentifiedTrack]) -> str:
    """mkvmerge 输出轨列表的可读、有界事实串（id/PID/codec/语言）。"""
    facts = []
    for track in tracks:
        pid = hex(track.stream_id) if track.stream_id is not None else "?"
        facts.append(
            f"id={track.id}/pid={pid}/{track.codec}/{(track.language or 'und').lower()}"
        )
    return _bounded_fact_list(facts)


def _mapped_official_facts(asset_tracks: Iterable, pairs: Iterable[tuple[int, int]]) -> str:
    """MPLS 官方轨 → 成品轨的可读、有界映射事实串。"""
    by_index = {int(track.index): track for track in asset_tracks}
    facts = []
    for official_index, track_id in pairs:
        official = by_index.get(int(official_index))
        if official is None:
            facts.append(f"official={official_index}→id={track_id}")
            continue
        facts.append(
            f"official={official_index}/pid=0x{int(official.pid):x}"
            f"→id={track_id}"
        )
    return _bounded_fact_list(facts)


def _bounded_fact_list(facts: list[str]) -> str:
    """轨道事实串：最多 limit 项且不超过宽度上限（超出部分省略号标记）。"""
    text = ", ".join(facts[:_MISMATCH_TRACK_LIMIT]) or "无"
    if len(facts) > _MISMATCH_TRACK_LIMIT:
        text += f", …(+{len(facts) - _MISMATCH_TRACK_LIMIT})"
    return _bounded_detail(text, _MISMATCH_LIST_WIDTH)


def _require_identified_container(payload: dict, *, label: str) -> None:
    """识别结果必须是**已识别出的容器**，否则失败关闭。

    2026-09-17：mkvmerge `--identify` 对打不开/未产出的文件会把错误放进
    stdout 的 JSON（`{"errors": [...]}`，无 `container`），并以退出码 2 返回。
    旧实现同时接受退出码 2，于是这份错误 JSON 被当成有效识别继续走时长门，
    最终报出「最终 MKV 缺少有效时长/Cues 信息」——把"根本没有成品"伪装成
    "成品缺一个可选字段"。这里在时长门之前先收口：error 列表非空、或容器被
    明确标为未识别（`container.recognized == false`），一律失败关闭并带上
    mkvmerge 原文，避免真实封装失败被内容门掩盖。
    """
    errors = payload.get("errors") or []
    if errors:
        joined = "；".join(str(error).strip() for error in errors)
        raise MatroskaBuildError(
            f"{label}失败：mkvmerge 未产出可识别的容器——"
            f"{_bounded_detail(joined, 300)}"
        )
    container = payload.get("container")
    if not isinstance(container, dict) or container.get("recognized") is False:
        raise MatroskaBuildError(
            f"{label}失败：mkvmerge 未识别出有效的 Matroska 容器，拒绝继续"
        )


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
    # 2026-09-14 P2：数量不一致必须自带可重建的差异事实（成品 id/PID/codec/
    # 语言 + 官方 index/PID→成品 id）；无命令日志时这是唯一可诊断来源。
    if len(audio) != len(resolved.audio_by_official_index):
        raise MatroskaBuildError(
            "最终 MKV 音轨数量与可映射 MPLS 音轨不一致："
            f"成品 {len(audio)} 条（{_identified_track_facts(audio)}），"
            f"可映射 {len(resolved.audio_by_official_index)} 条"
            f"（{_mapped_official_facts(asset.audio_tracks, resolved.audio_by_official_index)}）"
        )
    if len(subtitles) != len(resolved.subtitle_by_official_index):
        raise MatroskaBuildError(
            "最终 MKV 字幕数量与可映射 MPLS 字幕不一致："
            f"成品 {len(subtitles)} 条（{_identified_track_facts(subtitles)}），"
            f"可映射 {len(resolved.subtitle_by_official_index)} 条"
            f"（{_mapped_official_facts(asset.subtitle_tracks, resolved.subtitle_by_official_index)}）"
        )
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


def _expected_mkvmerge_output_order(
        pairs: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
    """mkvmerge 产物的音轨/字幕顺序 = **源文件内的轨顺序**（track ID 升序）。

    2026-09-19（音频承诺 Gold Case `dlv_895570e5d72cbfd7d5f9` 定因）：

    `--audio-tracks` / `--subtitle-tracks` 是**选择**列表，不是排序指令。
    本机 mkvmerge 实测：源轨 1=eng / 2=jpn / 3=chi，`--audio-tracks 3,2,1`
    的产物顺序仍是 1,2,3（eng, jpn, chi）。而本模块的
    `audio_by_official_index` / `subtitle_by_official_index` 按 **MPLS 官方表
    顺序**产出（`_resolve_official_pairs` 刻意保持官方表顺序）。两者只有在
    "官方表顺序恰好等于轨 ID 升序"时才一致；一旦不等，逐位比对就会把
    **每条轨语言都正确**的成品判成语言错——生产实证：好家伙（`ded5e7883c8e9e
    5851e66963`）在 stage=output_validation 98% 以
    「最终 MKV 音轨 1 语言 eng 与 MPLS 期望 jpn 不一致」失败关闭，用户拿不到
    已正确生成的交付。

    修复方向属**验证器语义错**（生成正确、判据错），不是放宽校验：数量门不变，
    逐轨仍要求"输出轨语言 == 映射到它的那条官方轨的语言"，只是把期望顺序
    换成 mkvmerge 真实输出顺序。（不改生成侧去加 `--track-order`：那会改变
    用户可见的"第一音轨"，属另一类变更，且无证据要求。）
    """
    return tuple(sorted(pairs, key=lambda pair: pair[1]))


def _validate_track_languages(asset: BlurayTitleAsset, resolved: ResolvedTracks,
                             audio: list, subtitles: list) -> None:
    """逐轨校验输出语言与 MPLS 官方语言一致（构建注入后的期望值）。

    round-2 P1-2：mkvmerge 最终 MKV 的 track.id 会重新编号（video=0、
    audio=1..、subtitle=..），与输入识别的 track ID 不同域。这里按**
    mkvmerge 真实输出顺序**（源轨 ID 升序，见
    :func:`_expected_mkvmerge_output_order`）与官方轨道逐轨比较；官方轨道按
    `BlurayTrack.index` 匹配（不是官方表位置——见该函数的定因说明）。
    """
    audio_by_index = {track.index: track for track in asset.audio_tracks}
    subtitle_by_index = {track.index: track for track in asset.subtitle_tracks}
    audio_expected = _expected_mkvmerge_output_order(
        resolved.audio_by_official_index)
    subtitle_expected = _expected_mkvmerge_output_order(
        resolved.subtitle_by_official_index)
    for position, track in enumerate(audio):
        if position >= len(audio_expected):
            continue
        official_index, _ = audio_expected[position]
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
        if position >= len(subtitle_expected):
            continue
        official_index, _ = subtitle_expected[position]
        official = subtitle_by_index.get(official_index)
        if official is None:
            continue
        expected = (official.language or "und").lower()
        if _canonical_language(track.language) != _canonical_language(expected):
            raise MatroskaBuildError(
                f"最终 MKV 字幕轨 {position} 语言 {track.language or 'und'} "
                f"与 MPLS 期望 {expected} 不一致"
            )


# ── 终化成品轨覆盖门（E1） ───────────────────────────────────────────────
#
# 2026-09-17《这个杀手不太冷》生产事故：终化成品里 13 条字幕轨**计数完全正确**
# （13 == 13、语言也对），但内容只覆盖到 2,608s / 7,978s（前 33%）——字幕在第 2
# 个 play item 之后整段消失，前面每一道门都放行。即：**"系统生成了文件"被当成
# "内容正确"**。本门补上后一半：
#
#   1. 对成品本身**独立测量**每条声明轨的在场区间，不信任任何计数/清单元数据；
#   2. 稀疏轨（字幕）用**最长未覆盖空档**判据。窗口间距 d 与窗宽 w 满足
#      d + w ≤ max_gap ⇒ **任何长度 > max_gap 的空档必然完整包含至少一个采样
#      窗口**（证明：取 ≥ 空档起点的最小采样起点 s，则 s < a + d，
#      故 s + w < a + d + w = a + max_gap < a + (b - a) = b）。这是可证明的
#      检出保证，不是抽样碰运气；
#   3. 连续轨（视频/音频）用抽点在场断言——任一采样窗口缺席即失败关闭；
#   4. 任何失败 → fail closed 并点名（轨、首末观测、最长空档），绝不降级为警告。
#
# 放行例外只允许来自显式源侧事实证明：`expected_end_by_stream` 可证明轨道
# 地平线；多段 DV 的完整源 packet 时间戳证明输入段自身的字幕空窗。
# 证明须随构建结果留痕；采样窗内任何源侧在场而成品缺失仍失败关闭。
# 「某一源段没声明该 PID」不构成放行理由——那正是 Léon 事故的形态。
_COVERAGE_SUBTITLE_MAX_GAP_SECONDS = 600.0
_COVERAGE_WINDOW_SECONDS = 60.0
_COVERAGE_DENSE_SAMPLES = 12
_COVERAGE_TAIL_TOLERANCE_SECONDS = 2.0
_COVERAGE_PROBE_TIMEOUT = 300
_COVERAGE_FACT_LIMIT = 6
_COVERAGE_TRUE_KINDS = frozenset({"subtitles"})


@dataclass(frozen=True)
class CoverageWindow:
    """一个采样窗口（秒，成品时间轴）。"""

    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class SourceSubtitleEvidence:
    """源侧字幕包时间轴与其在同一采样窗中的派生在场事实。"""

    packet_timestamps_by_stream: dict[int, tuple[float, ...]]
    presence_by_window: tuple[frozenset[int], ...]


@dataclass(frozen=True)
class TrackCoverageFact:
    """一条成品轨的实测覆盖事实（门与证据共用同一份测量）。"""

    position: int
    kind: str
    codec: str
    language: str
    windows_present: int
    windows_total: int
    first_seen: float | None
    last_seen: float | None
    longest_gap: float
    longest_gap_span: tuple[float, float] | None

    @property
    def label(self) -> str:
        return (f"轨{self.position} {self.kind or '?'}/{self.codec or '?'}"
                f"（{self.language or 'und'}）")

    def text(self) -> str:
        first = "—" if self.first_seen is None else f"{self.first_seen:.1f}s"
        last = "—" if self.last_seen is None else f"{self.last_seen:.1f}s"
        return (f"{self.label} 在场 {self.windows_present}/{self.windows_total} 窗，"
                f"首 {first} / 末 {last}，最长空档 {self.longest_gap:.1f}s")


def coverage_windows(duration: float, *, max_gap: float,
                     window: float) -> tuple[CoverageWindow, ...]:
    """稀疏轨采样计划：任何长度 > max_gap 的空档必定完整命中一个窗口。

    step = max_gap - window 是保证成立的前提（见本节的检出保证证明）；因此
    window 必须 < max_gap，否则窗口互相重叠到失去意义。末尾另加一个贴住
    影片结尾的窗口——它同时是"覆盖到接近结尾"的锚点。
    """
    duration = float(duration)
    max_gap = float(max_gap)
    window = float(window)
    if duration <= 0:
        raise MatroskaBuildError("轨覆盖门需要正的成品时长，拒绝在未知时长上放行")
    if window <= 0 or max_gap <= 0 or window >= max_gap:
        raise MatroskaBuildError(
            f"轨覆盖门采样参数非法：window={window} max_gap={max_gap}"
            "（必须 0 < window < max_gap）"
        )
    step = max_gap - window
    windows: list[CoverageWindow] = []
    start = 0.0
    while start < duration:
        windows.append(CoverageWindow(start=start,
                                      end=min(duration, start + window)))
        start += step
    tail_start = max(0.0, duration - window)
    if windows[-1].start < tail_start:
        windows.append(CoverageWindow(start=tail_start, end=duration))
    return tuple(windows)


def dense_sample_windows(duration: float, *, count: int,
                         window: float) -> tuple[CoverageWindow, ...]:
    """连续轨抽点计划：均匀铺满 + 末尾一个贴尾窗口。"""
    duration = float(duration)
    window = float(window)
    if duration <= 0:
        raise MatroskaBuildError("轨覆盖门需要正的成品时长，拒绝在未知时长上放行")
    width = min(window, duration)
    spans = max(1, int(count))
    usable = max(0.0, duration - width)
    windows = [
        CoverageWindow(start=usable * index / spans, end=usable * index / spans + width)
        for index in range(spans + 1)
    ]
    tail = CoverageWindow(start=max(0.0, duration - width), end=duration)
    if windows[-1] != tail:
        windows.append(tail)
    return tuple(windows)


def longest_uncovered_gap(
    present: Sequence[bool],
    windows: Sequence[CoverageWindow],
    duration: float,
) -> tuple[float, tuple[float, float] | None]:
    """最长**未覆盖**空档（秒）及其区间；首尾也算空档。

    只把"观测到在场的窗口"当作覆盖证据：窗口之间的空白、影片开头到首个
    在场窗口、最后一个在场窗口到影片结尾，全部计入空档。因此"字幕在 2,608s
    之后消失"会得到 5,170s 的空档，而不是被当成"结尾没有对白"放过。
    """
    covered = [w for w, hit in zip(windows, present) if hit]
    if not covered:
        return float(duration), (0.0, float(duration))
    gaps: list[tuple[float, float]] = []
    head = covered[0].start
    if head > 0:
        gaps.append((0.0, head))
    for previous, following in zip(covered, covered[1:]):
        if following.start > previous.end:
            gaps.append((previous.end, following.start))
    tail = float(duration) - covered[-1].end
    if tail > 0:
        gaps.append((covered[-1].end, float(duration)))
    if not gaps:
        return 0.0, None
    longest = max(gaps, key=lambda span: span[1] - span[0])
    return longest[1] - longest[0], longest


def longest_uncovered_packet_gap(
    packet_timestamps: Sequence[float], duration: float,
) -> tuple[float, tuple[float, float] | None]:
    """用完整 packet PTS 列表计算字幕轨的真实源侧最大空档。"""
    horizon = float(duration)
    if horizon <= 0:
        raise MatroskaBuildError("源字幕包空档证明需要正的时间地平线")
    times: list[float] = []
    for value in packet_timestamps:
        try:
            stamp = float(value)
        except (TypeError, ValueError) as error:
            raise MatroskaBuildError("源字幕包空档证明含非法 packet 时间戳") from error
        if not math.isfinite(stamp) or stamp < -0.5 or stamp > horizon + 0.5:
            raise MatroskaBuildError(
                f"源字幕包空档证明时间戳越界：{stamp!r}s / {horizon:.1f}s"
            )
        times.append(min(horizon, max(0.0, stamp)))
    ordered = sorted(set(times))
    if not ordered:
        return horizon, (0.0, horizon)
    gaps = [(0.0, ordered[0])]
    gaps.extend((previous, following)
                for previous, following in zip(ordered, ordered[1:]))
    gaps.append((ordered[-1], horizon))
    longest = max(gaps, key=lambda span: span[1] - span[0])
    gap = longest[1] - longest[0]
    return gap, longest if gap > 0 else None


def _probe_window_presence(probe: str, path: Path,
                           windows: Sequence[CoverageWindow], *,
                           timeout: int = _COVERAGE_PROBE_TIMEOUT,
                           ) -> tuple[frozenset[int], ...]:
    """逐窗口测量"哪些 ffprobe 流序号在该窗口内有包"。

    只读探针：`-read_intervals` 让 ffprobe 只解到窗口附近，不整片读取，
    因此对 70GB 级成品是"按窗口取样"而不是全量扫描。
    """
    presence: list[frozenset[int]] = []
    for window in windows:
        command = [
            probe, "-v", "error",
            "-read_intervals", f"{window.start:.3f}%+{window.length:.3f}",
            "-show_entries", "packet=stream_index,pts_time",
            "-of", "csv=p=0", str(path),
        ]
        try:
            completed = subprocess.run(command, check=True, capture_output=True,
                                       text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise _tool_failure(
                f"成品轨覆盖探针（窗口 {window.start:.1f}s）", error) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise MatroskaBuildError(
                f"成品轨覆盖探针失败（窗口 {window.start:.1f}s）："
                f"{_bounded_detail(_process_error_detail(error), 200)}"
            ) from error
        indices: set[int] = set()
        for line in completed.stdout.splitlines():
            fields = line.split(",")
            if not fields:
                continue
            try:
                index = int(fields[0])
            except (TypeError, ValueError):
                continue
            # pts_time 可能为 N/A（探针放不出时间戳）：无法定位就按"在本窗口内"
            # 计入——宁可把在场判宽，也不能把不在场判宽（那是漏检方向）。
            stamp = None
            if len(fields) > 1 and fields[1] not in ("N/A", ""):
                try:
                    stamp = float(fields[1])
                except (TypeError, ValueError):
                    stamp = None
            if stamp is None or (window.start - 0.5) <= stamp <= (window.end + 0.5):
                indices.add(index)
        presence.append(frozenset(indices))
    return tuple(presence)


def _probe_source_subtitle_presence(
    probe: str,
    segments: Sequence[Path],
    windows: Sequence[CoverageWindow],
    *,
    timeout: int = _COVERAGE_PROBE_TIMEOUT,
) -> tuple[frozenset[int], ...]:
    """完整扫描输入字幕包，再派生各覆盖窗口的源侧在场事实。"""
    return _probe_source_subtitle_evidence(
        probe, segments, windows, timeout=timeout,
    ).presence_by_window


def _resolve_segment_track_mapping(
    probe: str,
    segments: Sequence[Path],
    resolved,
    declared: Sequence,
) -> dict[int, int] | None:
    """非 DV 多段路线的豁免前置：从构建自身的轨选择推导 TS 流序号→轨位映射。

    mkvmerge 按 `resolved`（MPLS 官方表映射出的 video/audio/subtitle 轨 ID，
    顺序即成品轨序）从段里选轨；裁切 TS 用 `-map 0` 保留全部流，可能比成品
    多出未被 MPLS 引用的流。映射成立当且仅当：每段 ffprobe 的 codec_type
    序列（subtitle→subtitles）完全一致，且对选择集逐位验证
    `types[selection_id] == declared[position]`。任何不一致或探针失败返回
    None——豁免关闭，回到原 600 秒失败门（fail-closed），并留诊断日志。
    """
    expected = tuple(str(track.type) for track in declared)
    selection = (tuple(resolved.video_ids) + tuple(resolved.audio_ids)
                 + tuple(resolved.subtitle_ids))
    if len(selection) != len(expected):
        logger.warning(
            "源侧豁免映射不可证明：选择集 %d 轨 != 成品 %d 轨",
            len(selection), len(expected))
        return None
    reference_types = None
    for segment in segments:
        command = [
            probe, "-v", "error", "-show_entries", "stream=codec_type",
            "-of", "csv=p=0", str(segment),
        ]
        try:
            completed = subprocess.run(
                command, check=True, capture_output=True, text=True,
                timeout=_COVERAGE_PROBE_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning("源侧豁免布局探针失败（%s）：%s",
                           segment.name, _bounded_detail(str(error), 160))
            return None
        types = []
        for line in completed.stdout.splitlines():
            kind = line.strip()
            if kind == "subtitle":
                kind = "subtitles"
            types.append(kind)
        if reference_types is None:
            reference_types = tuple(types)
            mismatch = [
                position for position, stream_id in enumerate(selection)
                if position >= len(types) or types[stream_id] != expected[position]]
            if mismatch:
                logger.warning(
                    "源侧豁免映射不可证明：段 %s 类型序 %s / 选择 %s / 成品 %s，"
                    "失配位 %s", segment.name, types, list(selection),
                    list(expected), mismatch[:6])
                return None
        elif tuple(types) != reference_types:
            logger.warning(
                "源侧豁免映射不可证明：段 %s 类型序 %s 与首段 %s 不一致",
                segment.name, types, list(reference_types))
            return None
    return {int(stream_id): position
            for position, stream_id in enumerate(selection)}


def _probe_source_subtitle_evidence(
    probe: str,
    segments: Sequence[Path],
    windows: Sequence[CoverageWindow],
    *,
    timeout: int = _COVERAGE_PROBE_TIMEOUT,
) -> SourceSubtitleEvidence:
    """完整读取分段字幕 packet PTS，并按追加时间轴产生精确空档证明。

    不能从有限采样窗反推实际源空档：落在窗口之间的字幕簇会被漏看。每个段级
    MKV 只扫描一次字幕 packet，按实测段时长累计偏移；窗口在场仅作为输出对照，
    源侧长空档例外则使用完整 packet 时间戳计算。任何未定位包、探针失败或时间轴
    不足均失败关闭。
    """
    if not segments or not windows:
        raise MatroskaBuildError("源字幕覆盖证明缺少输入段或采样窗口，拒绝豁免")
    packet_timestamps: dict[int, list[float]] = {}
    presence: list[set[int]] = [set() for _ in windows]
    timeline_offset = 0.0
    for segment_index, segment in enumerate(segments):
        segment_duration = _probe_duration(probe, Path(segment))
        if segment_duration <= 0:
            raise MatroskaBuildError(
                f"源字幕覆盖证明分段 {segment_index + 1} 时长无效，拒绝豁免"
            )
        command = [
            probe, "-v", "error", "-select_streams", "s", "-show_packets",
            "-show_entries", "packet=stream_index,pts_time", "-of", "csv=p=0",
            str(segment),
        ]
        try:
            completed = subprocess.run(command, check=True, capture_output=True,
                                       text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise _tool_failure(
                f"源字幕覆盖证明（全分段 {segment_index + 1} packet 扫描）",
                error) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise MatroskaBuildError(
                f"源字幕覆盖证明失败（全分段 {segment_index + 1} packet 扫描）："
                f"{_bounded_detail(_process_error_detail(error), 200)}"
            ) from error
        for line in completed.stdout.splitlines():
            fields = line.split(",")
            if len(fields) < 2 or fields[1] in ("N/A", ""):
                raise MatroskaBuildError(
                    f"源字幕覆盖证明遇到不可定位的 packet（分段 {segment_index + 1}），"
                    "拒绝豁免"
                )
            try:
                stream_index = int(fields[0])
                stamp = float(fields[1])
            except (TypeError, ValueError) as error:
                raise MatroskaBuildError(
                    f"源字幕覆盖证明遇到非法 packet 时间戳（分段 {segment_index + 1}），"
                    "拒绝豁免"
                ) from error
            # 3.7.140→3.7.142：mpegts 裁切尾部的杂散字幕包 PTS 可越出标称段长
            # （王朝三轮实测 +2.2s / +7.9s / +33.8s，每轮重裁量不同）——这不是
            # 时间轴损坏。越界包一律钳到段尾参与累计（下行 clamp 原有语义），
            # 仅拒绝负时间、非有限值与 > 2×段长+60s 的真损坏形态。
            if (not math.isfinite(stamp) or stamp < -0.5
                    or stamp > segment_duration * 2.0 + 60.0):
                raise MatroskaBuildError(
                    f"源字幕覆盖证明时间戳越界（分段 {segment_index + 1}）："
                    f"{stamp!r}s / {segment_duration:.1f}s，拒绝豁免"
                )
            absolute_stamp = timeline_offset + min(segment_duration, max(0.0, stamp))
            packet_timestamps.setdefault(stream_index, []).append(absolute_stamp)
            for window_index, window in enumerate(windows):
                if window.start - 0.5 <= absolute_stamp <= window.end + 0.5:
                    presence[window_index].add(stream_index)
        timeline_offset += segment_duration
    if timeline_offset + _COVERAGE_TAIL_TOLERANCE_SECONDS < windows[-1].end:
        raise MatroskaBuildError(
            f"源字幕覆盖证明时长不足：输入段 {timeline_offset:.1f}s / "
            f"成品采样地平线 {windows[-1].end:.1f}s，拒绝豁免"
        )
    return SourceSubtitleEvidence(
        packet_timestamps_by_stream={
            index: tuple(sorted(stamps)) for index, stamps in packet_timestamps.items()
        },
        presence_by_window=tuple(frozenset(indices) for indices in presence),
    )


def summarise_track_coverage(
    identified_tracks: Sequence[IdentifiedTrack],
    presence: Sequence[frozenset[int]],
    windows: Sequence[CoverageWindow],
    duration: float,
) -> tuple[TrackCoverageFact, ...]:
    """把逐窗口在场事实折算成逐轨覆盖事实（纯函数，可离线复核）。

    ffprobe 的流序号与 mkvmerge 的轨道声明按**同一容器顺序**一一对应，这是
    Matroska 容器内流顺序的定义，不依赖任何猜测。
    """
    facts: list[TrackCoverageFact] = []
    for position, track in enumerate(identified_tracks):
        hits = tuple(position in seen for seen in presence)
        gap, span = longest_uncovered_gap(hits, windows, duration)
        covered = [window for window, hit in zip(windows, hits) if hit]
        facts.append(TrackCoverageFact(
            position=position,
            kind=track.type,
            codec=track.codec,
            language=track.language,
            windows_present=len(covered),
            windows_total=len(windows),
            first_seen=covered[0].start if covered else None,
            last_seen=covered[-1].end if covered else None,
            longest_gap=gap,
            longest_gap_span=span,
        ))
    return tuple(facts)


def require_track_coverage(
    probe: str,
    path: Path,
    identified_tracks: Sequence[IdentifiedTrack],
    *,
    duration: float,
    label: str,
    expected_end_by_stream: dict[int, float] | None = None,
    source_presence_by_window: Sequence[frozenset[int]] | None = None,
    source_packet_timestamps_by_stream: dict[int, Sequence[float]] | None = None,
    enforce: bool = True,
) -> dict:
    """E1 终化成品完整性门：每条声明轨都必须**被测量证明**覆盖到结尾。

    失败关闭口径（任一成立即拒绝，绝不降级为警告）：
      - 视频/音频轨：任一采样窗口缺席（连续流缺席窗口不是"静音段"，是丢失），
        或覆盖终点早于地平线 `required_end - _COVERAGE_TAIL_TOLERANCE_SECONDS`；
      - 字幕轨：以**该轨被证明的地平线**衡量的最长未覆盖空档 >
        ``_COVERAGE_SUBTITLE_MAX_GAP_SECONDS``。

    地平线默认是影片结尾；``expected_end_by_stream`` 可以按**源侧事实证明**
    把它缩到某条轨的真实终点（键为轨道位置，与 ffprobe 流序号一致）。传 None
    时所有轨的期望终点都是影片结尾——这正是"不得因为某一源段没声明某 PID 就
    默认整条字幕可以消失"的落点，也让例外必须显式、可审计、逐轨生效。

    多段 DV 可传入 `source_presence_by_window` 和完整的
    `source_packet_timestamps_by_stream`：成品须保留每个源侧在场窗，且完整源包时间戳
    证明源自身已有 >600s 空窗，才可将其作为覆盖事实。有限采样窗不能代替完整包事实。

    ``enforce=False`` 是**取证模式**：照常测量、照常把逐轨事实与违规项写进返回值，
    但不抛异常。它只用于"默认口径尚未被源侧事实证明覆盖"的路径——目前是**单段
    （单碟）终化**：多段路线有 ``require_segment_track_correspondence`` 逐段证明
    每个参封段都带全部轨（不带的段已被尾部排除），故"全时长期望"是有源侧依据的，
    可以失败关闭；而单段路线没有这层证明，母盘上本来就可能存在只覆盖局部的字幕轨
    （强制字幕/评论字幕），直接失败关闭会误伤健康成品。**这不是放宽 Léon 那类缺陷
    的检出**：Léon 是多段合辑，走 enforce=True 的路径。
    """
    declared = tuple(identified_tracks)
    if not declared:
        raise MatroskaBuildError(f"{label}轨覆盖门：成品未声明任何轨道，拒绝发布")
    seconds = float(duration)
    wants_sparse = any(track.type in _COVERAGE_TRUE_KINDS for track in declared)
    sparse_windows = (coverage_windows(
        seconds, max_gap=_COVERAGE_SUBTITLE_MAX_GAP_SECONDS,
        window=_COVERAGE_WINDOW_SECONDS) if wants_sparse else ())
    dense_windows = dense_sample_windows(
        seconds, count=_COVERAGE_DENSE_SAMPLES, window=_COVERAGE_WINDOW_SECONDS)
    sparse_presence = (_probe_window_presence(probe, path, sparse_windows)
                       if wants_sparse else ())
    dense_presence = _probe_window_presence(probe, path, dense_windows)
    sparse_facts = (summarise_track_coverage(
        declared, sparse_presence, sparse_windows, seconds) if wants_sparse else ())
    dense_facts = summarise_track_coverage(
        declared, dense_presence, dense_windows, seconds)
    overrides = dict(expected_end_by_stream or {})
    source_presence = (tuple(source_presence_by_window)
                       if source_presence_by_window is not None else None)
    if source_presence is not None and source_packet_timestamps_by_stream is None:
        raise MatroskaBuildError(
            f"{label}源字幕覆盖证明缺少完整 packet 时间戳，拒绝空窗豁免"
        )
    source_packet_timestamps = dict(source_packet_timestamps_by_stream or {})
    if source_presence is not None and len(source_presence) != len(sparse_windows):
        raise MatroskaBuildError(
            f"{label}源字幕覆盖证明窗口数不匹配："
            f"{len(source_presence)} != {len(sparse_windows)}，拒绝豁免"
        )
    source_proven_by_stream: dict[int, int] = {}

    failures: list[str] = []
    lines: list[str] = []
    reported: list[TrackCoverageFact] = []
    for position, track in enumerate(declared):
        sparse = track.type in _COVERAGE_TRUE_KINDS
        fact = sparse_facts[position] if sparse else dense_facts[position]
        reported.append(fact)
        required_end = min(seconds, float(overrides.get(position, seconds)))
        lines.append(fact.text())
        if sparse:
            # 稀疏轨：唯一判据是"最长未覆盖空档"（以该轨**被证明的终点**为地平线）。
            # 这条判据把"结尾没有对白"与"中途截断"区分开：前者是几十分钟量级的
            # 正常静默且不会跨过 max_gap 上限，后者是后半部整段消失。
            #
            # 这里刻意**不**加"最后时间戳 ≥ 片尾 − 2s"这类绝对判据：采样的粒度是
            # max_gap 量级，一条只在 1300s 前有 cue 的健康轨在 1400s 的片里本就
            # 可能缺席贴尾窗口，绝对判据会把它误判成截断。地平线口径同时让
            # `expected_end_by_stream` 的语义自然落地：被证明提前结束的轨，其
            # "片尾"就是那个证明终点。
            hits = tuple(position in seen for seen in sparse_presence)
            gap, span = longest_uncovered_gap(hits, sparse_windows, required_end)
            if source_presence is not None:
                source_hits = tuple(position in seen for seen in source_presence)
                source_proven_by_stream[position] = sum(source_hits)
                lost_windows = [
                    index for index, (source_hit, output_hit)
                    in enumerate(zip(source_hits, hits))
                    if source_hit and not output_hit
                ]
                if not any(source_hits):
                    failures.append(
                        f"{fact.label} 源侧未能证明任何字幕在场采样窗，拒绝空窗豁免"
                    )
                elif lost_windows:
                    first_lost = lost_windows[0]
                    sample = sparse_windows[first_lost]
                    failures.append(
                        f"{fact.label} 源侧在场窗缺失 {len(lost_windows)} 个"
                        f"（首 {sample.start:.1f}s–{sample.end:.1f}s），拒绝豁免"
                    )
                # 仅当成品保留了源侧所有抽样在场窗时，源侧也为空的间隔才
                # 可作为媒体自身的稀疏空窗证据；否则仍走原 600 秒失败门。
                elif gap > _COVERAGE_SUBTITLE_MAX_GAP_SECONDS:
                    packet_times = source_packet_timestamps.get(position)
                    if packet_times is None:
                        failures.append(
                            f"{fact.label} 缺少完整源字幕 packet 时间戳，拒绝空窗豁免"
                        )
                        continue
                    source_gap, source_span = longest_uncovered_packet_gap(
                        packet_times, required_end)
                    if source_gap <= _COVERAGE_SUBTITLE_MAX_GAP_SECONDS:
                        failures.append(
                            f"{fact.label} 成品最长未覆盖空档 {gap:.1f}s 超过上限，"
                            f"源侧实测最长空档 {source_gap:.1f}s"
                        )
                    else:
                        lines.append(
                            f"源侧证明 {fact.label} 稀疏空档 {source_gap:.1f}s"
                            + (f"（{source_span[0]:.1f}s–{source_span[1]:.1f}s）"
                               if source_span else "")
                        )
            elif gap > _COVERAGE_SUBTITLE_MAX_GAP_SECONDS:
                span_text = ("" if span is None
                             else f"（{span[0]:.1f}s–{span[1]:.1f}s）")
                failures.append(
                    f"{fact.label} 最长未覆盖空档 {gap:.1f}s{span_text} 超过上限 "
                    f"{_COVERAGE_SUBTITLE_MAX_GAP_SECONDS:.0f}s"
                    f"（地平线 {required_end:.1f}s）"
                )
            continue
        # 连续轨：任一采样窗口缺席即失败关闭——视频/音频没有"静音段"这回事。
        if fact.windows_present < fact.windows_total:
            missing = fact.windows_total - fact.windows_present
            first = 0.0 if fact.first_seen is None else fact.first_seen
            last = 0.0 if fact.last_seen is None else fact.last_seen
            failures.append(
                f"{fact.label} 有 {missing}/{fact.windows_total} 个采样窗口缺席"
                f"（首 {first:.1f}s / 末 {last:.1f}s）"
            )
        elif (fact.last_seen is not None
              and fact.last_seen < required_end - _COVERAGE_TAIL_TOLERANCE_SECONDS):
            failures.append(
                f"{fact.label} 覆盖只到 {fact.last_seen:.1f}s，"
                f"期望到 {required_end:.1f}s"
            )
    if failures and enforce:
        raise MatroskaBuildError(
            f"{label}轨覆盖门失败（时长 {seconds:.1f}s，共 {len(declared)} 条轨）："
            + "；".join(failures[:_COVERAGE_FACT_LIMIT])
            + (f"…(+{len(failures) - _COVERAGE_FACT_LIMIT})"
               if len(failures) > _COVERAGE_FACT_LIMIT else "")
            + "｜逐轨实测：" + "｜".join(lines[:_COVERAGE_FACT_LIMIT])
            + ("…" if len(lines) > _COVERAGE_FACT_LIMIT else "")
        )
    if failures:
        # 取证模式：不阻断构建，但必须留下与失败关闭同等的可诊断事实——
        # "静默通过"是本次事故的成因，取证模式不得重犯。
        logger.warning(
            "%s轨覆盖门（取证模式，未阻断）：%s｜逐轨实测：%s",
            label, "；".join(failures[:_COVERAGE_FACT_LIMIT]),
            "｜".join(lines[:_COVERAGE_FACT_LIMIT]),
        )
    return {
        "enforced": bool(enforce),
        "source_presence_verified": (
            source_presence is not None
            and source_packet_timestamps_by_stream is not None
        ),
        "violations": failures,
        "duration_seconds": seconds,
        "tracks": [
            {
                "position": fact.position,
                "type": fact.kind,
                "codec": fact.codec,
                "language": fact.language,
                "windows_present": fact.windows_present,
                "windows_total": fact.windows_total,
                "first_seen": fact.first_seen,
                "last_seen": fact.last_seen,
                "longest_gap_seconds": round(fact.longest_gap, 3),
                "source_proven_windows": source_proven_by_stream.get(fact.position),
                "source_max_gap_seconds": (
                    round(longest_uncovered_packet_gap(
                        source_packet_timestamps[fact.position], required_end)[0], 3)
                    if fact.position in source_packet_timestamps else None
                ),
                "expected_end_seconds": min(
                    seconds, float(overrides.get(fact.position, seconds))),
            }
            for fact in reported
        ],
    }


_SEGMENT_FACT_LIMIT = 6
_SEGMENT_FACT_WIDTH = 260
# 分段可比字段：mkvmerge 识别属性 + ffprobe 视频流事实。start_time/duration
# 按设计逐段不同（各 play item 从自己的 in_time 起），不参与比对。
_SEGMENT_PROPERTY_KEYS = (
    "codec_id", "profile", "pixel_dimensions", "display_dimensions",
    "default_duration", "audio_channels", "audio_sampling_frequency",
)
_SEGMENT_VIDEO_FACT_KEYS = (
    "codec_name", "profile", "width", "height", "pix_fmt",
    "avg_frame_rate", "r_frame_rate", "time_base",
)


def _segment_track_facts(payload: dict) -> tuple[tuple[str, int | None, str], ...]:
    """分段轨道事实表 ((type, pid, 可比事实串), ...)，复用主片识别的解析。

    事实串含局部轨 ID/codec/语言/通道数/档次/分辨率/帧时长——mkvmerge 追加
    按首段轨布局续接，任何一项逐段漂移都可能让对应流在拼接处静默截断。
    """
    raw = list(payload.get("tracks") or ())
    parsed = parse_identification(payload)
    if len(parsed) != len(raw):
        raise MatroskaBuildError("mkvmerge 识别结果的轨道数与原始条目不一致，拒绝按段核对")
    facts: list[tuple[str, int | None, str]] = []
    for identified, entry in zip(parsed, raw):
        properties = entry.get("properties") or {}
        parts = [f"id={identified.id}", identified.codec, identified.language or "und"]
        if identified.multiplexed_tracks:
            parts.append("multiplexed=" + ",".join(map(str, identified.multiplexed_tracks)))
        for key in _SEGMENT_PROPERTY_KEYS:
            value = properties.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        facts.append((identified.type, identified.stream_id, "|".join(parts)))
    return tuple(facts)


def _segment_key_text(key: tuple[str, int | None]) -> str:
    kind, pid = key
    return f"{kind}/pid={'?' if pid is None else hex(pid)}"


def _segment_fact_map(
    facts: tuple[tuple[str, int | None, str], ...],
) -> dict[tuple[str, int | None], tuple[str, ...]]:
    """按 (type, pid) 归组的事实表。

    同一 PID 可以有多条轨（TrueHD 主轨 + 内嵌 AC-3 core，mkvmerge 按同
    stream_id 分别暴露），因此值是**有序事实元组**而不是单条事实。
    """
    mapping: dict[tuple[str, int | None], list[str]] = {}
    for kind, pid, fact in facts:
        mapping.setdefault((kind, pid), []).append(fact)
    return {key: tuple(values) for key, values in mapping.items()}


def _segment_fact_text(values: tuple[str, ...]) -> str:
    return " & ".join(values) if len(values) > 1 else (values[0] if values else "无")


def _segment_key_list(facts: dict[tuple[str, int | None], tuple[str, ...]],
                      keys: Iterable[tuple[str, int | None]]) -> str:
    text = ", ".join(
        f"{_segment_key_text(key)}[{_segment_fact_text(facts[key])}]"
        for key in sorted(keys, key=str)
    )
    return _bounded_detail(text or "无", _SEGMENT_FACT_WIDTH)


@dataclass(frozen=True)
class SegmentTrackSubset:
    """后续段是首段轨集合的纯子集的事实（无增轨、无漂移）。

    盘上真实结构：终段/尾段 clip 自身少声明若干字幕或音轨 PID，母盘的原生
    播放器在该区间同样没有这些轨。成品因此在该区间不含该轨，这既不是封装
    缺陷也不必失败关闭——但必须作为显式事实留痕，否则"缺少一条轨"的成品
    与"管线和首段对齐"的成品无法区分。
    """

    label: str
    missing: tuple[tuple[str, int | None], ...]
    facts: tuple[tuple[str, int | None, str], ...]

    def detail(self) -> str:
        """单行事实（可再入失败/日志详情，命名段、play item、clip 与区间）。

        ``facts`` 取自**首段**识别结果：缺失轨只在首段有可比事实（本段没有
        该轨），这是该差异唯一可复核的来源。
        """
        return (
            f"{self.label} 轨道为首段的纯子集：缺少 "
            f"{_segment_key_list(_segment_fact_map(self.facts), self.missing)}；"
            "成品在该区间不含这些轨（与母盘原生播放一致），按子集放行"
        )


def require_segment_track_correspondence(
    first: tuple[tuple[str, int | None, str], ...],
    other: tuple[tuple[str, int | None, str], ...],
    *,
    label: str,
) -> SegmentTrackSubset | None:
    """逐段核对音视频/字幕轨与首段一一对应（追加前唯一的机会）。

    mkvmerge 的追加按首段轨布局续接：某段缺一条轨时既不报错也不补静默流
    （实测 v92：无音轨的第二段被追加后，成品音轨在拼接处截断且退出码为 0），
    因此"命令成功"不能证明拼接正确，必须在追加前逐段比对。

    - 新增轨、事实漂移（codec/语言/通道数等）、视频分歧一律失败关闭；
    - 纯子集缺失（后段少声明若干音轨/字幕轨，无新增、无漂移）返回
      ``SegmentTrackSubset`` 由调用方记录（2026-09-14 用户裁定：终段
      少声明字幕 PID 是母盘结构，失败关闭会让该标题永远不可交付）；
    - 无法归入纯子集的差异（同 PID 轨的省略/类型变化等）失败关闭。
    """
    first_by_key = _segment_fact_map(first)
    other_by_key = _segment_fact_map(other)
    missing = [key for key in first_by_key if key not in other_by_key]
    added = [key for key in other_by_key if key not in first_by_key]
    if added or (missing and not _segment_missing_is_type_prefix(first_by_key, other_by_key)):
        raise MatroskaBuildError(
            f"{label} 轨道与首段不对应：缺少 {_segment_key_list(first_by_key, missing)}；"
            f"新增 {_segment_key_list(other_by_key, added)}"
        )
    mismatched = sorted(
        (key for key in other_by_key if key in first_by_key
         and first_by_key[key] != other_by_key[key]),
        key=str,
    )
    if mismatched:
        detail = "；".join(
            f"{_segment_key_text(key)} 首段[{_segment_fact_text(first_by_key[key])}]"
            f"≠本段[{_segment_fact_text(other_by_key[key])}]"
            for key in mismatched[:_SEGMENT_FACT_LIMIT]
        )
        if len(mismatched) > _SEGMENT_FACT_LIMIT:
            detail += f"…(+{len(mismatched) - _SEGMENT_FACT_LIMIT})"
        raise MatroskaBuildError(
            f"{label} 轨道事实与首段不一致：{_bounded_detail(detail, _SEGMENT_FACT_WIDTH)}"
        )
    if missing:
        return SegmentTrackSubset(label=label, missing=tuple(missing), facts=first)
    return None


def _segment_missing_is_type_prefix(
    first_by_key: dict[tuple[str, int | None], tuple[str, ...]],
    other_by_key: dict[tuple[str, int | None], tuple[str, ...]],
) -> bool:
    """缺失是否让本段的每一类轨序列都仍是首段同类序列的**前缀**。

    2026-09-15：追加段的逐文件选项无法跨文件复用轨编号（mkvmerge 的
    --audio-tracks/--subtitle-tracks 是该输入文件的局部编号，引用不存在的编号
    直接报错退出），因此追加段按**类型 + 顺序**续接到首段的同类轨上。只有
    "每类序列都是首段同类序列的前缀"时，这条映射才与逐段核对结论一致：
    中段缺一条会让其后的同类轨整体前移——那是静默错接，必须失败关闭
    （同 PID 换成别的表达同样落到这里：序列不再相等）。

    纯子集缺失（末尾少声明若干音轨/字幕轨）仍按事实放行——母盘终段少声明
    PID 是真实结构（2026-09-14 用户裁定：失败关闭会让该标题永远不可交付）。
    """
    by_kind_first: dict[str, list[int | None]] = {}
    by_kind_other: dict[str, list[int | None]] = {}
    for kind, pid in first_by_key:
        by_kind_first.setdefault(kind, []).append(pid)
    for kind, pid in other_by_key:
        by_kind_other.setdefault(kind, []).append(pid)
    for kind, sequence in by_kind_other.items():
        head = by_kind_first.get(kind)
        if head is None or sequence != head[:len(sequence)]:
            return False
    return True


def _primary_video_facts(streams: list[dict] | None) -> dict:
    """首条视频流的可比事实（编码/档次/分辨率/帧率/时间基）。

    缺视频流即失败关闭：追加段没有视频就没有可续接的时间轴。
    """
    videos = [stream for stream in (streams or ()) if stream.get("codec_type") == "video"]
    if not videos:
        raise MatroskaBuildError("分段缺少视频流，无法核对拼接对应关系")
    primary = videos[0]
    return {key: primary.get(key) for key in _SEGMENT_VIDEO_FACT_KEYS}


def require_segment_video_correspondence(first: dict, other: dict, *, label: str) -> None:
    """逐段核对视频事实（mkvmerge 识别不提供档次/帧率/时间基，用 ffprobe 事实补齐）。"""
    if first == other:
        return
    detail = "；".join(
        f"{key} 首段[{first.get(key)}]≠本段[{other.get(key)}]"
        for key in _SEGMENT_VIDEO_FACT_KEYS
        if first.get(key) != other.get(key)
    )
    raise MatroskaBuildError(
        f"{label} 视频事实与首段不一致：{_bounded_detail(detail, _SEGMENT_FACT_WIDTH)}"
    )


class BuildArtifacts:
    """一次构建的精确工件路径（BuildIdentity 的一部分）。

    BATCH-006 R2 P0：所有文件名推导集中在唯一入口，representation 与
    MatroskaBuilder 共用——禁止两边手写文件名（此前 staging 路径在
    service.py 与 matroska.py 各写一次导致不一致）。``output`` 是
    generation 化的 partial 路径（main-title.m2ts.partial.N）。
    """

    def __init__(self, output: Path, segment_count: int = 0):
        self.output: Path = Path(output)
        self.partial: Path = self.output
        self.source: Path = self.output.with_name(self.output.name + ".source.m2ts")
        self.staging: Path = self.output.with_name("main-title.materializing")
        self.log: Path = self.output.with_name(f"build-{self._generation_suffix()}.log")
        # 多段合辑：每个 play item 一个独立分段文件（按 MPLS 顺序），与它
        # 自己的 staging。单段标题为空——路径集合与既有行为完全一致。
        count = max(0, int(segment_count))
        self.segments: tuple[Path, ...] = tuple(
            self.output.with_name(f"{self.output.name}{_ITEM_SEGMENT_MARKER}{index}.m2ts")
            for index in range(count)
        )
        self.staging_segments: tuple[Path, ...] = tuple(
            self.output.with_name(f"main-title.materializing.item{index}")
            for index in range(count)
        )

    def _generation_suffix(self) -> str:
        name = self.output.name
        # main-title.m2ts.partial.N -> N
        marker = ".partial."
        if marker in name:
            return name.split(marker, 1)[1]
        return "1"

    def as_artifacts_list(self) -> list[str]:
        """精确工件清单（供 pending 持久化与失效清理）。

        多段合辑的每一段（及段级 staging）都在清单内：清理从不 glob，
        未登记的段文件会永久泄漏。
        """
        return [
            str(self.partial),
            str(self.source),
            str(self.staging),
            str(self.log),
            *(str(path) for path in self.staging_segments),
            *(str(path) for path in self.segments),
        ]

    def checkpoint_paths(self) -> tuple[Path, ...]:
        """物化检查点（可跨重试复用）；发布或放弃收口后必须逐段删除。"""
        return (self.source, *self.segments)

    def owning_directory(self) -> Path:
        return self.output.parent


def build_artifacts(output: Path, segment_count: int = 0) -> BuildArtifacts:
    """唯一构建工件计算入口（representation 与 MatroskaBuilder 共用）。

    ``segment_count`` = 多 play item 主片的段数（单段标题为 0，清单与既有
    单文件路线逐字一致）。
    """
    return BuildArtifacts(output, segment_count=segment_count)


def seam_preserved_path(output: Path) -> Path:
    """拼接门失败时保留的完整成品路径（与 partial 同目录，带 generation）。

    与 partial 名同源推导：失败详情里点名的路径与沙盒里实际留下的字节文件
    必须是同一个，否则复检无法从日志定位工件。
    """
    output = Path(output)
    return output.with_name(f"{output.name}.seam-failure.mkv")


_ITEM_SEGMENT_MARKER = ".item"
_ITEM_SEGMENT_SUFFIX = ".m2ts"


def is_checkpoint_artifact(name: str) -> bool:
    """物化检查点文件名判定（recover 保留、发布后删除）。

    单文件路线为 ``*.source.m2ts``，多段合辑为 ``*.item{k}.m2ts``；两种都
    是"完整物化结果可跨重试复用"的同一类工件。
    """
    if name.endswith(".source.m2ts"):
        return True
    if not name.endswith(_ITEM_SEGMENT_SUFFIX):
        return False
    stem = name[: -len(_ITEM_SEGMENT_SUFFIX)]
    marker = stem.rfind(_ITEM_SEGMENT_MARKER)
    if marker < 0:
        return False
    return stem[marker + len(_ITEM_SEGMENT_MARKER):].isdigit()


def planned_segment_count(asset: BlurayTitleAsset) -> int:
    """多 play item 主片（连放/无缝分支）的段数；单段标题返回 0。

    仅两个及以上 play item 才拆分：单段标题保持既有单文件路线（99% 库），
    不得因本次改动改变其命令、文件与门禁行为。
    """
    return len(asset.clips) if len(asset.clips) > 1 else 0


def multiclip_item_bounds(asset: BlurayTitleAsset) -> tuple[tuple[int, int], ...]:
    """多 play item 的 (start, length)（MPLS 顺序）；单段标题返回空元组。

    边界与 asset.clips 数量不一致说明 reader 与 asset 不是同一份标题状态，
    继续按段物化会把字节错位落到成品里，必须失败关闭。
    """
    count = planned_segment_count(asset)
    if count < 2:
        return ()
    try:
        bounds = bluray_item_bounds(asset)
    except BlurayTitleError as error:
        raise MatroskaBuildError(
            f"多段合辑主片的 play item 边界不可用：{error}"
        ) from error
    if len(bounds) != count:
        raise MatroskaBuildError(
            f"多段合辑主片 play item 数量不一致：reader 边界 {len(bounds)} 段，"
            f"MPLS 段列表 {count} 段，拒绝按段物化"
        )
    return bounds


_SPACE_MARGIN_BYTES = 512 * 1024**2


def default_peak_space_bytes(asset: BlurayTitleAsset) -> int:
    """默认封装路线（分段物化 + mkvmerge 追加）的临时空间峰值上界。

    同一槽位同时存活的项（相对主片虚拟流字节 = Σ play item 长度，同一 clip
    被引用 K 次即计 K 份；准入阶段拿不到逐段长度，在写分段以整片为上界）：
    - 全部分段物化文件 1.0；
    - 物化期正在写的那一段 1.0（分段各自 staging，原子 rename）；
    - 正在生成的完整成品 ≤1.0；
    - 同一槽位保留的既有成品 1.0（交付侧保留到末次传输后 15 分钟）。
    DV/HDR10+ 路线的层文件峰值仍落在同一保守上界内（单段标题下与既有
    4× 语义逐字一致，PMT-only DV 无需二次准入）。
    """
    stream_bytes = int(asset.size)
    segments = stream_bytes
    in_flight_segment = stream_bytes
    muxed_output = stream_bytes
    retained_representation = stream_bytes
    return (
        segments + in_flight_segment + muxed_output + retained_representation
        + _SPACE_MARGIN_BYTES
    )


_MULTICLIP_LOG_CLIPS = 5
_MULTICLIP_LOG_WIDTH = 320


def describe_multiclip_compilation(asset: BlurayTitleAsset) -> str | None:
    """多剪辑合辑（Play All）主片的可解释摘要；非该结构返回 None。

    各 play item 的 in_time 非零表示成品时间轴按素材内部起点拼接（2026-09-14
    生产交付案例：3 段均 in_time=600s，seek 期望与逐段自身时间轴不重叠，90%
    目标落点越界）。本函数只记录逐段 in/out 事实，不改写时间轴、不拒绝该结构。
    """
    clips = sorted(asset.clips, key=lambda clip: clip.index)
    if len(clips) < 2 or not any(clip.in_time != 0 for clip in clips):
        return None
    facts = [
        f"#{clip.index} {clip.clip_id} "
        f"in={clip.in_time / 90000:.1f}s out={clip.out_time / 90000:.1f}s"
        for clip in clips[:_MULTICLIP_LOG_CLIPS]
    ]
    if len(clips) > _MULTICLIP_LOG_CLIPS:
        facts.append(f"…(+{len(clips) - _MULTICLIP_LOG_CLIPS})")
    return _bounded_detail(
        f"多段合辑主片 playlist={asset.playlist} clip_count={asset.clip_count}"
        f"（play item in_time 非零，成品时间轴按各段内部起点拼接，seek 期望不重叠）："
        + "；".join(facts),
        _MULTICLIP_LOG_WIDTH,
    )


def _multiclip_facts(asset: BlurayTitleAsset) -> str:
    """多 clip 合辑事实（无「本管线不重基」附注），供拼接门失败详情复用。"""
    return describe_multiclip_compilation(asset) or ""


def _with_multiclip_context(message: str, asset: BlurayTitleAsset) -> str:
    """给失败理由补上多 clip 合辑事实，使保守拒绝可自解释。

    2026-09-14 用户裁定：连放结构按保守做法处理——门禁照旧拒绝、不做时间轴
    重基，但运维不能只看到「seek 越界」而判断不出是来源缺陷还是管线策略。
    """
    compilation = describe_multiclip_compilation(asset)
    if not compilation:
        return message
    return (
        f"{message}｜主片为多 clip 合辑（连放结构）：{compilation}"
        "；本管线不为主片做时间轴重基，如需该结构请按单集来源投递"
    )


def _source_damage_for(asset: BlurayTitleAsset) -> list:
    """本素材已修补的源损伤清单（经 bluray 的只读访问器；失败即空表）。"""
    try:
        from .bluray import source_damage_records
        return list(source_damage_records(asset))
    except Exception:
        return []


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

        多段合辑按 play item 各自物化（N 个分段文件 + 单段 staging），
        普通路线峰值明细见 default_peak_space_bytes；DV（含 PMT-only，
        物化后才可识别）的层文件（BL ≈0.82 + 合并 DV ≈0.82）与 HDR10+
        的抽取/处理后视频（≈0.82 + 0.82）峰值仍由该保守上界覆盖，
        PMT-only DV 无需二次准入（design P1-2）。
        """
        return default_peak_space_bytes(asset)

    def _identify_path(self, source: Path, executable: str | None = None) -> dict:
        tool = executable or self.executable
        # 退出码 1 = mkvmerge 契约里的"有警告"，输出仍然有效：识别结果照常解析，
        # 警告留痕（2026-09-15 越南战争：混格式追加的成品识别曾以非零码返回，
        # check=True 把它当失败，成品因此永远出不了门）。退出码 2=错误，见
        # _run_mkvmerge_identify（2026-09-17 按 man 语义收口）。
        completed = self._run_mkvmerge_identify(tool, source, label="mkvmerge 主片识别")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise MatroskaBuildError(f"mkvmerge 主片识别返回无效 JSON：{error}") from error

    @staticmethod
    def _run_mkvmerge_identify(tool: str, source: Path, *, label: str,
                               timeout: int = 300):
        """跑一次 mkvmerge --identify。

        退出码按 man mkvmerge：0=成功、1=有警告（输出有效）、2=错误。
        2026-09-17 修正：此前"接受 0/2"（把 2 当警告）。对**未产出/打不开**
        的成品，mkvmerge 会把错误写进 stdout 的 JSON（`{"errors": [...]}`，
        无 container）并以退出码 2 返回；旧实现因此把这份错误 JSON 当成有效
        识别继续，直到成品门报「最终 MKV 缺少有效时长/Cues 信息」——真实封装
        失败被伪装成内容门失败。现在 2 一律失败关闭，并带上 stdout/stderr 原文。
        """
        command = [tool, "--identification-format", "json", "--identify", str(source)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            raise _tool_failure(label, error, limit=500) from error
        code = getattr(completed, "returncode", 0)
        stderr = (getattr(completed, "stderr", "") or "").strip()
        stdout = (getattr(completed, "stdout", "") or "").strip()
        if code not in (0, 1):
            detail = _bounded_detail(stderr or stdout, 300)
            raise MatroskaBuildError(
                f"{label}失败（退出码 {code}）：{detail or '（无输出文本）'}")
        if code == 1:
            logger.warning("%s：mkvmerge 退出码 1（有警告，输出有效）%s", label,
                           _bounded_detail(stderr, 300) or "（无输出文本）")
        return completed

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
            seconds = _timeout_seconds(error)
            if seconds is not None:
                raise TransientMatroskaBuildError(
                    f"主片视频轨探测超时（>{seconds:.0f}s），按瞬时故障重试"
                ) from error
            raise MatroskaBuildError(
                "主片视频轨探测失败，无法判断 Dolby Vision："
                f"{_bounded_detail(_process_error_detail(error))}"
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

        2026-09-14（霸王别姬 DIY 重制盘实测）：MPLS 官方视频 PID 可能与真正
        携带画面的 PID 不一致——官方表记 0x1011，而该 PID 的流没有宽高，
        真实的 3840x2160 在 0x1fff。此时按官方 PID 抽取裸 HEVC 只会得到无
        参数集的空流（ffmpeg: dimensions not set，退出码 234）。这种来源不是
        本管线能无损处理的，必须给出点名两个 PID 的可读拒绝，而不是把它当作
        编码器报错。
        """
        pid = self._official_video_pid(asset, streams)
        self._require_video_pid_carries_picture(pid, streams)
        return pid

    def _official_video_pid(self, asset: BlurayTitleAsset, streams: list[dict]) -> int:
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

    @staticmethod
    def _require_video_pid_carries_picture(pid: int, streams: list[dict]) -> None:
        """官方视频 PID 必须真的带画面；否则点名真正的 PID 拒绝。

        仅当官方 PID 的流缺失宽高、且另有唯一一条带宽高的视频流时才报这种
        "来源视频 PID 与 MPLS 表不一致"。宽高缺失也可能是探测本身失败，所以
        没有替代候选时保持原行为，不在这里扩张判定。
        """
        def resolve(raw: object) -> int | None:
            text = str(raw or "")
            try:
                return int(text, 16)
            except ValueError:
                return None

        videos = [
            stream for stream in streams
            if stream.get("codec_type") == "video"
        ]
        official = next((s for s in videos if resolve(s.get("id")) == pid), None)
        if official is None:
            return
        if int(official.get("width") or 0) > 0 and int(official.get("height") or 0) > 0:
            return
        carriers = [
            s for s in videos
            if int(s.get("width") or 0) > 0 and int(s.get("height") or 0) > 0
        ]
        if len(carriers) != 1:
            return
        real = resolve(carriers[0].get("id"))
        raise MatroskaBuildError(
            f"来源视频 PID 与 MPLS 官方表不一致：官方 PID 0x{pid:x} 的流没有画面"
            f"（宽高为空），真正的 {carriers[0].get('width')}x"
            f"{carriers[0].get('height')} 在 PID 0x{real:x}。"
            f"该来源为非标准重制盘，本管线无法按官方 PID 无损处理，请更换来源。"
        )

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

    # ---- 多段合辑的段级 DV 构建（2026-09-24 用户裁定「这条也能做」） ----

    def _stream_pid_index(self, source: Path) -> dict[int, list[int]]:
        """ffprobe：PID → 该 PID 的 ffmpeg 流索引（升序；同一 PID 可有多条流）。

        BDMV 的 TrueHD 会把内嵌 AC-3 core 作为**同一 PID 的第二条基本流**（ISO 的
        PMT 里 0x1100 出现两次即此形态），ffmpeg 因此把它暴露成两条流；按 PID
        取值时必须把整组一起带上，否则交付会丢 fallback 音轨。
        """
        try:
            completed = subprocess.run(
                [self.probe, "-v", "error", "-show_entries", "stream=index,id",
                 "-of", "json", str(source)],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise _tool_failure("流表探测", error) from error
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as error:
            raise MatroskaBuildError(f"ffprobe 返回无效 JSON：{error}") from error
        index: dict[int, list[int]] = {}
        for stream in payload.get("streams") or ():
            raw = stream.get("id")
            try:
                pid = int(str(raw), 0)
            except (TypeError, ValueError):
                continue
            index.setdefault(pid, []).append(int(stream.get("index") or 0))
        for values in index.values():
            values.sort()
        return index

    @staticmethod
    def _official_stream_plan(asset: BlurayTitleAsset, pid_index: dict[int, list[int]],
                              kind: str) -> tuple[list[int], list, int]:
        """官方 MPLS 表 → 该段实际携带的 ffmpeg 流索引 + 命中的官方条目 + 略过数。

        **每个官方条目只取该 PID 的第一条流**。为什么必须略过（2026-09-24 生产实测）：
        成品门 `validate_finalized_identification` 要求「成品音轨数 == 可映射官方条目
        数」。同 PID 的第二条流（TrueHD 的内嵌 AC-3 core）若单独成轨，成品就是 9 条
        而官方只有 8 条 ⇒ 该片被判「音轨数量与可映射 MPLS 音轨不一致」而失败关闭
        （`dlv_5105a3a361c39b28d76f` 的实际报错）。core 已由 TrueHD 轨自身承载
        （官方表也只声明一条），故不另立轨。

        返回 ``(indices, official_entries, skipped)``；``indices`` 与该段中间件的轨序
        一一对应（mkvmerge 按位置取轨）。
        """
        official = asset.audio_tracks if kind == "audio" else asset.subtitle_tracks
        indices: list[int] = []
        entries: list = []
        skipped = 0
        for entry in official:
            streams = pid_index.get(int(entry.pid))
            if not streams:
                continue
            indices.append(streams[0])
            entries.append(entry)
            skipped += max(0, len(streams) - 1)
        return indices, entries, skipped

    def _extract_official_intermediate(self, source: Path, indices: list[int],
                                       target: Path, label: str) -> None:
        """把该段的官方流重封成 **matroska 中间件**（mkvmerge 读得动）。

        为什么走 ffmpeg 中间件：这类 DIY 盘的 TS，**mkvmerge 读不到音频**（`mkvmerge
        -J` 只有视频 + PGS，而 ffprobe/ffmpeg 能读到 8 条音轨、字节层 PID 亦在），
        管线又要求「没有可映射音轨即失败关闭」。ffmpeg 写出的容器它读得动，于是两个
        工具各用自己读得动的部分：ffmpeg 取流、mkvmerge 封装 + 按官方表注入语言与
        默认轨。轨序 = 官方条目顺序（`resolved` 由该顺序构造，不依赖 mkvmerge 在
        原盘 TS 上的（读不到的）识别结果）。

        **容器选 matroska 而不是 mpegts**：实测只含 PGS 的 TS，mkvmerge 读出来是
        0 轨（`mkvmerge -J subs.ts → tracks: []`），字幕会被静默丢光；matroska
        中间件则逐轨可读（首版实现即如此）。
        """
        if not indices:
            return
        command = [self.ffmpeg, "-v", "error", "-y", "-i", str(source)]
        for index in indices:
            command += ["-map", f"0:{index}"]
        command += ["-c", "copy", "-f", "matroska", str(target)]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True,
                           timeout=3600)
        except (OSError, subprocess.SubprocessError) as error:
            raise _tool_failure(f"{label} 中间件重封", error) from error
        if not target.is_file() or target.stat().st_size <= 0:
            raise MatroskaBuildError(f"{label} 中间件为空（{target.name}），失败关闭")

    def _dv_segment(self, asset: BlurayTitleAsset, source: Path, target: Path,
                    progress: Callable[[int, str], None] | None,
                    *, label: str, layout: dict | None) -> dict:
        """段级 DV：抽 BL/EL → dovi_tool 合并 → ffmpeg 官方音轨/字幕中间件 → 段级封装。

        返回 ``{"path", "evidence", "resolved", "official_audio", "official_subtitle"}``。
        ``layout`` 为首段确立的**官方轨集合**（后续段必须一致：追加要求轨布局相同，
        不一致即失败关闭，绝不拼出错的成品）。
        """
        dovi_executable = self._require_dovi_tool()
        streams = self._video_streams(source)
        base_pid, enhancement_pid = self._dv_video_pids(asset, streams)
        base_path = source.with_name(source.name + ".bl.hevc")
        enhancement_path = source.with_name(source.name + ".el.hevc")
        merged_video = source.with_name(source.name + ".dv.hevc")
        self._extract_dv_layers(source, base_pid, enhancement_pid, progress,
                                base_path, enhancement_path)
        self._merge_dv_layers(dovi_executable, base_path, enhancement_path,
                              merged_video, progress)

        pid_index = self._stream_pid_index(source)
        audio_indices, audio_official, audio_skipped = self._official_stream_plan(
            asset, pid_index, "audio")
        sub_indices, sub_official, sub_skipped = self._official_stream_plan(
            asset, pid_index, "subtitle")
        if not audio_indices:
            raise MatroskaBuildError(
                f"多段 DV {label} 段内找不到任何官方音轨（PID 表与段 PMT 无交集），"
                "失败关闭（不出静音片段）")
        official_audio = tuple(int(entry.index) for entry in audio_official)
        official_subtitle = tuple(int(entry.index) for entry in sub_official)
        if layout is not None and (official_audio != layout["audio"]
                                   or official_subtitle != layout["subtitle"]):
            raise MatroskaBuildError(
                f"多段 DV {label} 段官方轨集合与首段不一致（追加需要同布局）："
                f"首段 audio={layout['audio']}/subs={layout['subtitle']}，"
                f"本段 audio={official_audio}/subs={official_subtitle}，失败关闭")

        audio_ts = source.with_name(source.name + ".dv-audio.mka")
        subtitle_ts = source.with_name(source.name + ".dv-subs.mka")
        self._extract_official_intermediate(source, audio_indices, audio_ts,
                                            f"{label} 音轨")
        self._extract_official_intermediate(source, sub_indices, subtitle_ts,
                                            f"{label} 字幕")

        # 官方映射由**实测 PID 表**直接构造：mkvmerge 在这类盘上读不到原文流，
        # 但中间件是我们自己按官方 PID 重封的，轨序 = 官方条目顺序，映射确定。
        resolved = ResolvedTracks(
            video_ids=(0,),
            audio_ids=tuple(range(len(audio_official))),
            subtitle_ids=tuple(range(len(sub_official))),
            audio_by_official_index=tuple(
                (int(entry.index), position) for position, entry in enumerate(audio_official)),
            subtitle_by_official_index=tuple(
                (int(entry.index), position) for position, entry in enumerate(sub_official)),
        )
        segment_command = build_dv_segment_command(
            asset, merged_video, audio_ts, subtitle_ts, target,
            title=f"{label}（DV 段级）", resolved=resolved)
        completed = subprocess.run(segment_command, capture_output=True, text=True)
        if completed.returncode not in (0, 1):
            raise MatroskaBuildError(
                f"多段 DV 段级封装失败（{label}，mkvmerge rc={completed.returncode}）："
                f"{(completed.stderr or '')[-300:]}")
        if not target.is_file() or target.stat().st_size <= 0:
            raise MatroskaBuildError(f"多段 DV 段级产物为空（{label}）")

        el_digests, el_stats = _el_vcl_parse(enhancement_path)
        rpu_evidence = self._capture_merged_rpu_evidence(
            dovi_executable, merged_video, source.with_name(source.name + ".dv-rpu.bin"))
        evidence = {
            "label": label,
            "dovi_executable": dovi_executable,
            "bl_size": int(base_path.stat().st_size),
            "merged_size": int(merged_video.stat().st_size),
            "el_digests": el_digests,
            "el_stats": el_stats,
            "merged_rpu_evidence": rpu_evidence,
            "fps": _probe_video_fps(self.probe, source),
            "duration_s": _probe_duration(self.probe, source),
            "audio_tracks": len(audio_official),
            "subtitle_tracks": len(sub_official),
            "official_audio_indices": list(official_audio),
            "official_subtitle_indices": list(official_subtitle),
            "skipped_same_pid_streams": int(audio_skipped + sub_skipped),
            "source_pids": {"base": base_pid, "enhancement": enhancement_pid},
        }
        for path in (base_path, enhancement_path, merged_video, audio_ts, subtitle_ts):
            path.unlink(missing_ok=True)
        return {"path": target, "evidence": evidence, "resolved": resolved,
                "official_audio": official_audio, "official_subtitle": official_subtitle}

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
            raise _tool_failure("HDR10+ 探测", error) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise MatroskaBuildError(f"ffprobe 返回无效 JSON：{error}") from error
        for frame in payload.get("frames") or []:
            for side in frame.get("side_data_list") or []:
                if "User Data Unregistered SEI" in str(side.get("side_data_type", "")):
                    return True
        return False

    def _verify_seekable(self, output: Path, dv_facts: dict | None = None,
                         *, seam_targets: Sequence[float] = ()) -> None:
        """可重复的 seek/Cues 自动证据（round-2 P1-4）。

        BATCH-006：DV Profile 7 成品必须传 dv_facts（含 dovi_executable）
        让 seek 校验走 DV 正交路线（stream-copy + BL 解码），否则合并流被
        通用 decoder 判"有包无帧"误拒（劳伦斯 part-2 实测 15min59s 封包后
        在 10% seek 失败）。

        ``seam_targets``：多段合辑的内部拼接点（秒），与 10/50/90% 走同一套
        门（同一容差、同一 DV/非 DV 分支），不另立宽松标准。
        """
        duration = _probe_duration(self.probe, output)
        if duration <= 0:
            raise MatroskaBuildError("成品时长无效，无法验证 seek")
        diagnostics: dict = {}
        try:
            _verify_cues_and_seek(
                self.probe, output, int(duration * 1_000_000_000),
                diagnostics=diagnostics,
                dv_facts=dv_facts, extra_targets=tuple(seam_targets),
            )
        except MatroskaBuildError as error:
            if not getattr(error, "seek_diagnostics", None):
                # 失败详情要能在拼接门里自诊断：测量事实随异常对象带回
                # （不改判定；单文件路线同样受益）。
                error.seek_diagnostics = diagnostics
            raise



    def _verify_dolby_vision(self, output: Path) -> dict:
        """Refuse a finalized MKV whose video track lost the DV enhancement layer."""
        try:
            completed = subprocess.run(
                [self.probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream_side_data", "-of", "json", str(output)],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise _tool_failure("Dolby Vision 核验", error) from error
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
                # 规格 D4（2026-09-16）：reader 的**瞬态**故障（子进程中断/通信
                # 失败/无返回结果）按可重试处理，与 m2ts 路线对称；**源确定性**
                # 故障（reader 报读取失败、洞、全空包）保持终态——重试无意义。
                try:
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
                except TransientBlurayReadError as error:
                    raise TransientMatroskaBuildError(
                        f"主片物化时 reader 瞬态故障，按瞬时故障重试：{error}"
                    ) from error
            # 原子发布：临时文件写完后 rename 到 source（读者只看到完整文件）
            os.replace(staging, source)
            _log.debug(
                "materialize done inode=%s written_gb=%.1f path=%s",
                locals().get("_ino", "?"), written / 1024**3, source,
            )
            # 规格 D3（证据面，最小面）：本次物化若修补过源损伤，把它**显式**
            # 推到交付进度上——"明知有瑕疵仍发布"必须是可审计的，不能静默。
            # 逐条事实（clip/偏移/包数/丢失毫秒）另经 bluray.source_damage_records
            # 暴露，供交付 manifest 消费（manifest 字段待接线）。
            damages = _source_damage_for(asset)
            if damages:
                lost = sum(float(d.lost_millis) for d in damages)
                summary = "；".join(
                    f"clip={d.clip_id} offset={d.byte_offset} 包数={d.packet_count} "
                    f"丢失={d.lost_millis:.1f}ms" for d in damages)
                _log.warning("源损伤已修补 %d 处（合计 %.1f ms）：%s",
                             len(damages), lost, summary)
                if progress:
                    progress(41, f"源损伤已就地修补 {len(damages)} 处"
                                 f"（合计 {lost:.1f} ms，长度不变）")
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
        compilation = describe_multiclip_compilation(asset)
        if compilation:
            logger.warning("%s", compilation)
        if self._source_checkpoint_is_valid(asset, source):
            if progress:
                progress(40, "复用已完成的 MPLS 主片检查点")
            return True
        # Never append to an interrupted transport stream.
        source.unlink(missing_ok=True)
        self._materialize_title(asset, source, progress=progress, staging=staging)
        return False

    @staticmethod
    def _segment_checkpoint_is_valid(expected_bytes: int, segment: Path) -> bool:
        """分段检查点：只有逐段字节数精确等于该 play item 长度才可复用。

        截断/缺失分段绝不续用（追加进成品的是错位时间轴），由调用方删除后
        重新物化；这与单文件路线的 source 检查点语义一致。
        """
        try:
            return segment.is_file() and segment.stat().st_size == int(expected_bytes)
        except OSError:
            return False

    @staticmethod
    def _materialize_item(
        asset: BlurayTitleAsset,
        item_index: int,
        target: Path,
        staging: Path,
        *,
        item_bytes: int,
        base_bytes: int,
        total_bytes: int,
        progress: Callable[[int, str], None] | None = None,
    ) -> None:
        """物化单个 play item：写段级 staging 后原子 rename（读者只看到完整段）。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        total = max(1, int(total_bytes))
        item_total = max(1, int(item_bytes))
        written = 0
        last_report = 0.0
        staging.unlink(missing_ok=True)
        try:
            with staging.open("wb", buffering=1024 * 1024) as stream:
                for chunk in iter_bluray_item(asset, item_index, chunk_size=1024 * 1024):
                    stream.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if progress and (now - last_report >= 0.5 or written >= item_total):
                        last_report = now
                        # 前 40% 分给逐段提取（与单文件路线同一进度语义）。
                        percent = min(40, int((base_bytes + written) * 40 / total))
                        progress(
                            percent,
                            f"正在提取 MPLS 主片第 {item_index + 1} 段："
                            f"{(base_bytes + written) / 1024**3:.1f} / "
                            f"{total / 1024**3:.1f} GB",
                        )
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)

    _SEGMENT_SUBSET_LOG_LIMIT = 3
    _SEGMENT_SUBSET_LOG_WIDTH = 300
    # 分段末端"溢出"容差：超过该秒数即裁到 play item 标称时长（见 _trim_segment）。
    # 帧间隔约 0.033~0.042s，取 0.05s 不会误裁正常末帧。
    _SEGMENT_TAIL_OVERHANG_TOLERANCE = 0.05

    def _prepare_items(
        self,
        asset: BlurayTitleAsset,
        bounds: tuple[tuple[int, int], ...],
        artifacts: BuildArtifacts,
        *,
        progress: Callable[[int, str], None] | None = None,
    ) -> tuple[dict, tuple[str, ...], tuple[int, ...], tuple[Path, ...], float,
               list[dict], object]:
        """逐段物化→识别→核对，返回（首段识别结果，子集差异事实，尾部排除项，
        参与封装的段文件，末端溢出秒数，逐段 DV 证据[非 DV 时为空]）。

        边物化边核对：纯子集缺失（后段少声明若干音轨/字幕轨）按事实放行；
        轨道**新增/漂移**（同 PID 换声道/编码/语言等）不得并入同一轨——
        mkvmerge 追加只能映射到首文件已存在的目标轨（目标轨属性由首文件
        决定），强行并入就是把后段内容塞进不匹配的轨（坏音/坏画面）。
        这类段**只允许出现在尾部**并被显式排除（见 _trailing_exclusions）；
        中段出现即失败关闭——跳过中段等于成品连续性被破坏，那是内容损坏
        而不是结构差异。每段路径在写入前已由 BuildArtifacts 登记。
        多段合辑不能走的路线（HDR10+ 剥离）在逐段阶段点名拒绝——不能
        退回字节拼接，那正是本路线要修的缺陷。DV 走段级 BL+EL 合并后再追加
        （见 _dv_segment 与 build_dv_segment_command）。
        """
        compilation = describe_multiclip_compilation(asset)
        if compilation:
            logger.warning("%s", compilation)
        clips = sorted(asset.clips, key=lambda clip: clip.index)
        total_bytes = sum(length for _, length in bounds)
        first_payload: dict | None = None
        first_tracks: tuple[tuple[str, int | None, str], ...] | None = None
        first_video: dict | None = None
        variances: list[str] = []
        # index -> 轨道不对应事实（视频分歧或同 PID 换声道/编码/语言的漂移）。
        violations: dict[int, str] = {}
        # 逐段"是否与首段对应"（首段恒为 True），用于判定违规段是否在尾部。
        corresponding: list[bool] = []
        # 用于**封装**的段路径（与 artifacts.segments 同序）：末端越过切点的段
        # 会被裁到标称时长后替换，避免接缝留洞（见 _trim_segment）。多段 DV 时
        # 这里换成**段级自洽的 DV MKV**（BL+EL 已合并、官方音轨/字幕已入段）。
        mux_segments: list[Path] = []
        # 逐段实测的末端溢出合计（秒）：接入时长期望（见 _verify_multiclip_seams）。
        expected_overhang = 0.0
        # 多段 DV（2026-09-24 用户裁定「这条也能做」）：首段识别出 DV 后启用，
        # 逐段构建 DV 段并采集证据；非 DV 时保持既有纯追加路线（行为逐字不变）。
        dv_enabled = False
        dv_layout: dict | None = None
        dv_evidence: list[dict] = []
        # 首段实测得到的官方轨映射（成品门要按它校验数量/语言，见 build()）。
        dv_resolved = None
        written = 0
        for index, (_, length) in enumerate(bounds):
            segment = artifacts.segments[index]
            label = f"第 {index + 1}/{len(bounds)} 段（play item {index}）"
            if self._segment_checkpoint_is_valid(length, segment):
                if progress:
                    progress(min(40, int(written * 40 / max(1, total_bytes))),
                             f"复用已完成的分段 {index + 1}/{len(bounds)}")
            else:
                # 截断/缺失分段绝不续用（错位字节会进成品）。
                segment.unlink(missing_ok=True)
                self._materialize_item(
                    asset, index, segment, artifacts.staging_segments[index],
                    item_bytes=length, base_bytes=written, total_bytes=total_bytes,
                    progress=progress,
                )
            written += length
            payload = self._identify_path(segment)
            tracks = _segment_track_facts(payload)
            video_streams = self._video_streams(segment)
            video_facts = _primary_video_facts(video_streams)
            if index == 0:
                first_payload = payload
                first_tracks = tracks
                first_video = video_facts
                if asset.dolby_vision or dual_hevc_video_streams(video_streams):
                    # 多段 DV：逐段 BL+EL 合并后再追加（2026-09-24 用户裁定并已
                    # 真机验证）。见本文件 build_dv_segment_command 的说明。
                    dv_enabled = True
            else:
                assert first_tracks is not None and first_video is not None
                try:
                    if dv_enabled:
                        # DV 多段（2026-09-24 实测）：段级轨布局由 _dv_segment
                        # 按官方 PID 计划经 ffmpeg 重建，不依赖 mkvmerge 对
                        # 原始分段的轨道识别——该识别在这类 DIY 盘上逐段不可靠
                        # （生产实测《阿凡达》加长版 33 段：首段见 2 条音轨、
                        # 其余段见 0 条，段 2..33 被误判违规尾部排除，成品只剩
                        # 第 1 段，最终 EL 一致性门拒绝：source=256758
                        # recovered=14972）。故跳过音轨/字幕轨逐段对应核对；
                        # 视频事实仍核对（ffprobe 输入可靠，防跨段分辨率/编码漂移）。
                        subset = None
                    else:
                        subset = require_segment_track_correspondence(
                            first_tracks, tracks, label=label)
                    require_segment_video_correspondence(
                        first_video, video_facts, label=label)
                except MatroskaBuildError as error:
                    # 不立刻抛：违规段是否可显式排除要等全部段核对完
                    # （只有尾部连续违规段才能排除，见 _trailing_exclusions）。
                    violations.setdefault(index, str(error))
                else:
                    if subset is not None:
                        # 纯子集（末段少声明若干轨）同样属于"与首段不完全对应"：
                        # 2026-09-15 实测——把它追加进成品会让 mkvmerge 汇总出的
                        # 容器**没有时长**（"最终 MKV 缺少有效时长/Cues 信息"，被
                        # 管线成品门拒绝；单独 mux 该段、或只追加完全对应的段则正常）。
                        # 因此尾部子集与漂移一样按尾部排除处理（见 _trailing_exclusions），
                        # 事实照旧留痕。
                        fact = self._segment_subset_fact(clips, index, subset)
                        violations.setdefault(
                            index, fact.split("：", 1)[-1] if "：" in fact else fact)
            corresponding.append(index not in violations)
            # 末端溢出（PGS 显示集 PTS 越过切点）**只测量、不裁切**：
            # 裁切（无论 matroska 还是 TS 承载）会让 mkvmerge 对追加成品**无法
            # 确定容器时长**，被成品门判「缺少有效时长/Cues 信息」；而原始分段
            # 追加一切正常（真盘实测：17 段原始分段 → rc=0、时长 6574.81s 双工具
            # 一致）。溢出的成因是母盘切点落在 PGS 显示集内部（**母盘结构，非本
            # 管线缺陷**），其后果是每个接缝有 ~0.1–0.5s 无视频（mkvmerge 按
            # 上一文件最大轨末续接）——把它作为**实测事实**进入时长期望与日志
            # （见 _verify_multiclip_seams 的 expected_overhang），而不是靠裁切
            # 去改字节（裁切的代价是成品时长不可测，得不偿失）。
            duration_seconds = (
                (clips[index].out_time - clips[index].in_time) / 90000
                if index < len(clips) else 0.0
            )
            mux_segment = segment
            if dv_enabled:
                # 段级 DV：BL+EL 合并 + ffmpeg 官方音轨/字幕中间件 → 段级自洽 MKV，
                # 再按播放序追加（各段官方轨集合相同，由 layout 核对强制）。
                dv_segment = self._dv_segment(
                    asset, mux_segment,
                    mux_segment.with_name(mux_segment.name + ".dv.mkv"),
                    progress, label=label, layout=dv_layout)
                if dv_layout is None:
                    dv_layout = {"audio": dv_segment["official_audio"],
                                 "subtitle": dv_segment["official_subtitle"]}
                    dv_resolved = dv_segment["resolved"]
                dv_evidence.append(dv_segment["evidence"])
                mux_segment = dv_segment["path"]
            if duration_seconds > 0:
                first_ts = self._segment_first_timestamp(segment)
                if first_ts is not None:
                    overhang = self._segment_tail_overhang(
                        segment, first_ts, duration_seconds)
                    if overhang > self._SEGMENT_TAIL_OVERHANG_TOLERANCE:
                        expected_overhang += overhang
                        fact = (
                            f"{label} 末端溢出 {overhang:.3f}s（越过 play item "
                            f"out_time 的 PGS/音轨包；母盘切点落在显示集内部的"
                            "结构事实）→ 计入时长期望，不改字节"
                        )
                        logger.warning("%s", fact)
                        if len(variances) < self._SEGMENT_SUBSET_LOG_LIMIT:
                            variances.append(fact)
            mux_segments.append(mux_segment)
            if self._has_hdr10plus(segment):
                # HDR10+ 需要整片抽取+剥离后再封装；追加语法无法同时表达
                # 分段与剥离后的裸流，退回拼接会产出动态元数据未处理的成品。
                raise MatroskaBuildError(
                    f"多段合辑主片 {label} 命中 HDR10+ 动态元数据剥离路线："
                    "该路线不支持按 play item 分段追加，失败关闭（不退回字节拼接）"
                )
        assert first_payload is not None
        excluded = self._trailing_exclusions(clips, bounds, corresponding, violations)
        if dv_enabled and excluded:
            # DV 多段不允许尾部排除：段级 DV MKV 由官方 PID 计划重建，不应出现
            # 结构违规；若出现（如视频事实漂移）即失败关闭——静默截断会让成品
            # 缺段，而 EL 一致性门比对的是全片源序列，必须保持同一口径。
            raise MatroskaBuildError(
                "DV 多段合辑不允许尾部排除段：违规段 "
                f"{sorted(excluded)}（段级轨布局由官方 PID 计划重建，"
                "出现结构违规即失败关闭，不静默截断）"
            )
        if progress:
            progress(40, f"MPLS 主片分段完成：{len(bounds)} 段"
                         + (f"，尾部排除 {len(excluded)} 段" if excluded else ""))
        if variances:
            logger.warning(
                "多段合辑子集差异 %d 处（已记录，不拒绝）：%s",
                len(variances), "｜".join(variances),
            )
        return (first_payload, tuple(variances), excluded, tuple(mux_segments),
                expected_overhang, dv_evidence, dv_resolved)

    @staticmethod
    def _trailing_exclusions(
        clips: list,
        bounds: tuple[tuple[int, int], ...],
        corresponding: list[bool],
        violations: dict[int, str],
    ) -> tuple[int, ...]:
        """违规段只在**尾部连续**时显式排除；否则失败关闭（中段不可跳过）。

        2026-09-15 越南战争（S01E09 盘）：主片 playlist 19 段里 item 0..16 是两集
        正片（同一 clip 连续覆盖），item 17 是 173s 预告（轨道结构为首段的纯子集，
        已按事实放行），item 18 是 11.4s 碟内 logo——它与首段**同 PID 不同声道数**
        （0x1100 首段 6ch、本段 2ch），mkvmerge 只能映射到首文件那条 6ch 轨，并入即
        坏音。这类段只能显式排除并留痕，否则该标题永远不可交付（用户 2026-09-14
        对"终段少声明字幕 PID"的同类裁定：结构差异不该让标题永远失败）。

        判定：从尾向前，只要某段不违规就停止；违规段之后的**任何**段都不许被
        排除（那会跳过中段内容）。首段永不排除（它是轨布局基准），全部违规时
        也失败关闭。
        """
        if not violations:
            return ()
        excluded: list[int] = []
        for index in range(len(corresponding) - 1, 0, -1):
            if corresponding[index]:
                break
            excluded.append(index)
        if not excluded or len(excluded) == len(corresponding):
            raise MatroskaBuildError(
                "多段合辑主片轨道不对应由首段之后第 "
                f"{min(violations) + 1} 段起且无法按尾部排除（首段不可排除）："
                f"{violations[min(violations)]}"
            )
        facts = []
        for index in sorted(excluded):
            clip = clips[index] if index < len(clips) else None
            clip_text = (
                f"clip={clip.clip_id} in={clip.in_time / 90000:.1f}s "
                f"out={clip.out_time / 90000:.1f}s"
                if clip is not None else "clip=?"
            )
            start, length = bounds[index]
            facts.append(
                f"item {index + 1}/{len(corresponding)} {clip_text} "
                f"字节=[{start},{start + length})：{violations[index]}"
            )

        logger.warning(
            "多段合辑尾部 %d 段轨道结构与首段不兼容，已显式排除（不并入、不静默）："
            "\n%s",
            len(excluded), "\n".join(facts),
        )
        return tuple(sorted(excluded))

    def _segment_first_timestamp(self, segment: Path) -> float | None:
        """分段首个包的时间戳（秒，素材内时间轴）；测不出返回 None（不裁切）。"""
        try:
            completed = subprocess.run(
                [self.probe, "-v", "error", "-read_intervals", "%+#1",
                 "-show_entries", "packet=pts_time,dts_time", "-of", "json",
                 str(segment)],
                check=True, capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        try:
            packets = json.loads(completed.stdout).get("packets") or []
        except json.JSONDecodeError:
            return None
        for packet in packets:
            for key in ("pts_time", "dts_time"):
                value = packet.get(key)
                if value is None:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    def _segment_tail_overhang(self, segment: Path, first_ts: float,
                               duration: float) -> float:
        """分段末端最大时间戳相对"标称末点"的溢出秒数（0.0 = 无溢出/测不出）。

        2026-09-15 越南战争：切点常落在 PGS 显示集内部，末段字幕包的 PTS 会越过
        play item 的 out_time（实测 item0 +0.545s、item1 +0.137s、item5 +0.429s）。
        只读探针（seek 到末尾附近的窗口），不整文件扫描。
        """
        window_start = first_ts + duration - 0.5
        try:
            completed = subprocess.run(
                [self.probe, "-v", "error",
                 "-read_intervals", f"{window_start:.3f}%+{1.5:.3f}",
                 "-show_entries", "packet=pts_time,dts_time",
                 "-of", "json", str(segment)],
                check=True, capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return 0.0  # 探针失败不阻断构建：不裁切（保守），由拼接门报问题
        try:
            packets = json.loads(completed.stdout).get("packets") or []
        except json.JSONDecodeError:
            return 0.0
        ends: list[float] = []
        for packet in packets:
            for key in ("pts_time", "dts_time"):
                value = packet.get(key)
                if value is None:
                    continue
                try:
                    ends.append(float(value))
                except (TypeError, ValueError):
                    pass
        if not ends:
            return 0.0
        return max(0.0, max(ends) - (first_ts + duration))

    def _trim_segment(self, segment: Path, first_ts: float, duration: float,
                      staging: Path) -> Path | None:
        """把分段裁到 play item 标称时长（copy 级，不重编码）；失败返回 None。

        为什么必须裁：mkvmerge 的 file 模式追加把下一段接在**上一个文件的最大轨末**
        之后，而每段末尾的 PGS 包 PTS 会越过切点 → 每个接缝留 0.1~0.5s 的视频/音频
        空洞（实测接缝 1：视频停在 539.773s，540.35s 才接上；17 个接缝累计 5.6s，
        正是 ±2s 总时长门的偏差）。裁到标称时长后各轨末端对齐，接缝不再有洞。
        `-t` 以首个包为基准 → 与素材内起点无关，恰好等于"从该段内容起点保留 duration 秒"。
        """
        # 产物名必须**跟着段文件**（段文件含 generation：main-title.m2ts.partial.<N>.item<k>.m2ts）。
        # 用未带代次的 staging 名（main-title.materializing.item<k>）会让不同构建
        # 的裁切产物同名互相覆盖/复用——2026-09-15 实测：早期有缺陷的版本留下的
        # 裁切产物被后续构建复用，导致 "does not contain a track with the ID 5"
        # 与追加映射失败。staging 参数保留用于日志定位。
        trimmed = segment.with_name(segment.name + ".trimmed.ts")
        trimmed.unlink(missing_ok=True)
        command = [
            self.ffmpeg, "-y", "-nostats", "-v", "error",
            "-i", str(segment),
            # -map 0 必须显式给出：ffmpeg 默认每种类型只挑一条（best video +
            # best audio + 一条字幕），裁切会把其余音轨/全部 PGS 丢掉——实测
            # 后果是 mkvmerge 报 "A track with the ID 5 was requested but not
            # found" 并以 --append-to invalid 终止（2026-09-15 越南战争）。
            "-map", "0",
            # 不加 -copyts：ffmpeg 的 -t 以**输出时间轴**为准，保留素材内时间戳
            # （-copyts）时起点是 600s 量级，-t 539.8 会把整段裁空（实测产出
            # 0 字节）。裁切按"从该段内容起点保留 duration 秒"的语义即可。
            "-t", f"{duration:.3f}",
            # 承载容器必须与源同族（mpegts）：裁切只改时间轴，不改容器语义。
            # 用 matroska 承载时 ffmpeg 不写 Segment Info 时长（TS 输入 + -c copy），
            # mkvmerge 汇总成品时因此报「缺少有效时长/Cues 信息」被管线自己的成品门
            # 拒绝（2026-09-15 越南战争 build-12 实证）。
            "-c", "copy", "-f", "mpegts", str(trimmed),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=3600)
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning("分段裁切失败（保留原段）：%s", _process_error_detail(error))
            return None
        if completed.returncode != 0 or not trimmed.exists():
            logger.warning("分段裁切失败（退出码 %s，保留原段）：%s",
                           completed.returncode, (completed.stderr or "")[-200:])
            return None
        return trimmed

    @staticmethod
    def _segment_subset_fact(clips: list, index: int,
                             subset: SegmentTrackSubset) -> str:
        """把纯子集差异绑定到可复核的分段事实（clip id + 素材内区间 + 缺失轨）。"""
        clip = clips[index] if index < len(clips) else None
        clip_text = (
            f"clip={clip.clip_id} in={clip.in_time / 90000:.1f}s "
            f"out={clip.out_time / 90000:.1f}s"
            if clip is not None else "clip=?"
        )
        return _bounded_detail(
            f"{subset.label} {clip_text}：{subset.detail()}",
            MatroskaBuilder._SEGMENT_SUBSET_LOG_WIDTH,
        )


    @staticmethod
    def _item_durations(asset: BlurayTitleAsset, bounds: tuple[tuple[int, int], ...]) -> tuple[float, ...]:
        """各 play item 的时长（秒，MPLS 顺序）；与边界数量不符即失败关闭。

        时长为 out_time - in_time：随 out_time 用素材内起点，而不是从 0 起算，
        否则多段合辑的期望总时长会超过实际成品时间轴（生产缺陷根因）。
        """
        clips = sorted(asset.clips, key=lambda clip: clip.index)
        if len(clips) != len(bounds):
            raise MatroskaBuildError(
                f"多段合辑 play item 时长与分段数量不一致：{len(clips)} != {len(bounds)}"
            )
        durations = tuple(
            (clip.out_time - clip.in_time) / 90000 for clip in clips
        )
        if any(duration <= 0 for duration in durations):
            raise MatroskaBuildError("多段合辑 play item 时长非正，拒绝发布")
        return durations

    def _verify_multiclip_seams(
        self,
        asset: BlurayTitleAsset,
        output: Path,
        bounds: tuple[tuple[int, int], ...],
        *,
        segments: Sequence[Path] = (),
        expected_overhang: float = 0.0,
    ) -> None:
        """多段合辑拼接硬门：总时长、每个拼接点时间戳连续、按点 seek 锚定。

        「命令成功」不构成拼接证据：分段各自合法、追加后仍可能在拼接点回跳
        或让对应流截断。这里全部由成品本身可复核：容器时长对 Σ play item
        时长、有界窗口内拼接点两侧时间戳不回跳不留缺口，并把每个拼接点加入
        seek 目标（不只 10/50/90%）。

        门禁失败必须自诊断（2026-09-14 地球脉动：只报「无视频包」区分不出
        来源缺头部、append 未续接还是探针越界）：失败详情携带窗口包数（窄窗
        + 加宽窗）、首末 pts_time、逐段字节数与逐段实测时长、成品声明时长与
        Σ 分段的对照，以及调用方保留的完整成品路径。测量本身失败不得替换
        原始失败原因，只额外记录失败事实。门不放宽：成品仍不发布。
        """
        durations = self._item_durations(asset, bounds)
        expected = sum(durations)
        # 2026-09-15：期望时长 = Σ play item 时长 + **逐段实测的末端溢出合计**。
        # mkvmerge 的 file 模式追加把每段接在上一文件最大轨末之后，而母盘切点常
        # 落在 PGS 显示集内部（该段末包 PTS 越过 out_time）→ 每缝留 ~0.1–0.5s
        # 无视频。这是**母盘结构事实**、逐段实测（见 _prepare_items），不是缺陷；
        # 期望必须把它计入，否则一个完全正确的成品会被 ±2s 门误拒。
        expected += max(0.0, float(expected_overhang or 0.0))
        declared = _probe_duration(self.probe, output)
        if declared <= 0:
            raise MatroskaBuildError("多段合辑成品时长无效，无法验证拼接")
        if abs(declared - expected) > _SEAM_DURATION_TOLERANCE:
            raise MatroskaBuildError(
                f"多段合辑成品时长 {declared:.1f}s 与期望（Σ play item 时长 "
                f"{sum(durations):.1f}s + 逐段实测末端溢出 "
                f"{expected_overhang:.1f}s = {expected:.1f}s）偏差超限"
                f"（±{_SEAM_DURATION_TOLERANCE:.0f}s），拒绝发布"
            )
        clips = sorted(asset.clips, key=lambda clip: clip.index)
        segment_durations, segment_duration_error = self._segment_duration_facts(
            durations, segments,
        )
        timeline = _probe_video_timeline(self.probe, output)
        base = 0.0
        try:
            base = float(timeline.get("start_time") or 0)
        except (TypeError, ValueError):
            base = 0.0
        seams: list[float] = []
        cumulative = base
        for duration in durations[:-1]:
            cumulative += duration
            seams.append(cumulative)
        for index, seam in enumerate(seams, start=1):
            measured: dict = {}
            label = f"多段合辑拼接点 {index}/{len(seams)}"
            context = self._seam_segment_context(
                bounds, durations, clips, segments, segment_durations,
                segment_duration_error, declared, expected, index,
            )
            for text in context:
                logger.warning("%s %s", label, text)
            try:
                _verify_seam_continuity(
                    self.probe, output, seam, label=label, diag=measured,
                    wide_start=_seam_wide_start_seconds(seam, durations[index - 1]),
                )
            except MatroskaBuildError as error:
                raise _seam_gate_error(
                    error,
                    f"（目标 {seam:.1f}s）",
                    _seam_diagnostic_text(measured, context),
                    seam_index=index, seam_seconds=seam,
                ) from error
        # 拼接点既验时间戳连续，也做与 10/50/90% 同一套 seek 门（不放松）。
        seek_diagnostics: dict = {}
        try:
            _verify_cues_and_seek(
                self.probe, output, int(max(0.0, declared) * 1_000_000_000),
                diagnostics=seek_diagnostics, extra_targets=tuple(seams),
            )
        except MatroskaBuildError as error:
            diagnostics = getattr(error, "seek_diagnostics", None) or seek_diagnostics
            raise _seam_gate_error(
                error, "（同一套 seek 门）",
                _seam_seek_diagnostic_text(diagnostics, seams),
                seam_index=diagnostics.get("seam_index"),
                seam_seconds=diagnostics.get("seam_seconds"),
            ) from error

    @staticmethod
    def _seam_segment_context(
        bounds: tuple[tuple[int, int], ...],
        durations: tuple[float, ...],
        clips: list,
        segments: Sequence[Path],
        segment_durations: dict[int, float],
        segment_duration_error: str,
        declared: float,
        expected: float,
        seam_index: int,
    ) -> list[str]:
        """拼接点两侧的可复核分段事实（连续时也记录，供成品留痕）。"""
        return [
            text for text in (
                _seam_segment_bounds_text(bounds, durations, clips, seam_index),
                _seam_segment_bytes_text(bounds, segments, seam_index),
                _seam_segment_durations_text(segment_durations, seam_index,
                                             error=segment_duration_error),
                _seam_declared_vs_expected_text(declared, expected),
            ) if text
        ]


    def _segment_duration_facts(
        self,
        durations: tuple[float, ...],
        segments: Sequence[Path],
    ) -> tuple[dict[int, float], str]:
        """逐段实测时长（mkvmerge 看到的各分段）：测量失败只记录，不替换原因。"""
        measured: dict[int, float] = {}
        for index, segment in enumerate(segments):
            if index >= len(durations):
                break
            try:
                measured[index] = _probe_duration(self.probe, Path(segment))
            except MatroskaBuildError as error:
                return measured, f"第 {index + 1} 段时长测量失败：{error}"
        return measured, ""

    @staticmethod
    def _preserve_seam_artifact(output: Path) -> Path | None:
        """把拼接门失败的完整成品移出正常清理范围，供后续复检。

        representation 的失败路径会无条件删除 partial 成品；沙盒里必须留下
        可复检的字节证据。rename（非复制）保证成品不再被当作可发布路径；
        目标已存在（同代重复）或改名失败时不覆盖、不吞掉真实失败原因。
        """
        preserved = seam_preserved_path(output)
        try:
            if preserved.exists():
                return preserved
            os.replace(output, preserved)
            return preserved
        except OSError as error:
            logger.warning("拼接门失败成品保留失败：%s", _bounded_detail(error, 200))
            return None

    @staticmethod
    def _seam_gate_failure(error: MatroskaBuildError, asset: BlurayTitleAsset,
                           output: Path) -> MatroskaBuildError:
        """拼接门失败详情：先保留成品，再合成「原因 + 测量事实 + 保留路径」。

        成品 rename 到 ``seam_preserved_path``（正常清理不再命中该路径），
        详情点名同一路径供复检，并补上多 clip 合辑上下文。门本身不放宽：
        能走到这里的构建一律不发布。
        """
        diagnostics = getattr(error, "seam_diagnostics", None) or {}
        preserved = MatroskaBuilder._preserve_seam_artifact(output)
        preserved_text = (
            f"失败成品已保留在 {preserved}（供复检，发布路径不含该文件）"
            if preserved is not None
            else f"失败成品保留失败（{seam_preserved_path(output)} 未生成）"
        )
        facts = str(diagnostics.get("facts") or "").strip()
        # 三部分都可能很长（原因 ≤700、事实 ≤700、路径 ~180），而执行账本的
        # detail 只保留前 300 字符：保留路径必须紧跟失败原因，测量事实放尾部
        # （完整事实仍在 seam_diagnostics 与 build 日志里）。
        head = str(diagnostics.get("head") or "").strip() or _bounded_detail(
            error, _SEAM_FAILURE_HEAD_LIMIT
        )
        tail = "｜".join(text for text in (facts, _multiclip_facts(asset)) if text)
        failure = MatroskaBuildError(
            f"{head}｜{preserved_text}" + (f"｜{tail}" if tail else "")
        )
        failure.seam_diagnostics = {**diagnostics, "preserved": str(preserved or "")}
        return failure


    def build(self, asset: BlurayTitleAsset, output: Path, *, title: str,
              progress: Callable[[int, str], None] | None = None) -> dict:
        if shutil.which(self.executable) is None:
            raise MatroskaBuildError("服务器未安装 mkvmerge")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # BATCH-006 R2 P0：唯一工件计算入口——source/staging 不再手写。
        bounds = multiclip_item_bounds(asset)
        artifacts = build_artifacts(
            output, segment_count=len(bounds) if bounds else 0,
        )
        source = artifacts.source
        temporaries: list[Path] = []
        first_payload: dict | None = None
        segment_variances: tuple[str, ...] = ()
        # 尾部轨道不兼容而被显式排除的段（不并入成品；事实随构建记录）。
        excluded_items: tuple[int, ...] = ()
        # 实际参与封装的段路径（多段路线含裁切件；单段路线为空 → 无清理）。
        mux_segments: tuple[Path, ...] = ()
        # 多段 DV 的逐段证据（按播放序；非 DV 路线为空）与首段官方轨映射。
        dv_evidence: list[dict] = []
        dv_resolved = None
        tool_warnings = ""
        try:
            if bounds:
                (first_payload, segment_variances, excluded_items,
                 mux_segments, expected_overhang, dv_evidence,
                 dv_resolved) = self._prepare_items(
                    asset, bounds, artifacts, progress=progress,
                )
            else:
                self._materialize_or_reuse_title(
                    asset, source, progress=progress, staging=artifacts.staging,
                )
            if progress:
                progress(40, "正在识别主片音视频轨")
            if bounds:
                # 多段合辑以首段识别结果解析轨 ID；其余段已逐段核对过对应关系，
                # mkvmerge 按同一轨布局续接追加。被显式排除的尾部段不参与封装，
                # 也不参与拼接门与时长期望（见 _prepare_items/_trailing_exclusions）。
                mux_source = (mux_segments[0] if mux_segments
                              else artifacts.segments[0])
                identified = parse_identification(first_payload)
                included_items = tuple(
                    index for index in range(len(bounds))
                    if index not in excluded_items
                )
                included_bounds = tuple(bounds[index] for index in included_items)
                # 封装与拼接门都用"参与封装"的段（末端溢出的段已被裁切替换）。
                included_segments = tuple(
                    (mux_segments[index] if index < len(mux_segments)
                     else artifacts.segments[index])
                    for index in included_items
                )
                included_asset = replace(
                    asset, clips=tuple(
                        clip for clip in sorted(asset.clips, key=lambda item: item.index)
                        if clip.index in set(included_items)
                    ),
                )
            else:
                mux_source = source
                identified = parse_identification(self._identify_path(source))
            resolved = resolve_official_tracks(asset, identified)
            if bounds and dv_evidence:
                if dv_resolved is not None:
                    # 多段 DV 的官方轨映射来自**段级实测**（原始分段在这类盘上
                    # mkvmerge 读不到音轨，用它算出来的映射会让成品门误判数量）。
                    resolved = dv_resolved
                # 多段 DV：段级已是自洽的单视频轨 DV MKV（BL+EL 已合并、官方
                # 音轨/字幕已入段），这里只做**纯追加**（时间轴按段续接）；DV
                # 证据由各段按播放序聚合成 dv_facts，交给统一的 Profile 7 成品门。
                dolby_vision = True
                dv_facts = _aggregate_multiclip_dv_facts(dv_evidence)
                command = build_append_only_command(
                    included_segments[0], included_segments[1:], output, title=title)
            elif bounds:
                # 多段合辑的 HDR10+ 路线已在逐段阶段失败关闭；这里只做追加封装。
                dolby_vision = False
                dv_facts = None
                command = build_mkvmerge_command(
                    asset, resolved, mux_source, output, title=title,
                    append_inputs=included_segments[1:],
                )
            else:
                video_streams = self._video_streams(mux_source)
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
            # 2026-09-14 P2：mkvmerge 命令（含选定轨 ID 与输入/输出路径）必须先
            # 落日志——成品轨数量不一致时命令是唯一可重建的事实来源。命令只含
            # 本地暂存路径，不含媒体 URL/凭据。
            logger.info(
                "mkvmerge 命令：%s（video=%s audio=%s subtitle=%s）",
                " ".join(command),
                ",".join(map(str, resolved.video_ids)),
                ",".join(map(str, resolved.audio_ids)),
                ",".join(map(str, resolved.subtitle_ids)) or "-",
            )
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert process.stdout is not None
            # 工具输出的有界尾部：非零退出时用于区分"空间耗尽（瞬时）"与
            # 媒体缺陷（永久），不改变既有失败文案。
            tool_tail: list[str] = []
            for line in process.stdout:
                tool_tail.append(line.rstrip())
                if len(tool_tail) > _TOOL_TAIL_LINES:
                    tool_tail.pop(0)
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
            tool_detail = "\n".join(tool_tail[-_TOOL_TAIL_LINES:])
            if code != 0 and _is_out_of_space(tool_detail):
                # 空间不足优先判定（生产实测该情形也可能以退出码 2 出现，
                # 绝不能当"仅警告"放过——那样会把截断的成品当成品送往下游）。
                raise TransientMatroskaBuildError(
                    f"mkvmerge 封装因临时空间不足中断（退出码 {code}），按瞬时故障重试："
                    f"{_bounded_detail(tool_detail, 200)}"
                )
            # mkvmerge 退出码契约（man mkvmerge：0=成功、1=**有警告但封装继续**、
            # 2=**错误并立即中止、不产出可用成品**）。
            #
            # 2026-09-17 修正：此前把 1/2 的语义写反（代码注释称"2=已完成但有
            # 警告"）。真正的封装错误（生产标本 dlv_b3532c0cbbbe66742be8 地球脉动
            # 第二季 3 段：非法 --append-to 使 mkvmerge 以退出码 2 中止、不产出
            # 任何文件）被当成"警告、成品可用"继续执行，直到成品门以
            # 「最终 MKV 缺少有效时长/Cues 信息」失败——一个硬封装失败被伪装成
            # 内容门失败并触发 9 次无效重投。现按文档语义收口：只有 1 放行，
            # 2 及其它非零一律失败关闭并带上 mkvmerge 原文。
            if code == 1:
                # 命令成功不构成正确证据：警告文本必须留痕，成品仍由紧随其后的
                # 独立校验（成品识别、轨道计数、时长门、拼接门、seek 门）判定。
                warnings_text = _bounded_detail(
                    tool_detail or "（无输出文本）",
                    600,
                )
                logger.warning("mkvmerge 退出码 1（有警告，成品继续走校验门）：%s",
                               warnings_text)
                tool_warnings = warnings_text
            elif code != 0:
                raise MatroskaBuildError(
                    f"mkvmerge 生成失败（退出码 {code}）："
                    f"{_bounded_detail(tool_detail, 300) or '（无输出文本）'}"
                )
            completed = self._run_mkvmerge_identify(
                self.executable, output, label="最终 MKV 校验")
            identified = json.loads(completed.stdout)
            _require_identified_container(identified, label="最终 MKV 校验")
            container = identified.setdefault("container", {})
            properties = container.setdefault("properties", {})
            if int(properties.get("duration") or 0) <= 0:
                # mkvmerge 对"追加了无时长头的 TS 输入"的成品可能不写容器时长
                # （实测：单输入/两输入时时长在位，多段追加后缺失）。时长本就有
                # 独立探针（ffprobe 从时间戳算出，拼接门与 seek 门都在用同一事实），
                # 因此缺字段不构成"成品无效"——用独立测量补齐后继续走同一套门，
                # 而不是拿一个工具的可选字段当否决权（越南战争 build-*/3.7.83 实证）。
                measured = _probe_duration(self.probe, output)
                if measured > 0:
                    properties["duration"] = int(measured * 1_000_000_000)
                    logger.warning(
                        "成品识别缺容器时长，改用独立探针复核：%.1fs（认定时为 "
                        "mkvmerge 未写入该可选字段）", measured)
            result = validate_finalized_identification(
                asset, resolved, identified, dolby_vision=dolby_vision,
            )
            # E1 轨覆盖门：轨道计数/语言正确 ≠ 内容正确。这里对成品本身逐轨
            # 测量在场区间，把「字幕后半部整段消失」这一类"文件生成了但内容
            # 不完整"的成品挡在 Ready 之前（2026-09-17 Léon 事故的系统级防线）。
            #
            # 阻断范围（用户 2026-09-17 硬约束："对于单碟的电影都不能有影响"）：
            # - 多段合辑（bounds 非空）**失败关闭**：非 DV 路线有逐段轨道对应证明；
            #   DV 路线的音轨/字幕按官方 PID 计划逐段重建，且成品覆盖门随后会将源侧
            #   在场窗逐轨对比到追加结果。Léon 事故正是这一路线。
            # - 单段/单碟（bounds 为空）**取证模式**：照常测量、照常把违规项与逐轨
            #   事实写进构建结果并在日志留名，但不阻断——母盘上本来就可能存在只
            #   覆盖局部的字幕轨（强制字幕/评论轨），在缺少源侧对应证明前直接拒绝
            #   会误伤健康成品。生产取证后再决定是否收紧。
            coverage_seconds = int(properties.get("duration") or 0) / 1_000_000_000
            if coverage_seconds <= 0:
                coverage_seconds = _probe_duration(self.probe, output)
            coverage_tracks = parse_identification(identified)
            source_presence = None
            source_packet_timestamps = None
            if bounds and any(
                    track.type in _COVERAGE_TRUE_KINDS for track in coverage_tracks):
                # 源侧字幕 packet 证明只对「输入流序号 == 成品轨位置」可证明的
                # 路径开放（豁免判定本身始终 fail-closed）：
                # - 多段 DV：段级 MKV 按同一官方 PID 计划构造，轨序由
                #   _dv_segment 强制一致（Léon 事故防线，2026-09-17）。
                # - 多段原始 TS（3.7.137，王朝形态）：逐段 ffprobe 流类型序列与
                #   成品轨类型序列 1:1（顺序与计数完全一致）时，TS 流序号即
                #   Matroska 轨位置；任何数据流/多余流/乱序/探针失败都拒绝
                #   豁免，维持原 600s 失败门。原碟稀疏字幕轨（如王朝轨9 PGS
                #   chi 仅每 clip 头部约 280s 有包）由此按「源侧完整 packet
                #   时间戳证明自身空档」放行；提取丢内容仍失败关闭。
                mapping = None
                if dv_evidence:
                    mapping = (dict(enumerate(
                        range(len(coverage_tracks)))))
                else:
                    mapping = _resolve_segment_track_mapping(
                        self.probe, included_segments, resolved, coverage_tracks)
                if mapping is not None:
                    source_windows = coverage_windows(
                        coverage_seconds,
                        max_gap=_COVERAGE_SUBTITLE_MAX_GAP_SECONDS,
                        window=_COVERAGE_WINDOW_SECONDS,
                    )
                    source_scan_heartbeat = _start_progress_ticker(
                        progress, 96, "完整扫描多段源字幕 packet 时间戳",
                    )
                    try:
                        source_evidence = _probe_source_subtitle_evidence(
                            self.probe, included_segments, source_windows,
                        )
                    finally:
                        source_scan_heartbeat.set()
                    # TS 流序号 → 成品轨位（DV 段级 MKV 两者天然同位）。
                    source_presence = tuple(
                        {mapping[stream_id] for stream_id in present
                         if stream_id in mapping}
                        for present in source_evidence.presence_by_window)
                    source_packet_timestamps = {
                        mapping[stream_id]: stamps
                        for stream_id, stamps in
                        source_evidence.packet_timestamps_by_stream.items()
                        if stream_id in mapping}
            result["track_coverage"] = require_track_coverage(
                self.probe, output, coverage_tracks,
                duration=coverage_seconds, label="最终 MKV 校验",
                source_presence_by_window=source_presence,
                source_packet_timestamps_by_stream=source_packet_timestamps,
                enforce=bool(bounds),
            )
            heartbeat = _start_progress_ticker(
                progress, 97, "成品校验中（时长/拼接点/seek 门）")
            try:
                if bounds:
                    # 多段合辑：先验总时长与每个拼接点的时间戳连续性，并把
                    # 拼接点加入同一套 seek 门（见 _verify_multiclip_seams）。
                    # 拼接门失败必须自诊断：测量事实在异常对象上带回，保留
                    # 成品后再组装失败详情（保留路径不可被后续清理删除）。
                    self._verify_multiclip_seams(
                        included_asset, output, included_bounds,
                        segments=included_segments,
                        expected_overhang=expected_overhang,
                    )
                else:
                    self._verify_seekable(output, dv_facts=dv_facts)
            except TransientMatroskaBuildError:
                raise
            except MatroskaBuildError as error:
                raise self._seam_gate_failure(error, asset, output) from error
            finally:
                heartbeat.set()
            if dolby_vision:
                # BATCH-006 R4 对齐：与 FifoMatroskaBuilder 一致，DV 成品执行
                # 完整 Profile 7 证据链（RPU 载荷 + EL 融合大小 + dovi_tool
                # 命令事实），再由 ffprobe 验证 DOVI 配置记录。此前仅 seek
                # 正交 + _verify_dolby_vision，缺 RPU/EL 硬门。
                _verify_dv_profile7_evidence(self.ffmpeg, output, dv_facts)
                self._verify_dolby_vision(output)
            # 校验门用完了：清理本代次的裁切产物（仅裁切件；原始分段仍按既有
            # 检查点语义保留/清理）。失败路径不清理——保留作为复核证据。
            for path in mux_segments:
                if path.name.endswith(".trimmed.ts"):
                    path.unlink(missing_ok=True)
            if tool_warnings:
                # mkvmerge 警告同样必须进构建结果（成品通过全部门，但警告是事实：
                # 复核者要能看见"这台机器这次封装到底警告了什么"）。
                result["mkvmerge_warnings"] = tool_warnings
            if excluded_items:
                result["excluded_trailing_items"] = list(excluded_items)
            if segment_variances:
                # 纯子集差异是成品事实（该区间确实没有这些轨）：必须随构建
                # 结果进入 manifest 诊断，而不是只留在日志里。
                result["segment_track_variances"] = list(segment_variances)
            return result
        except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as error:
            if _is_out_of_space(error):
                # 空间耗尽不是媒体缺陷：永久失败会烧掉重试预算（ENOSPC 曾被
                # 包装成 MatroskaBuildError）。
                raise TransientMatroskaBuildError(
                    f"临时空间不足（{_bounded_detail(_process_error_detail(error), 200)}），"
                    "按瞬时故障重试"
                ) from error
            if isinstance(error, subprocess.TimeoutExpired):
                # 长任务（dv 层提取/合并）超时按瞬时分类，不得包装成"校验失败"
                # 的永久媒体缺陷。
                raise _tool_failure("最终 MKV 构建", error) from error
            raise MatroskaBuildError(f"最终 MKV 校验失败：{error}") from error
        finally:
            # 单文件 source 与逐段分段都是可跨重试复用的检查点：只有完整文件
            # 保留，截断/缺失一律删除（绝不把错位字节追加进成品）。
            if not self._source_checkpoint_is_valid(asset, source):
                source.unlink(missing_ok=True)
            for index, segment in enumerate(artifacts.segments):
                if not self._segment_checkpoint_is_valid(bounds[index][1], segment):
                    segment.unlink(missing_ok=True)
            for path in artifacts.staging_segments:
                path.unlink(missing_ok=True)
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
        except (OSError, subprocess.SubprocessError) as error:
            raise _tool_failure("ffprobe 探测", error) from error
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
        """流式 FIFO 无物化主片/分段：正在封装的成品 + 同槽位保留的既有成品。

        保留成品指交付侧可继续传输的上一版成品（末次传输后保留 15 分钟），
        它与本次封装同时占用同一目录空间。
        """
        stream_bytes = int(asset.size)
        muxed_output = stream_bytes
        retained_representation = stream_bytes
        return muxed_output + retained_representation + _SPACE_MARGIN_BYTES

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
        # FIFO 路线不经物化复用钩子，合辑结构同样必须留痕（2026-09-14 P2）。
        compilation = describe_multiclip_compilation(asset)
        if compilation:
            logger.warning("%s", compilation)
        if planned_segment_count(asset) >= 2:
            # ffmpeg 单输入流 copy 无法按 play item 追加：整条虚拟流直写会在
            # 各段 in_time 处留下不连续时间轴（正是 mkvmerge 追加要修的缺陷）。
            raise MatroskaBuildError(
                "多段合辑主片命中 FFmpeg FIFO 流式路线：该路线不支持按 play item "
                "分段追加，失败关闭（不输出时间轴错误的成品）"
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
            feed_exc: list[BaseException] = []

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
                    feed_exc.append(error)
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
                if _is_out_of_space(detail):
                    # 空间耗尽按瞬时故障重试，不得当作媒体缺陷失败关闭。
                    raise TransientMatroskaBuildError(
                        f"ffmpeg 封装因临时空间不足中断（退出码 {code}），按瞬时故障重试"
                        f"{feed_note}\n{_bounded_detail(detail, 200)}"
                    )
                if feed_exc and isinstance(feed_exc[-1], TransientBlurayReadError):
                    # 规格 D4：源读取的**瞬态**故障（reader 子进程中断/通信失败）
                    # 不是媒体缺陷——ffmpeg 因断流报了非零退出码，但根因可重试。
                    raise TransientMatroskaBuildError(
                        f"封装因源读取瞬态故障中断（退出码 {code}），按瞬时故障重试"
                        f"{feed_note}\n{_bounded_detail(detail, 200)}"
                    )
                raise MatroskaBuildError(
                    f"ffmpeg 封装失败（退出码 {code}）{feed_note}\n"
                    f"ffmpeg 命令：{' '.join(cmd)}\n{detail}"
                )

            # Validate the output.
            return self._validate_output(asset, output)

        except OSError as error:
            if _is_out_of_space(error):
                raise TransientMatroskaBuildError(
                    f"FIFO 封装临时空间不足（{_bounded_detail(_process_error_detail(error), 200)}），"
                    "按瞬时故障重试"
                ) from error
            raise
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
            if isinstance(error, subprocess.TimeoutExpired):
                raise _tool_failure("最终 MKV 流校验", error) from error
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
        # 显式探测超时属瞬时故障（文档语义），探测超时不构成媒体缺陷证据。
        raise TransientMatroskaBuildError(
            f"HDR10+ 探测超时（>{timeout_seconds:.0f}s），子进程已终止，按瞬时故障重试"
        )
    if failure is not None:
        raise TransientMatroskaBuildError(
            f"HDR10+ 探测读取失败（子进程已终止回收）："
            f"{_bounded_detail(_process_error_detail(failure))}"
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
                          ffmpeg: str = "ffmpeg",
                          extra_targets: Sequence[float] = ()) -> dict:
    """结构化 Cues + 多点时间窗口 seek（Codex P1-B / round-17 B）。

    - 结构化 EBML Cues（>=1 个完整 CuePoint，非裸字节搜索）；
    - 10%/50%/90%（按 MPLS 期望时长）各用 `target%+10` 时间窗口读取视频
      packet 与 decoded frame。

    ``extra_targets``（秒，绝对时间）：多段合辑的内部拼接点，与三个百分比
    目标走完全相同的门与容差（不放松、不另立标准）。

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
    try:
        cue_points = _count_cue_points(output)
    except CuesParseError as error:
        if diagnostics is not None:
            diagnostics["cues_status"] = error.status
        raise MatroskaBuildError(
            f"成品 Cues 解析失败（{error.status}）：{error}"
        ) from error
    if diagnostics is not None:
        diagnostics["cue_points"] = cue_points
        diagnostics["cues_status"] = "valid" if cue_points else "absent"
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
    jobs: list[tuple[float | None, float]] = [
        (frac, duration * frac) for frac in _SEEK_TARGETS
    ]
    jobs += [(None, float(seam)) for seam in extra_targets]
    segments = list(diagnostics.get("segments") or ()) if diagnostics is not None else []
    for job_index, (frac, target) in enumerate(jobs):
        label = f"{frac:.0%}" if frac is not None else f"拼接点 {job_index + 1 - len(_SEEK_TARGETS)}"
        point: dict = {"frac": frac, "target_seconds": round(target, 3)}
        if frac is None:
            point["seam_index"] = job_index + 1 - len(_SEEK_TARGETS)
        seek_points.append(point)
        packets = _probe_interval_packets(probe, output, target, diag=point)
        point["packet_count"] = len(packets)
        point["first_packet"] = packets[0] if packets else None
        point["last_packet"] = packets[-1] if packets else None
        if not packets:
            point["failure"] = "no_packets"
            _raise_seek_failure(
                f"成品 seek {label}（{target:.1f}s）索引/时间轴定位失败："
                f"窗口内无视频包，拒绝发布",
                frac=frac, target=target, point=point, segments=segments,
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
                _raise_seek_failure(
                    f"成品 seek {label}（{target:.1f}s）窗口内无关键访问单元（K 包），"
                    f"拒绝发布",
                    frac=frac, target=target, point=point, segments=segments,
                )
            if first_pts is None:
                point["failure"] = "packet_pts_missing"
                _raise_seek_failure(
                    f"成品 seek {label}（{target:.1f}s）首包 PTS 缺失，拒绝发布",
                    frac=frac, target=target, point=point, segments=segments,
                )
            if not (target - _SEEK_FRAME_TOLERANCE <= first_pts <= target + _SEEK_FRAME_TOLERANCE):
                point["failure"] = "packet_out_of_range"
                point["first_packet_pts"] = round(first_pts, 3)
                _raise_seek_failure(
                    f"成品 seek {label} 首包 PTS 越界（期望 {target:.1f}s，"
                    f"实际 {first_pts:.1f}s），拒绝发布",
                    frac=frac, target=target, point=point, segments=segments,
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
                **({"seam_index": point["seam_index"]} if frac is None else {}),
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
            _raise_seek_failure(
                f"成品 seek {label}（{target:.1f}s）关键帧/解码窗口失败："
                f"有视频包但窗口内无解码帧，拒绝发布",
                frac=frac, target=target, point=point, segments=segments,
            )
        first = min(frames)
        if not (target - _SEEK_FRAME_TOLERANCE <= first
                <= target + _SEEK_FRAME_TOLERANCE):
            point["failure"] = "out_of_range"
            point["first_frame_seconds"] = round(first, 3)
            _raise_seek_failure(
                f"成品 seek {label} 时间越界（期望 {target:.1f}s，"
                f"实际 {first:.1f}s），拒绝发布",
                frac=frac, target=target, point=point, segments=segments,
            )
        point["status"] = "ok"
        results.append({
            "frac": frac, "target_seconds": round(target, 3),
            **({"seam_index": point["seam_index"]} if frac is None else {}),
            "packet_count": len(packets), "frame_count": len(frames),
            "first_frame_seconds": round(first, 3),
        })
    return {"cues": results}


def _raise_seek_failure(message: str, *, frac: float | None, target: float,
                        point: dict, segments: list) -> None:
    """seek 门失败：先落测量事实（失败点上异常后由调用方组装详情），再抛出。"""
    error = MatroskaBuildError(message)
    error.seek_diagnostics = {
        "point": point, "segments": segments,
        "label": f"{frac:.0%}" if frac is not None else "拼接点",
        "seam_index": point.get("seam_index"),
        "seam_seconds": round(target, 3) if frac is None else None,
    }
    raise error


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
        raise _tool_failure("关键 AU 定位", error) from error
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
        raise _tool_failure("seek 窗口 BL 分离", error) from error
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
        raise _tool_failure("DV BL 解码帧数探测", error) from error
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
# 多段合辑拼接门：成品时长对 Σ play item 时长的容差（与视频时间轴门同一 ±2s）；
# 拼接点两侧有界窗口、最大允许空档与 B 帧重排回跳容差。
_SEAM_DURATION_TOLERANCE = 2.0
_SEAM_HALF_WINDOW_SECONDS = 5.0
_SEAM_MAX_GAP_SECONDS = 2.0
_SEAM_REORDER_TOLERANCE = 1.0
# 拼接门失败详情的有界上限：必须同时容纳原始原因（头部）与可复核测量事实。
_SEAM_FACTS_LIMIT = 700
# 失败详情头部（原因）的上限：头部之后紧跟保留路径，必须让路径落进执行账本
# detail 保留的前 300 字符内。
_SEAM_FAILURE_HEAD_LIMIT = 170
# 拼接门失败详情里的分段事实上限（详情有界，超出只保留头部）。
_SEAM_FACT_SEGMENTS = 4


def _verify_seam_continuity(probe: str, output: Path, seam_seconds: float, *,
                            label: str, diag: dict | None = None,
                            wide_start: float | None = None) -> dict:
    """拼接点时间戳连续性（有界窗口内可复核，不靠命令成功推断）。

    三件事都要成立（任一不满足失败关闭，并带差异事实）：
    - 探针顺序内不出现超过 1s 的大幅回退（B 帧重排容差内不算；追加段重启
      时间轴会在这里以秒级/分钟级回退暴露）；
    - 拼接点两侧都有视频包（内容覆盖拼接点，否则后段缺头部或前段缺尾部）；
    - 两侧时间戳空档不超过 2s（真实追加是帧间隔级，断流/错位会留下秒级缺口）。

    ``diag`` / ``wide_start``：把窄窗（判定用，不改）与可选的加宽窗测量写入
    调用方诊断，失败详情才有可复核事实；加宽窗只测量，不参与判定。
    """
    start = max(0.0, seam_seconds - _SEAM_HALF_WINDOW_SECONDS)
    packets = _probe_interval_packets(
        probe, output, start, window_seconds=2 * _SEAM_HALF_WINDOW_SECONDS,
        diag=diag,
    )
    times = [value for value in (_packet_time_seconds(packet)
                                 for packet in packets)
             if value is not None]
    if diag is not None:
        diag["packet_count"] = len(packets)
        diag["narrow_window"] = (round(start, 3), round(start + 2 * _SEAM_HALF_WINDOW_SECONDS, 3))
        _record_packet_time_range(diag, times)
        _record_wide_window(probe, output, diag, wide_start, seam_seconds)
    if not times:
        raise MatroskaBuildError(f"{label} 附近（{seam_seconds:.1f}s）无视频包，拒绝发布")
    for previous, current in zip(times, times[1:]):
        if current < previous - _SEAM_REORDER_TOLERANCE:
            raise MatroskaBuildError(
                f"{label} 时间戳回跳：{previous:.3f}s → {current:.3f}s，"
                f"超出重排容差 {_SEAM_REORDER_TOLERANCE:.1f}s，拒绝发布"
            )
    before = [value for value in times if value <= seam_seconds]
    after = [value for value in times if value > seam_seconds]
    first_after = min(after) if after else None
    last_before = max(before) if before else None
    if first_after is None or last_before is None:
        raise MatroskaBuildError(
            f"{label} 时间戳未覆盖拼接点（{seam_seconds:.1f}s）：前侧 "
            f"{len(before)} 个包（末 {last_before}），后侧 {len(after)} 个包"
            f"（首 {first_after}），拒绝发布"
        )
    if first_after - last_before > _SEAM_MAX_GAP_SECONDS:
        raise MatroskaBuildError(
            f"{label} 时间轴空档 {first_after - last_before:.3f}s 超限"
            f"（{last_before:.3f}s→{first_after:.3f}s，上限 "
            f"{_SEAM_MAX_GAP_SECONDS:.1f}s），拒绝发布"
        )
    return {
        "seam_seconds": round(seam_seconds, 3),
        "last_before_seconds": round(last_before, 3),
        "first_after_seconds": round(first_after, 3),
    }


def _record_packet_time_range(diag: dict, times: list[float]) -> None:
    """窄窗/加宽窗的 PTS 事实：包数、有序、首末 pts_time（无包如实记 None）。"""
    ordered = sorted(times)
    diag["packet_count"] = len(times)
    diag["packet_sorted"] = all(
        current >= previous for previous, current in zip(times, times[1:])
    ) if times else None
    diag["first_pts"] = round(ordered[0], 3) if ordered else None
    diag["last_pts"] = round(ordered[-1], 3) if ordered else None


def _record_wide_window(probe: str, output: Path, diag: dict,
                        wide_start: float | None, seam_seconds: float) -> None:
    """加宽窗（回看到本段起点）只做测量：任何失败只记事实，不替换失败原因。"""
    if wide_start is None:
        return
    start, window = _seam_wide_window(wide_start, seam_seconds)
    if start >= diag["narrow_window"][0]:
        diag["wide_window"] = "与本段起点同界（无可加宽余量）"
        return
    diag["wide_window"] = (round(start, 3), round(start + window, 3))
    try:
        packets = _probe_interval_packets(probe, output, start, window_seconds=window)
    except MatroskaBuildError as error:
        diag["wide_failure"] = _bounded_detail(error, 200)
        return
    wide_times = [value for value in (_packet_time_seconds(packet)
                                      for packet in packets)
                  if value is not None]
    diag["wide_packet_count"] = len(packets)
    diag["wide_first_pts"] = round(min(wide_times), 3) if wide_times else None
    diag["wide_last_pts"] = round(max(wide_times), 3) if wide_times else None


def _seam_wide_window(wide_start: float, seam_seconds: float) -> tuple[float, float]:
    """加宽窗 = [max(0, 回看起点), 拼接点]（有界：不长于本段自身时长）。"""
    start = max(0.0, float(wide_start))
    return start, max(0.0, seam_seconds - start)


def _seam_wide_start_seconds(seam_seconds: float, item_duration: float) -> float:
    """加宽窗回看起点：本段起点（单段自身时长有界），且不长于拼接点。"""
    return max(0.0, seam_seconds - max(0.0, float(item_duration)))


def _seam_diagnostic_text(diag: dict, context: Sequence[str]) -> str:
    """拼接门失败的可复核事实（有界、保留头部），供失败详情直接使用。"""
    parts = [str(text) for text in context if text]
    window = diag.get("narrow_window")
    parts.append(
        f"窄窗={_seam_window_text(window)} 视频包={diag.get('packet_count', 0)} "
        f"首末pts={diag.get('first_pts')}→{diag.get('last_pts')}"
        f"（有序={diag.get('packet_sorted')}）"
    )
    parts.append(
        f"加宽窗={_seam_window_text(diag.get('wide_window'))} "
        f"视频包={diag.get('wide_packet_count', 0)} "
        f"首末pts={diag.get('wide_first_pts')}→{diag.get('wide_last_pts')}"
        + (f"（测量失败：{diag['wide_failure']}）" if diag.get("wide_failure") else "")
    )
    return "；".join(parts)


def _seam_window_text(window) -> str:
    if not window:
        return "未测"
    if isinstance(window, str):
        return window
    return f"{window[0]:.1f}s→{window[1]:.1f}s"


def _seam_seek_diagnostic_text(diagnostics: dict, seams: Sequence[float]) -> str:
    """拼接点 seek 门失败的可复核事实（目标、包数、首末包、分段字节/时长）。"""
    point = diagnostics.get("point") or {}
    parts = [
        f"seek 目标={point.get('target_seconds')}s "
        f"结果={point.get('failure') or '未完成'} "
        f"视频包={point.get('packet_count')} "
        f"首包={_bounded_detail(point.get('first_packet'), 60)} "
        f"末包={_bounded_detail(point.get('last_packet'), 60)}"
    ]
    if diagnostics.get("label"):
        parts.append(f"目标标签={diagnostics['label']}")
    if diagnostics.get("seam_seconds") is None:
        parts.append("（失败点不是拼接门目标，拼接点仍按原判定拒绝发布）")
    segments = list(diagnostics.get("segments") or ())
    if segments:
        facts: list[str] = []
        for index, segment in enumerate(segments[: _SEAM_FACT_SEGMENTS]):
            path = Path(segment)
            try:
                facts.append(f"item{index} {path.stat().st_size}B {path.name}")
            except OSError:
                facts.append(f"item{index} 不可测 {path.name}")
        parts.append("分段=" + "；".join(facts))
    if seams:
        parts.append("拼接点=" + ",".join(f"{value:.1f}s" for value in seams))
    return "；".join(part for part in parts if part)


def _seam_gate_error(error: MatroskaBuildError, position: str, facts: str, *,
                     seam_index: int | None = None,
                     seam_seconds: float | None = None) -> MatroskaBuildError:
    """把测量事实与失败原因合成拼接门失败（原因在头部，事实随后）。

    测量事实随异常对象带回：build 据此保留成品并在详情里点名保留路径。
    ``head`` 只放失败本身的原因（不含异常链文本），保留路径必须紧跟它落在
    执行账本 detail 保留的前 300 字符内。
    """
    head = _bounded_detail(f"{error}{position}", _SEAM_FAILURE_HEAD_LIMIT)
    fact_text = str(facts or "").strip()
    detail = f"{head}｜{fact_text}" if fact_text else head
    failed = MatroskaBuildError(_bounded_detail(detail, _SEAM_FACTS_LIMIT))
    failed.seam_diagnostics = {
        "head": head,
        "facts": _bounded_detail(fact_text, _SEAM_FACTS_LIMIT),
        "seam_index": seam_index,
        "seam_seconds": seam_seconds,
    }
    return failed


def _seam_segment_bounds_text(bounds: tuple[tuple[int, int], ...], durations, clips,
                              seam_index: int) -> str:
    """拼接点前侧段与后侧段（play item/clip/in/out/时长/字节边界）。"""
    if seam_index >= len(bounds):
        return ""
    before_index = seam_index - 1
    after_index = seam_index
    return (
        f"前段=item{before_index} {_seam_item_text(bounds, durations, clips, before_index)}"
        f" 后段=item{after_index} {_seam_item_text(bounds, durations, clips, after_index)}"
    )


def _seam_item_text(bounds: tuple[tuple[int, int], ...], durations, clips,
                    index: int) -> str:
    clip = clips[index] if index < len(clips) else None
    start, length = bounds[index]
    clip_text = (
        f"clip={clip.clip_id} in={clip.in_time / 90000:.1f}s out={clip.out_time / 90000:.1f}s"
        if clip is not None else "clip=?"
    )
    duration_text = f"{durations[index]:.1f}s" if index < len(durations) else "?"
    return f"{clip_text} 时长={duration_text} 字节=[{start},{start + length})"


def _seam_segment_bytes_text(bounds: tuple[tuple[int, int], ...],
                             segments: Sequence[Path], seam_index: int) -> str:
    """逐段字节长度（MPLS 边界 + 分段文件实际保存的字节数，拼接点前后各两段）。"""
    first = max(1, seam_index - 2)
    last = min(len(bounds), seam_index + 1)
    facts: list[str] = []
    for index in range(first, last + 1):
        start, length = bounds[index - 1]
        path = Path(segments[index - 1]) if index - 1 < len(segments) else None
        try:
            saved = path.stat().st_size if path is not None and path.is_file() else None
        except OSError:
            saved = None
        if saved is None:
            facts.append(f"item{index - 1} 边界={length}B（分段文件不可测）")
        else:
            facts.append(f"item{index - 1} 边界={length}B 分段={saved}B")
    if not facts:
        return ""
    return "逐段字节：" + "；".join(facts)


def _seam_segment_durations_text(measured: dict[int, float], seam_index: int,
                                 *, error: str = "") -> str:
    """逐段实测时长（mkvmerge 看到的各分段）；测量失败如实记录。"""
    if error:
        return error
    facts = [
        f"item{index} {measured[index]:.1f}s"
        for index in range(max(0, seam_index - 1), min(len(measured), seam_index + 1))
        if index in measured
    ]
    if not facts:
        return ""
    return "逐段时长：" + "；".join(facts)


def _seam_declared_vs_expected_text(declared: float, expected: float) -> str:
    return f"成品声明时长={declared:.1f}s Σ分段={expected:.1f}s"





def _packet_time_seconds(packet: dict | None) -> float | None:
    """窗口判定的时间戳（秒）：优先 PTS，缺 PTS 时退到 DTS，两者都缺返回 None。

    2026-09-15 真盘实证（地球脉动 dlv_720414a5…，VC-1 1080i）：mkvmerge 成品里
    该视频轨的 packet 只有 DTS（`pts_time` 为 N/A）。只认 PTS 的窗口判定于是把
    **内容完好**的拼接点报成"附近无视频包"并拒绝发布——实测窄窗 [3065,3075] 内
    按 DTS 有 240 个视频包（24fps×10s，时间轴连续），按 PTS 为 0；同一区间的
    音频包与整片声明时长都正常。

    拼接门要判的三件事（内容覆盖拼接点、空档有界、不出现大幅回跳）用 DTS 判定
    同样成立，且 DTS 单调使回跳检查更严。**DV 路线的 PTS 对齐门不在此列**：它要
    的是解码对齐，仍只认 PTS（见 seek 门的 DV 分支），那条不变式不因本函数放松。
    """
    if not packet:
        return None
    for key in ("pts_time", "dts_time"):
        value = packet.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


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
                            diag: dict | None = None, *,
                            window_seconds: float = _SEEK_WINDOW_SECONDS) -> list[dict]:
    """读目标时间窗口内选中视频流的 packet（PTS/DTS/flags/pos）。
    子进程 rc/stderr/命令写入 diag（Codex P1-3 增量诊断）。
    ``window_seconds`` 仅改变有界窗口长度（拼接点用更短的对称窗口）。"""
    command = [
        probe, "-v", "error", "-select_streams", "v:0",
        "-read_intervals", f"{target:.3f}%+{window_seconds}",
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
        detail = _process_error_detail(error)
        if diag is not None:
            diag["packet_rc"] = getattr(error, "returncode", None)
            diag["packet_stderr"] = detail[-300:]
        raise _tool_failure("成品 seek packet 探测", error) from error
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
        detail = _process_error_detail(error)
        if diag is not None:
            diag["frame_rc"] = getattr(error, "returncode", None)
            diag["frame_stderr"] = detail[-300:]
        raise _tool_failure("成品 seek frame 探测", error) from error
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
                diag["frame_fallback_stderr"] = _process_error_detail(error)[-300:]
            # 回退探测超时同样按瞬时分类：不能让"有包无帧"的确定性判定
            # 掩盖一次超时。
            if _timeout_seconds(error) is not None:
                raise _tool_failure("成品 seek frame 回退探测", error) from error
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
        raise _tool_failure("seek 窗口合并 HEVC 提取", error) from error
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
        raise _tool_failure("成品流探测", error) from error
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
        raise _tool_failure("DV 配置记录核验", error) from error
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
        """物化主片 + ffmpeg 正在写的部分 MKV + 同槽位保留的既有成品。"""
        stream_bytes = int(asset.size)
        source = stream_bytes
        muxed_output = stream_bytes
        retained_representation = stream_bytes
        return source + muxed_output + retained_representation + _SPACE_MARGIN_BYTES

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
        if planned_segment_count(asset) >= 2:
            # ffmpeg 单输入无法按 play item 追加（见 Fifo/FfmpegDv 同类拒绝）。
            raise MatroskaBuildError(
                "多段合辑主片命中 FFmpeg-file 路线：该路线不支持按 play item "
                "分段追加，失败关闭（不输出时间轴错误的成品）"
            )
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
                if _is_out_of_space(detail):
                    raise TransientMatroskaBuildError(
                        f"ffmpeg 封装因临时空间不足中断（退出码 {code}），按瞬时故障重试："
                        f"{_bounded_detail(detail, 200)}"
                    )
                raise MatroskaBuildError(
                    f"ffmpeg 封装失败（退出码 {code}）\nffmpeg 命令：{' '.join(cmd)}\n{detail}"
                )
            if progress:
                progress(96, "统一输出校验（语言/时长/codec/分辨率/Cues/seek）")
            return _validate_ffmpeg_output(
                asset, streams, output, self.probe, dolby_vision=False,
                source_path=source, ffmpeg_executable=self.executable,
            )
        except OSError as error:
            if _is_out_of_space(error):
                raise TransientMatroskaBuildError(
                    f"FFmpeg-file 路线临时空间不足"
                    f"（{_bounded_detail(_process_error_detail(error), 200)}），按瞬时故障重试"
                ) from error
            raise
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
        """DV 保真路线的临时空间峰值上界（保守 5×）。

        生命周期项（相对主片字节）：物化源 + BL + EL + 合并视频（三者共存到
        封装前）+ 正在封装的成品 + 同槽位保留的既有成品（末次传输后 15 分钟）。
        实际合计 ≈4.65×（BL/合并视频各 ≈0.82），取 5× 作上界。
        """
        return 5 * int(asset.size) + _SPACE_MARGIN_BYTES

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
        if planned_segment_count(asset) >= 2:
            # 合并后的裸 HEVC 时间轴来自整条虚拟流，无法按 play item 追加。
            raise MatroskaBuildError(
                "多段合辑主片命中 FFmpeg DV 保真路线：该路线不支持按 play item "
                "分段追加，失败关闭（不输出时间轴错误的成品）"
            )
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
                if _is_out_of_space(detail):
                    raise TransientMatroskaBuildError(
                        f"mkvmerge DV 保真封装因临时空间不足中断（退出码 {code}），"
                        f"按瞬时故障重试：{_bounded_detail(detail, 200)}"
                    )
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
        except OSError as error:
            if _is_out_of_space(error):
                raise TransientMatroskaBuildError(
                    f"FFmpeg DV 保真路线临时空间不足"
                    f"（{_bounded_detail(_process_error_detail(error), 200)}），按瞬时故障重试"
                ) from error
            raise
        finally:
            if not self._source_checkpoint_is_valid(asset, source):
                source.unlink(missing_ok=True)
            for path in temporaries:
                path.unlink(missing_ok=True)
