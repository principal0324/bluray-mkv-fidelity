"""Persistent libbluray main-title readers used by secure DAT packaging."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator


class BlurayTitleError(RuntimeError):
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
        with self.lock:
            if not self.is_alive() or self.process.stdin is None or self.process.stdout is None:
                # DIST-004 P0 D.2：reader 子进程中断属于瞬态（可自动重试的
                # 传输类故障），由上层按 TransientMatroskaBuildError 有限重试；
                # 这里从池中丢弃，下次 get() 重新拉起干净进程。
                pool = _reader_pool()
                pool.discard(self.asset, self)
                raise BlurayTitleError(
                    "Blu-ray title reader 已退出（子进程中断，可重试）"
                )
            try:
                self.process.stdin.write(f"READ\t{offset}\t{length}\n".encode("ascii"))
                self.process.stdin.flush()
                header = self.process.stdout.readline()
            except (BrokenPipeError, OSError) as error:
                pool = _reader_pool()
                pool.discard(self.asset, self)
                raise BlurayTitleError("Blu-ray title reader 通信失败") from error
            if not header:
                # DIST-004 P0 D.1：reader 子进程中断必须带真实阶段证据（退出码），
                # 不能只留泛化"没有返回结果"。
                exit_code = self.process.poll()
                pool = _reader_pool()
                pool.discard(self.asset, self)
                raise BlurayTitleError(
                    "Blu-ray title reader 没有返回结果"
                    + (f"（子进程退出码 {exit_code}）" if exit_code is not None else "")
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
    executable = _find_executable()
    client = _reader_pool.get(executable, asset)
    offset = start
    remaining = length
    try:
        while remaining:
            request = min(chunk_size, remaining)
            yield client.read(offset, request)
            offset += request
            remaining -= request
    except Exception:
        _reader_pool.discard(asset, client)
        raise


atexit.register(clear_bluray_reader_pool)
