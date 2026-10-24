"""Persistent libbluray main-title readers used by secure DAT packaging."""

from __future__ import annotations

import atexit
import functools
import logging
import os
import shutil
import signal
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator


class BlurayTitleError(RuntimeError):
    """Blu-ray 主片准备/读取失败。"""


class TransientBlurayReadError(BlurayTitleError):
    """**瞬态**读取故障：reader 子进程中断 / 管道通信失败 / 无返回结果。

    2026-09-16（规格 D4）分开这一类的原因：这些故障重试**有意义**（下次会
    从池里拉起一个干净进程），而**确定性**源故障（源文件有洞、全空包 PID、
    文件不存在）重试无意义、必须保持失败关闭并进入终态。此前两者都是
    `BlurayTitleError`，而 mkvmerge 路线的重试门只认
    `TransientMatroskaBuildError` ⇒ 注释里写着"可重试"的子进程中断实际零重试
    直接终态（m2ts 路线反而包成了 Transient，两条线不对称）。
    读取失败（reader 返回 NOT OK，如 `read failed`）**不在此列**：那是源事实。
    """
    """Raised when a BDMV title cannot be inspected or read safely."""


@dataclass(frozen=True)
class BlurayClip:
    """One MPLS play item in title order (clip_id + 90 kHz in/out points).

    The ordered sequence of clips is the authoritative playback identity of a
    Blu-ray title: two playlists exposing the same clip sequence (after the
    duplicate-slot normalization) are the same cut; different sequences are
    different editions/parts. Seamless branching keeps multiple clips inside a
    single title, never split into editions or parts.
    """

    index: int
    clip_id: str
    in_time: int
    out_time: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "index": self.index,
            "clip_id": self.clip_id,
            "in_time": self.in_time,
            "out_time": self.out_time,
        }


@dataclass(frozen=True)
class BlurayTrack:
    """One original Blu-ray elementary stream from the selected title."""

    index: int
    pid: int
    coding_type: int
    language: str
    # Authoritative video attributes from the HDMV stream attributes table
    # (only present on VIDEO tracks when the title reader reports them).
    # None means the field was not collected; alternate-source matching must
    # treat missing fields as incompatible (fail-closed).
    video_format: int | None = None
    frame_rate: int | None = None

    def as_dict(self) -> dict[str, int | str | None]:
        return {
            "index": self.index,
            "pid": self.pid,
            "coding_type": self.coding_type,
            "language": self.language,
            "video_format": self.video_format,
            "frame_rate": self.frame_rate,
        }


@dataclass(frozen=True)
class BlurayTitleAsset:
    root: Path
    entry_path: str
    title_index: int
    playlist: int
    duration_90k: int
    size: int
    clip_count: int
    # ``bd_open`` accepts both a mounted BDMV directory and a Blu-ray image.
    # Keep the source kind explicit so packaging and diagnostics never silently
    # fall back to the old "largest M2TS" ISO heuristic.
    source_kind: str = "bdmv"
    video_tracks: tuple[BlurayTrack, ...] = ()
    audio_tracks: tuple[BlurayTrack, ...] = ()
    subtitle_tracks: tuple[BlurayTrack, ...] = ()
    clips: tuple[BlurayClip, ...] = ()

    @property
    def dolby_vision(self) -> bool:
        """Dual primary video tracks (BL + EL) mean Dolby Vision on UHD Blu-ray.

        The enhancement layer must survive into the final MKV; a missing EL is
        a silent HDR10 downgrade and is rejected at finalization time.
        """
        return len(self.video_tracks) > 1

    @staticmethod
    def _preferred_index(tracks: tuple[BlurayTrack, ...]) -> int | None:
        for track in tracks:
            if track.language.lower() in {"zho", "chi", "zh"}:
                return track.index
        return None

    @property
    def default_audio_index(self) -> int | None:
        return self._preferred_index(self.audio_tracks)

    @property
    def default_subtitle_index(self) -> int | None:
        return self._preferred_index(self.subtitle_tracks)


_ENTRY_PATH = "HDATHOME/MAIN_TITLE.m2ts"
_MAX_RESPONSE = 64 * 1024 * 1024
_NEAR_TITLE_TOLERANCE_90K = 90_000


