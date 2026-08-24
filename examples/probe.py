#!/usr/bin/env python3
"""探测蓝光内容。

用法：
    python examples/probe.py /path/to/bdmv_or_iso
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bluray_fidelity import list_main_titles, probe_main_title


def main():
    if len(sys.argv) < 2:
        print("用法: python probe.py <bdmv_or_iso_path> [--all]")
        sys.exit(1)

    source = Path(sys.argv[1]).resolve()
    all_titles = "--all" in sys.argv

    if all_titles:
        titles = list_main_titles(source)
        print(f"找到 {len(titles)} 个标题\n")
        for t in titles:
            dv = "DV" if len(t.video_tracks) > 1 else "HDR10"
            hours, rem = divmod(t.duration_90k // 90_000, 3600)
            mins, secs = divmod(rem, 60)
            print(f"  标题 {t.title_index}: MPLS {t.playlist:05d} | {hours}:{mins:02d}:{secs:02d} | {t.clip_count} 片段 | {dv}")
    else:
        asset = probe_main_title(source)
        dv = "Dolby Vision" if len(asset.video_tracks) > 1 else "HDR10"
        print(f"源:   {asset.root}")
        print(f"标题: {asset.title_index}")
        print(f"MPLS: {asset.playlist:05d}")
        print(f"格式: {dv}")
        print(f"片段: {asset.clip_count}")
        print(f"时长: {asset.duration_90k / 90_000:.1f}s")
        print(f"\n视频轨: {len(asset.video_tracks)}")
        for v in asset.video_tracks:
            print(f"  [{v.index}] PID 0x{v.pid:04x}")
        print(f"\n音频轨: {len(asset.audio_tracks)}")
        for a in asset.audio_tracks:
            print(f"  [{a.index}] PID 0x{a.pid:04x} lang={a.language}")
        print(f"\n字幕轨: {len(asset.subtitle_tracks)}")
        for s in asset.subtitle_tracks:
            print(f"  [{s.index}] PID 0x{s.pid:04x} lang={s.language}")


if __name__ == "__main__":
    main()
