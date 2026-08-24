"""Direct MPLS-title extraction for Zidoo's native transport-stream playback.

This intentionally does not remux or transcode anything.  libbluray resolves
the selected MPLS title and we persist its original M2TS byte stream as the
final representation.  The temporary ``.partial`` file is therefore the
final output itself; there is no second source copy and no mkvmerge stage.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from .bluray import BlurayTitleAsset, BlurayTitleError, iter_bluray_title
from .matroska import TransientMatroskaBuildError


class M2tsTitleBuilder:
    """Extract one selected Blu-ray MPLS title without changing its streams."""

    def build(
        self,
        asset: BlurayTitleAsset,
        output: Path,
        *,
        title: str,
        progress: Callable[[int, str], None] | None = None,
    ) -> dict:
        del title  # The container stream has no generated title metadata.
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        total = int(asset.size)
        if total <= 0:
            raise BlurayTitleError("Blu-ray 主片大小无效")

        written = 0
        last_report = 0.0
        try:
            with output.open("wb", buffering=1024 * 1024) as stream:
                for chunk in iter_bluray_title(asset, chunk_size=1024 * 1024):
                    stream.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if progress and (now - last_report >= 0.5 or written == total):
                        last_report = now
                        progress(
                            min(99, int(written * 99 / total)),
                            f"正在提取 MPLS 主片：{written / 1024**3:.1f} / "
                            f"{total / 1024**3:.1f} GB",
                        )
                stream.flush()
                os.fsync(stream.fileno())
        except BlurayTitleError as error:
            # DIST-004 P0 D.2：reader 子进程中断（含中途退出）属于瞬态传输类
            # 故障，转成 TransientMatroskaBuildError 走有限自动重试；重试期间
            # 交付保持 preparing 并展示尝试次数。reader 已从池中丢弃，重试
            # 时重新拉起干净子进程。达到预算后由上层上报 EXECUTION_FAILED。
            output.unlink(missing_ok=True)
            raise TransientMatroskaBuildError(
                f"Blu-ray 主片提取中断（可重试）：{error}"
            ) from error
        except Exception:
            output.unlink(missing_ok=True)
            raise

        if written != total:
            output.unlink(missing_ok=True)
            raise BlurayTitleError(
                f"Blu-ray 主片读取不完整：{written} / {total} 字节"
            )

        if progress:
            progress(99, "正在完成主片校验")

        return {
            "duration_ns": int(asset.duration_90k) * 1_000_000_000 // 90_000,
            # The M2TS bytes retain any Dolby Vision enhancement layer.  This
            # field is only UI metadata; do not infer it by altering streams.
            "dolby_vision": False,
            "audio_tracks": tuple(track.as_dict() for track in asset.audio_tracks),
            "subtitle_tracks": tuple(track.as_dict() for track in asset.subtitle_tracks),
        }
