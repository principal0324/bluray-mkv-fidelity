#!/usr/bin/env python3
"""将蓝光内容封装为 MKV。

用法：
    python examples/finalize.py /path/to/bdmv_or_iso -o output.mkv
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bluray_fidelity import probe_main_title, MatroskaBuilder


def main():
    if len(sys.argv) < 4:
        print("用法: python finalize.py <bdmv_or_iso> -o <output.mkv>")
        sys.exit(1)

    source = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[sys.argv.index("-o") + 1]).resolve()

    try:
        asset = probe_main_title(source)
    except Exception as e:
        print(f"探测失败: {e}")
        sys.exit(1)

    dv = "Dolby Vision" if len(asset.video_tracks) > 1 else "HDR10"
    print(f"源:   {source}")
    print(f"标题: MPLS {asset.playlist:05d}")
    print(f"格式: {dv}")
    print(f"输出: {output}")
    print()

    def progress(pct, msg):
        print(f"  [{pct:3d}%] {msg}")

    builder = MatroskaBuilder()
    try:
        result = builder.build(asset, output, progress=progress)
        print(f"\n完成: {output}")
        if result.get("dolby_vision"):
            print("Dolby Vision: 已确认 (BL+EL+RPU)")
    except Exception as e:
        print(f"\n失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