def _find_executable() -> str:
    configured = os.getenv("HDATHOME_BLURAY_TITLE_READER", "").strip()
    candidates = (
        configured,
        "/usr/local/bin/hdathome-bluray-title-reader",
        "/opt/hdathome/bin/hdathome-bluray-title-reader",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    discovered = shutil.which("hdathome-bluray-title-reader")
    if discovered:
        return discovered
    raise BlurayTitleError("未找到 hdathome-bluray-title-reader")


def _run_probe(root: Path) -> str:
    executable = _find_executable()
    try:
        completed = subprocess.run(
            [executable, "--probe", str(root)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as error:
        raise BlurayTitleError("Blu-ray title reader 不存在") from error
    except subprocess.TimeoutExpired as error:
        raise BlurayTitleError("Blu-ray 主片探测超时") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise BlurayTitleError(f"Blu-ray 主片探测失败：{detail or '未知错误'}") from error
    return completed.stdout


def _run_list(root: Path) -> str:
    executable = _find_executable()
    try:
        completed = subprocess.run(
            [executable, "--list", str(root)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as error:
        raise BlurayTitleError("Blu-ray title reader 不存在") from error
    except subprocess.TimeoutExpired as error:
        raise BlurayTitleError("Blu-ray 标题列表探测超时") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise BlurayTitleError(f"Blu-ray 标题列表探测失败：{detail or '未知错误'}") from error
    return completed.stdout


def _run_streams(root: Path, title_index: int) -> str:
    executable = _find_executable()
    try:
        completed = subprocess.run(
            [executable, "--streams", str(root), str(title_index)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as error:
        raise BlurayTitleError("Blu-ray title reader 不存在") from error
    except subprocess.TimeoutExpired as error:
        raise BlurayTitleError("Blu-ray 轨道探测超时") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise BlurayTitleError(f"Blu-ray 轨道探测失败：{detail or '未知错误'}") from error
    return completed.stdout


def _run_item_bounds(root: Path, title_index: int) -> str:
    executable = _find_executable()
    try:
        completed = subprocess.run(
            [executable, "--item-bounds", str(root), str(title_index)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as error:
        raise BlurayTitleError("Blu-ray title reader 不存在") from error
    except subprocess.TimeoutExpired as error:
        raise BlurayTitleError("Blu-ray play item 边界探测超时") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise BlurayTitleError(f"Blu-ray play item 边界探测失败：{detail or '未知错误'}") from error
    return completed.stdout


def _source_kind(root: Path) -> str:
    return "iso" if root.is_file() and root.suffix.lower() == ".iso" else "bdmv"


def _validate_source(root: Path) -> str:
    root = Path(root).resolve()
    source_kind = _source_kind(root)
    if source_kind == "bdmv" and not (root / "BDMV" / "index.bdmv").is_file():
        raise BlurayTitleError(f"找不到 BDMV/index.bdmv：{root}")
    if source_kind == "iso" and not root.is_file():
        raise BlurayTitleError(f"ISO 文件不存在：{root}")
    return source_kind


def _parse_title_line(root: Path, source_kind: str, line: str, prefix: str) -> BlurayTitleAsset:
    parts = line.split("\t")
    if len(parts) != 6 or parts[0] != prefix:
        raise BlurayTitleError("Blu-ray title reader 输出无效：字段数量错误")
    try:
        title_index, playlist, duration, size, clip_count = (
            int(value) for value in parts[1:]
        )
    except ValueError as error:
        raise BlurayTitleError("Blu-ray title reader 输出无效：字段不是整数") from error
    if min(title_index, playlist, duration, size, clip_count) < 0:
        raise BlurayTitleError("Blu-ray title reader 输出无效：字段为负数")
    if duration == 0 or size == 0 or clip_count == 0:
        raise BlurayTitleError("Blu-ray title reader 输出无效：主片为空")
    return BlurayTitleAsset(
        root=root,
        entry_path=_ENTRY_PATH,
        title_index=title_index,
        playlist=playlist,
        duration_90k=duration,
        size=size,
        clip_count=clip_count,
        source_kind=source_kind,
    )


def _parse_streams(output: str) -> tuple[
    tuple[BlurayTrack, ...], tuple[BlurayTrack, ...], tuple[BlurayTrack, ...],
    tuple[BlurayClip, ...],
]:
    video: list[BlurayTrack] = []
    audio: list[BlurayTrack] = []
    subtitles: list[BlurayTrack] = []
    clips: list[BlurayClip] = []
    # video index -> (video_format, frame_rate) from VINFO lines
    video_attributes: dict[int, tuple[int | None, int | None]] = {}
    for line in output.splitlines():
        if line.startswith("CLIP\t"):
            parts = line.split("\t")
            if len(parts) != 5:
                raise BlurayTitleError("Blu-ray title reader 输出无效：CLIP 字段错误")
            try:
                clip_index = int(parts[1])
                in_time = int(parts[3])
                out_time = int(parts[4])
            except ValueError as error:
                raise BlurayTitleError("Blu-ray title reader 输出无效：CLIP 字段不是整数") from error
            if clip_index < 0 or in_time < 0 or out_time < 0:
                raise BlurayTitleError("Blu-ray title reader 输出无效：CLIP 字段为负数")
            clips.append(BlurayClip(
                index=clip_index,
                clip_id=parts[2].strip(),
                in_time=in_time,
                out_time=out_time,
            ))
            continue
        if line.startswith("VINFO\t"):
            parts = line.split("\t")
            if len(parts) != 4:
                raise BlurayTitleError("Blu-ray title reader 输出无效：VINFO 字段错误")
            try:
                video_index = int(parts[1])
                video_format = int(parts[2])
                frame_rate = int(parts[3])
            except ValueError as error:
                raise BlurayTitleError("Blu-ray title reader 输出无效：VINFO 字段不是整数") from error
            if video_index < 0:
                raise BlurayTitleError("Blu-ray title reader 输出无效：VINFO 字段为负数")
            # frame_rate==0 表示该 libbluray 头版本未暴露精确帧率字段；
            # 保持 None 使签名不完整（fail-closed），不参与备用源合并。
            video_attributes[video_index] = (
                video_format,
                frame_rate if frame_rate > 0 else None,
            )
            continue
        if not line.startswith("STREAM\t"):
            continue
        parts = line.split("\t")
        if len(parts) != 6 or parts[1] not in {"VIDEO", "AUDIO", "SUBTITLE"}:
            raise BlurayTitleError("Blu-ray title reader 输出无效：轨道字段错误")
        try:
            track = BlurayTrack(
                index=int(parts[2]),
                pid=int(parts[3]),
                coding_type=int(parts[4]),
                language=parts[5].strip().lower(),
            )
        except ValueError as error:
            raise BlurayTitleError("Blu-ray title reader 输出无效：轨道字段不是整数") from error
        if track.index < 0 or track.pid < 0 or track.coding_type < 0:
            raise BlurayTitleError("Blu-ray title reader 输出无效：轨道字段为负数")
        if parts[1] == "VIDEO":
            video_format, frame_rate = video_attributes.get(track.index, (None, None))
            video.append(replace(track, video_format=video_format, frame_rate=frame_rate))
        elif parts[1] == "AUDIO":
            audio.append(track)
        else:
            subtitles.append(track)
    return tuple(video), tuple(audio), tuple(subtitles), tuple(clips)


def _parse_item_bounds(output: str) -> tuple[tuple[int, int], ...]:
    """解析 ITEM 行；区间必须按 MPLS 顺序首尾相接，否则 fail closed。"""
    bounds: list[tuple[int, int]] = []
    for line in output.splitlines():
        if not line.startswith("ITEM\t"):
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise BlurayTitleError("Blu-ray title reader 输出无效：ITEM 字段错误")
        try:
            index = int(parts[1])
            start = int(parts[2])
            length = int(parts[3])
        except ValueError as error:
            raise BlurayTitleError("Blu-ray title reader 输出无效：ITEM 字段不是整数") from error
        # 序号必须连续、长度必须为正且相邻区间不得重叠或留空隙：
        # 任何一个 play item 边界错位都会让后续所有分段的字节失去意义。
        if index != len(bounds) or start < 0 or length <= 0:
            raise BlurayTitleError("Blu-ray title reader 输出无效：ITEM 顺序或范围错误")
        if bounds and start != bounds[-1][0] + bounds[-1][1]:
            raise BlurayTitleError("Blu-ray title reader 输出无效：ITEM 区间不连续")
        bounds.append((start, length))
    if not bounds:
        raise BlurayTitleError("Blu-ray title reader 输出无效：没有 play item")
    if bounds[0][0] != 0:
        raise BlurayTitleError("Blu-ray title reader 输出无效：ITEM 起点不为 0")
    return tuple(bounds)


@functools.lru_cache(maxsize=16)
def _item_bounds_cached(root: str, title_index: int) -> tuple[tuple[int, int], ...]:
    return _parse_item_bounds(_run_item_bounds(Path(root), title_index))


def bluray_item_bounds(asset: BlurayTitleAsset) -> tuple[tuple[int, int], ...]:
    """每个 MPLS play item 在标题虚拟字节流中的 (start, length)，按 MPLS 顺序。

    同一 clip 在 MPLS 中出现 K 次就得到 K 段独立区间（多 clip 连放/无缝分支
    按 play item 逐段落地的依据）。边界由 reader 端 bd_seek_playitem 与
    MPLS 包划分交叉校验，这里再确认区间总数与 asset 的 clip_count/size 一致；
    不一致说明 reader 与 asset 不是同一份标题状态，直接 fail closed。
    """
    bounds = _item_bounds_cached(str(asset.root), asset.title_index)
    if len(bounds) != asset.clip_count:
        raise BlurayTitleError(
            f"Blu-ray play item 数量不符：{len(bounds)} != {asset.clip_count}"
        )
    if sum(length for _, length in bounds) != asset.size:
        raise BlurayTitleError("Blu-ray play item 区间总长与标题大小不符")
    return bounds


def _parse_selected_output(root: Path, source_kind: str, output: str) -> BlurayTitleAsset:
    selected = next(
        (line for line in output.splitlines() if line.startswith("SELECTED\t")),
        None,
    )
    if selected is None:
        raise BlurayTitleError("Blu-ray title reader 输出无效：缺少 SELECTED")
    asset = _parse_title_line(root, source_kind, selected, "SELECTED")
    video_tracks, audio_tracks, subtitle_tracks, clips = _parse_streams(output)
    return replace(
        asset,
        video_tracks=video_tracks,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        clips=clips,
    )


def _with_streams(root: Path, asset: BlurayTitleAsset) -> BlurayTitleAsset:
    video_tracks, audio_tracks, subtitle_tracks, clips = _parse_streams(
        _run_streams(root, asset.title_index)
    )
    return replace(
        asset,
        video_tracks=video_tracks,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        clips=clips,
    )


def _track_preference(asset: BlurayTitleAsset) -> tuple[int, int, int]:
    """Prefer Chinese-capable variants, then a stable lower MPLS number."""
    chinese_audio = asset.default_audio_index is not None
    chinese_subtitle = asset.default_subtitle_index is not None
    return (
        int(chinese_audio) + int(chinese_subtitle),
        int(chinese_audio),
        -asset.playlist,
    )


def _prefer_near_equal_chinese_title(
    root: Path,
    selected: BlurayTitleAsset,
) -> BlurayTitleAsset:
    """Resolve near-identical MPLS variants without changing stream bytes.

    Some discs expose several playlists whose durations differ by less than a
    frame but whose language sets differ. The longest-only rule can therefore
    select a foreign-language branch. Only near-equal candidates participate;
    genuinely different cuts remain untouched.
    """
    try:
        candidates = list_main_titles(root)
    except BlurayTitleError:
        return selected
    longest = max(candidate.duration_90k for candidate in candidates)
    near = [
        candidate
        for candidate in candidates
        if longest - candidate.duration_90k <= _NEAR_TITLE_TOLERANCE_90K
    ]
    if len(near) <= 1:
        return selected

    inspected: list[BlurayTitleAsset] = []
    for candidate in near:
        if candidate.title_index == selected.title_index:
            inspected.append(selected)
            continue
        try:
            inspected.append(_with_streams(root, candidate))
        except BlurayTitleError:
            inspected.append(candidate)
    preferred = max(inspected, key=_track_preference)
    if _track_preference(preferred) > _track_preference(selected):
        return preferred
    return selected


def list_main_titles(root: Path) -> list[BlurayTitleAsset]:
    root = Path(root).resolve()
    source_kind = _validate_source(root)
    candidates = [
        _parse_title_line(root, source_kind, line, "TITLE")
        for line in _run_list(root).splitlines()
        if line.startswith("TITLE\t")
    ]
    if not candidates:
        raise BlurayTitleError("Blu-ray title reader 输出无效：没有可用标题")
    return candidates


def probe_main_title(
    root: Path,
    *,
    playlist: int | None = None,
    title_index: int | None = None,
) -> BlurayTitleAsset:
    root = Path(root).resolve()
    source_kind = _validate_source(root)
    if playlist is not None or title_index is not None:
        candidates = list_main_titles(root)
        matches = [
            candidate for candidate in candidates
            if (playlist is None or candidate.playlist == playlist)
            and (title_index is None or candidate.title_index == title_index)
        ]
        if not matches:
            raise BlurayTitleError(
                f"找不到手动指定的 MPLS：playlist={playlist}, title_index={title_index}"
            )
        selected = matches[0]
        try:
            output = _run_streams(root, selected.title_index)
        except BlurayTitleError:
            # Older helpers can still select a title; metadata is additive and
            # must not make an otherwise valid package unreadable.
            return selected
        video_tracks, audio_tracks, subtitle_tracks, clips = _parse_streams(output)
        return replace(
            selected,
            video_tracks=video_tracks,
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
            clips=clips,
        )
    output = _run_probe(root)
    selected = _parse_selected_output(root, source_kind, output)
    return _prefer_near_equal_chinese_title(root, selected)


def _pdeathsig_preexec():
    """子进程 preexec：设置 PR_SET_PDEATHSIG(SIGKILL)。

    DIST-010 WP-B：父进程（Executor 容器）无论正常退出、崩溃还是被
    kill -9 / docker stop 强杀，内核都会自动 SIGKILL reader 进程，
    根除生产现场观测到的"成品后 reader 孤儿运行数小时"问题。
    atexit 只覆盖正常退出，无法处理 SIGKILL；PDEATHSIG 是内核级保证。
    """
    import signal
    import ctypes

    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        # 非 Linux / prctl 不可用：退回进程组终止（close() 仍覆盖正常路径）
        pass


class _ReaderClient:
    def __init__(self, executable: str, asset: BlurayTitleAsset):
        self.asset = asset
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            [executable, "--serve", str(asset.root), str(asset.title_index)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=(os.name == "posix"),
            preexec_fn=(_pdeathsig_preexec if os.name == "posix" else None),
        )

    def is_alive(self) -> bool:
        return self.process.poll() is None

    @staticmethod
    def _read_exact(stream, length: int) -> bytes:
        output = bytearray()
        while len(output) < length:
            block = stream.read(length - len(output))
            if not block:
                break
            output.extend(block)
        return bytes(output)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or length > _MAX_RESPONSE:
            raise BlurayTitleError("Blu-ray 主片读取范围无效")
        if offset + length > self.asset.size:
            raise BlurayTitleError("Blu-ray 主片读取超出结尾")
        return self._exchange(f"READ\t{offset}\t{length}\n".encode("ascii"), length)

    def read_item(self, item_index: int, offset_in_item: int, length: int) -> bytes:
        """读取单个 play item 内的一段字节（偏移相对该 item 起点）。"""
        if item_index < 0 or offset_in_item < 0 or length < 0 or length > _MAX_RESPONSE:
            raise BlurayTitleError("Blu-ray play item 读取范围无效")
        bounds = bluray_item_bounds(self.asset)
        if item_index >= len(bounds):
            raise BlurayTitleError(f"Blu-ray play item 序号越界：{item_index}")
        item_length = bounds[item_index][1]
        # 越出本段 item 的读取会读到下一段（或标题结尾之外）的字节，
        # 属于打包事故而不是可重试的传输故障，必须在发出请求前拒绝。
        if offset_in_item > item_length or length > item_length - offset_in_item:
            raise BlurayTitleError(
                f"Blu-ray play item 读取超出结尾：{offset_in_item}+{length}"
                f" > {item_length}"
            )
        return self._exchange(
            f"READITEM\t{item_index}\t{offset_in_item}\t{length}\n".encode("ascii"),
            length,
        )

    def _exchange(self, request: bytes, length: int) -> bytes:
        with self.lock:
            if not self.is_alive() or self.process.stdin is None or self.process.stdout is None:
                # DIST-004 P0 D.2：reader 子进程中断属于瞬态（可自动重试的
                # 传输类故障），由上层按 TransientMatroskaBuildError 有限重试；
                # 这里从池中丢弃，下次 get() 重新拉起干净进程。
                pool = _reader_pool()
                pool.discard(self.asset, self)
                raise TransientBlurayReadError(
                    "Blu-ray title reader 已退出（子进程中断，可重试）"
                )
            try:
                self.process.stdin.write(request)
                self.process.stdin.flush()
                header = self.process.stdout.readline()
            except (BrokenPipeError, OSError) as error:
                pool = _reader_pool()
                pool.discard(self.asset, self)
                raise TransientBlurayReadError(
                    "Blu-ray title reader 通信失败（可重试）") from error
            if not header:
                # DIST-004 P0 D.1：reader 子进程中断必须带真实阶段证据（退出码），
                # 不能只留泛化"没有返回结果"。
                exit_code = self.process.poll()
                pool = _reader_pool()
                pool.discard(self.asset, self)
                raise TransientBlurayReadError(
                    "Blu-ray title reader 没有返回结果（子进程已退出，可重试）"
                    + (f"（退出码 {exit_code}）" if exit_code is not None else "")
                )
            parts = header.rstrip(b"\n").split(b"\t", 1)
            if len(parts) != 2 or parts[0] != b"OK":
                detail = parts[-1].decode("utf-8", errors="replace")
                raise BlurayTitleError(f"Blu-ray title reader 读取失败：{detail}")
            try:
                response_length = int(parts[1])
            except ValueError as error:
                raise BlurayTitleError("Blu-ray title reader 返回长度无效") from error
            if response_length != length or response_length > _MAX_RESPONSE:
                raise BlurayTitleError(
                    f"Blu-ray title reader 返回长度不符：{response_length} != {length}"
                )
            payload = self._read_exact(self.process.stdout, response_length)
            if len(payload) != response_length:
                raise BlurayTitleError(
                    f"Blu-ray title reader 输出不足：{len(payload)} != {response_length}"
                )
            return payload

    def close(self) -> None:
        process = self.process
        try:
            if process.poll() is None and process.stdin is not None:
                try:
                    process.stdin.write(b"QUIT\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


class _ReaderPool:
    def __init__(self, max_entries: int = 4):
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, int], _ReaderClient] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def _key(asset: BlurayTitleAsset) -> tuple[str, int]:
        return str(asset.root.resolve()), asset.title_index

    def get(self, executable: str, asset: BlurayTitleAsset) -> _ReaderClient:
        key = self._key(asset)
        with self._lock:
            client = self._entries.get(key)
            if client is not None and client.is_alive():
                self._entries.move_to_end(key)
                return client
            if client is not None:
                client.close()
                self._entries.pop(key, None)
            client = _ReaderClient(executable, asset)
            self._entries[key] = client
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                _old_key, old_client = self._entries.popitem(last=False)
                old_client.close()
            return client

    def discard(self, asset: BlurayTitleAsset, client: _ReaderClient) -> None:
        key = self._key(asset)
        with self._lock:
            current = self._entries.get(key)
            if current is client:
                self._entries.pop(key, None)
                client.close()

    def clear(self) -> None:
        with self._lock:
            clients = list(self._entries.values())
            self._entries.clear()
        for client in clients:
            client.close()


_reader_pool = _ReaderPool()


def clear_bluray_reader_pool() -> None:
    # play item 边界缓存与 reader 进程同生命周期：进程池清空后重新探测，
    # 避免测试或换盘后继续用旧标题的边界。
    _item_bounds_cached.cache_clear()
    _reader_pool.clear()


def iter_bluray_title(
    asset: BlurayTitleAsset,
    start: int = 0,
    length: int | None = None,
    chunk_size: int = 65536,
) -> Iterator[bytes]:
    if start < 0 or start > asset.size:
        raise BlurayTitleError("Blu-ray 主片读取起点越界")
    if length is None:
        length = asset.size - start
    if length < 0 or start + length > asset.size:
        raise BlurayTitleError("Blu-ray 主片读取范围越界")
    if chunk_size <= 0 or chunk_size > _MAX_RESPONSE:
        raise BlurayTitleError("Blu-ray 主片读取块大小无效")
    plan = _direct_title_plan(asset)
    if plan is not None:
        yield from _iter_direct_range(
            asset, plan[0], start=start, length=length, chunk_size=chunk_size)
        return
    executable = _find_executable()
    client = _reader_pool.get(executable, asset)
    offset = start
    remaining = length
    _require_visible_pid(
        f"主片（libbluray 读取 {asset.root.name}）", asset.size,
        lambda at, size: client.read(at, size))
    try:
        while remaining:
            request = min(chunk_size, remaining)
            yield client.read(offset, request)
            offset += request
            remaining -= request
    except Exception:
        _reader_pool.discard(asset, client)
        raise


def iter_bluray_item(
    asset: BlurayTitleAsset,
    item_index: int,
    chunk_size: int = 1 << 20,
) -> Iterator[bytes]:
    """按 play item 顺序产出一个 item 的字节；item_index 越界必须抛错（fail closed）。

    2026-09-14（dlv_eb87606f… 霸王别姬）：能证明"该 item 恰好覆盖整个 clip
    文件"时走**直读**（BDMV 直接读文件 / ISO 走镜像内 UDF），不再经 libbluray
    的 title 读层——该读层在这张盘的正片 playlist 上把每个 TS 包的 PID 字段
    改写成 0x1FFF（见 udf_media 模块头）。证明不了等价的 item（按时间裁剪的
    多段合辑）继续走原路径：未实现的能力失败关闭，绝不近似。
    """
    bounds = bluray_item_bounds(asset)
    if item_index < 0 or item_index >= len(bounds):
        raise BlurayTitleError(f"Blu-ray play item 序号越界：{item_index}")
    if chunk_size <= 0 or chunk_size > _MAX_RESPONSE:
        raise BlurayTitleError("Blu-ray 主片读取块大小无效")
    plan = _direct_item_plan(asset)
    if plan is not None:
        yield from _iter_direct_item_checked(asset, plan, item_index, chunk_size)
        return
    executable = _find_executable()
    client = _reader_pool.get(executable, asset)
    offset = 0
    remaining = bounds[item_index][1]
    _require_visible_pid(
        f"主片第 {item_index + 1} 段（libbluray 读取 {asset.root.name}）",
        remaining,
        lambda at, size: client.read_item(item_index, at, size))
    try:
        while remaining:
            request = min(chunk_size, remaining)
            yield client.read_item(item_index, offset, request)
            offset += request
            remaining -= request
    except Exception:
        _reader_pool.discard(asset, client)
        raise


# ---- 直读（BDMV 文件 / ISO 内 UDF）：绕开 libbluray 的 title 读层 ----

# 直读计划按（素材, title）缓存：clip 长度只在素材被替换时改变，同一构建内
# 反复构造计划等于反复打开 ISO（昂贵），重复判定也无意义。None 同样缓存
# ——"不可直读"是构造期确定的事实，不是每次读取都要重算的。
_DIRECT_PLAN_CACHE: dict[tuple[str, int], tuple | None] = {}
_DIRECT_PLAN_LOCK = threading.Lock()
_direct_logger = logging.getLogger("hdathome.bluray")


def _direct_item_plan(asset: BlurayTitleAsset) -> tuple | None:
    """该主片的直读计划（逐 item 可证明等价）；不成立返回 None。"""
    from . import udf_media

    key = (str(asset.root), int(asset.title_index))
    with _DIRECT_PLAN_LOCK:
        if key in _DIRECT_PLAN_CACHE:
            return _DIRECT_PLAN_CACHE[key]
    plan = None
    try:
        if asset.clips:
            plan = udf_media.build_direct_plan(
                asset.root, asset.source_kind, asset.clips,
                bluray_item_bounds(asset),
            )
    except Exception:
        _direct_logger.warning("直读计划构造失败，本次回落 libbluray 读取：%s",
                               asset.root, exc_info=False)
        plan = None
    with _DIRECT_PLAN_LOCK:
        _DIRECT_PLAN_CACHE[key] = plan
    return plan


def _direct_title_plan(asset: BlurayTitleAsset) -> tuple | None:
    """整片直读计划：仅当主片就是单个未裁剪 clip（等价可证）时返回。"""
    plan = _direct_item_plan(asset)
    if plan is not None and len(plan) == 1 and plan[0].length == asset.size:
        return plan
    return None


def _direct_read_at(asset: BlurayTitleAsset, clip_id: str, at: int, size: int) -> bytes:
    from . import udf_media

    reader = udf_media.clip_reader(asset.root, asset.source_kind, clip_id)
    try:
        return reader.read_at(at, size)
    finally:
        reader.close()


def _iter_direct_range(asset: BlurayTitleAsset, item, *, start: int, length: int,
                       chunk_size: int) -> Iterator[bytes]:
    """主片区间（item 内偏移 start..start+length）的直读。"""
    from . import udf_media

    offset_in_item = start - item.offset
    if offset_in_item < 0 or offset_in_item + length > item.length:
        raise BlurayTitleError("直读范围越界（item 覆盖不足）")
    label = (f"主片直读（{asset.source_kind} {item.clip_id}.m2ts，"
             f"item 覆盖整段 {item.length} 字节）")
    # 整个区间只开一次素材句柄：ISO 的每次打开都要解析 UDF 卷，按 chunk 反复
    # 打开在 100 GB 级素材上会变成数万次卷解析（纯开销，不是读盘）。
    reader = udf_media.clip_reader(asset.root, asset.source_kind, item.clip_id)
    try:
        _require_visible_pid(
            label, length,
            lambda at, size: reader.read_at(min(offset_in_item + at,
                                                max(0, item.length - 1)), size))
        offset = offset_in_item
        remaining = length
        while remaining:
            request = min(chunk_size, remaining)
            data = reader.read_at(offset, request)
            if not data:
                raise udf_media.DirectReadError(
                    f"直读提前结束：{item.clip_id} offset={offset}")
            chunk = data[:request]
            yield chunk
            offset += len(chunk)
            remaining -= len(chunk)
    except udf_media.DirectReadError as error:
        raise BlurayTitleError(str(error)) from error
    finally:
        reader.close()


def _iter_direct_item_checked(asset: BlurayTitleAsset, plan: tuple,
                              item_index: int, chunk_size: int) -> Iterator[bytes]:
    """单个 item 的直读（含空包 PID 加固判定 + 源损伤就地补空包）。"""
    from . import udf_media

    item = plan[item_index]
    label = (f"主片第 {item_index + 1} 段直读"
             f"（{asset.source_kind} {item.clip_id}.m2ts）")
    try:
        _require_visible_pid(
            label, item.length,
            lambda at, size: _direct_read_at(asset, item.clip_id, at, size))
        # 源损伤处置（2026-09-16 用户裁定「3 帧以内都修」）：整包对齐的失同步
        # 槽位就地改写成合法空包，长度守恒；超预算 / 无锚点 / ATS 不可用一律
        # 失败关闭（绝不静默放行）。修补证据经 on_repair 落日志并累积到本素材
        # 的清单，供交付面（D3）消费。
        yield from udf_media.normalize_packet_damage(
            udf_media.iter_direct_item(
                plan, item_index, chunk_size,
                root=asset.root, source_kind=asset.source_kind),
            clip_id=item.clip_id,
            on_repair=lambda damage: _record_source_damage(asset, damage))
    except udf_media.DirectReadError as error:
        raise BlurayTitleError(str(error)) from error


# 本素材本次构建内已修补的源损伤（D3 证据面：交付 manifest / 控制台可见）。
# 键 = (root, title_index)，值 = 该素材的 PacketDamage 列表。
_SOURCE_DAMAGE: dict[tuple[str, int], list] = {}
_SOURCE_DAMAGE_LOCK = threading.Lock()


def _record_source_damage(asset: BlurayTitleAsset, damage) -> None:
    key = (str(asset.root), int(asset.title_index))
    with _SOURCE_DAMAGE_LOCK:
        _SOURCE_DAMAGE.setdefault(key, []).append(damage)


def source_damage_records(asset: BlurayTitleAsset) -> list:
    """该素材本次构建内修补过的源损伤（空列表 = 没有洞，或没走直读路径）。"""
    key = (str(asset.root), int(asset.title_index))
    with _SOURCE_DAMAGE_LOCK:
        return list(_SOURCE_DAMAGE.get(key, ()))


def _require_visible_pid(label: str, length: int, read_at) -> None:
    """读路径加固：采样窗口全为空包 PID → 失败关闭并点名（霸王别姬的签名）。

    只否决"**每个**采样窗口的**每个**采样包都是空包"：真实流可以有整段空包
    填充，但不会所有窗口都如此。全空包意味着下游只会拿到 mkvmerge/ffmpeg 的
    天书报错，必须在取字节这一步失败并把读法与窗口写进错误（2026-09-14 实证：
    同一张盘同一 playlist，libbluray 读路径给出 0x1FFF ×508）。
    """
    from . import udf_media

    sample = udf_media.PID_WINDOW_PACKETS * udf_media.PACKET_SIZE
    if length <= 0:
        return
    try:
        first = read_at(0, min(sample, length))
    except Exception:
        return  # 采样失败不改变原读取语义：真正的读取错误由读取路径本身报出
    windows = [(f"{label} 头 {len(first)} 字节", first)]
    if length > 4 * sample and udf_media.null_pid_ratio(first) >= 1.0:
        mid = length // 2
        mid -= mid % udf_media.PACKET_SIZE
        try:
            windows.append((f"{label} 中段 offset={mid}", read_at(mid, sample)))
        except Exception:
            pass
    try:
        udf_media.require_visible_pid(windows)
    except udf_media.DirectReadError as error:
        raise BlurayTitleError(str(error)) from error


# DIST-010 WP-B：启动时清扫陈旧 reader 进程（上代崩溃残留）
_READER_PID_FILE = "bluray-reader-pids.json"


def _reader_pid_file_path() -> Path:
    from ...config import settings

    return settings.executor_data_dir / _READER_PID_FILE


def _save_reader_pids(pids: set[int]) -> None:
    import json as _json

    path = _reader_pid_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(sorted(pids)), encoding="utf-8")


def cleanup_stale_readers() -> int:
    """启动时终止上代残留的 reader 进程。

    PDEATHSIG 在正常情况下已保证随父进程死亡；此函数兜底处理：
    - 容器内 PID 复用导致 PDEATHSIG 打到错误进程的极端场景；
    - PDEATHSIG 不可用平台（非 Linux）的崩溃残留。
    只杀 PID 文件中记录的进程；不扫描全局进程表（不误杀其他服务）。
    返回清理数。
    """
    import json as _json

    path = _reader_pid_file_path()
    if not path.exists():
        return 0
    try:
        pids = {int(p) for p in _json.loads(path.read_text("utf-8"))}
    except (ValueError, OSError):
        return 0
    cleaned = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            cleaned += 1
        except (ProcessLookupError, PermissionError):
            pass  # 已死或非本用户
    _save_reader_pids(set())
    return cleaned


atexit.register(clear_bluray_reader_pool)
