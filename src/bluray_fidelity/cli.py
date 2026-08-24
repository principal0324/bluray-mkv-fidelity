"""Command-line interface for bluray-mkv-fidelity."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from .bluray import (
    BlurayTitleAsset,
    BlurayTitleError,
    list_main_titles,
    probe_main_title,
)
from .matroska import (
    MatroskaBuilder,
    MatroskaBuildError,
    TransientMatroskaBuildError,
)


def _find_tool(name: str) -> str | None:
    """Locate an external tool on PATH or common install locations."""
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/usr/local/bin", "/opt/hdathome/bin"):
        candidate = Path(prefix) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _check_tools() -> list[str]:
    """Verify required external tools are available. Return list of missing."""
    required = {
        "mkvmerge": "MKVToolNix (mkvmerge)",
        "ffmpeg": "FFmpeg (ffmpeg)",
        "ffprobe": "FFmpeg (ffprobe)",
    }
    optional = {
        "dovi_tool": "dovi_tool (required for Dolby Vision content)",
        "hdathome-bluray-title-reader": "bluray-title-reader (native C++ helper)",
    }
    missing = []
    for tool, desc in required.items():
        if not _find_tool(tool):
            missing.append(f"  REQUIRED: {tool} — {desc}")
    for tool, desc in optional.items():
        if not _find_tool(tool):
            missing.append(f"  optional: {tool} — {desc}")
    return missing


def _format_duration(duration_90k: int) -> str:
    """Format a 90kHz duration into HH:MM:SS."""
    seconds = duration_90k // 90_000
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_size(size: int) -> str:
    """Format byte count into human-readable size."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"


def cmd_probe(args: argparse.Namespace) -> int:
    """Probe a Blu-ray source and display title information."""
    root = Path(args.source).resolve()
    try:
        if args.all_titles:
            titles = list_main_titles(root)
            print(f"Found {len(titles)} title(s) in {root}\n")
            for t in titles:
                dv = "DV" if len(t.video_tracks) > 1 else "HDR10"
                print(
                    f"  Title {t.title_index}: MPLS {t.playlist:05d} "
                    f"| {_format_duration(t.duration_90k)} "
                    f"| {_format_size(t.size)} "
                    f"| {t.clip_count} clip(s) "
                    f"| {dv}"
                )
                if t.video_tracks:
                    print(f"    Video: {len(t.video_tracks)} track(s)")
                if t.audio_tracks:
                    langs = ", ".join(
                        f"{a.language}(0x{a.pid:04x})" for a in t.audio_tracks
                    )
                    print(f"    Audio: {langs}")
                if t.subtitle_tracks:
                    langs = ", ".join(
                        f"{s.language}(0x{s.pid:04x})" for s in t.subtitle_tracks
                    )
                    print(f"    Subtitle: {langs}")
                print()
        else:
            asset = probe_main_title(root)
            dv = "Dolby Vision" if len(asset.video_tracks) > 1 else "HDR10"
            print(f"Source:     {root}")
            print(f"Title:      {asset.title_index}")
            print(f"Playlist:   {asset.playlist:05d}")
            print(f"Duration:   {_format_duration(asset.duration_90k)}")
            print(f"Size:       {_format_size(asset.size)}")
            print(f"Clips:      {asset.clip_count}")
            print(f"Format:     {dv}")
            print(f"Source:     {asset.source_kind}")
            print()
            if asset.video_tracks:
                print(f"Video tracks: {len(asset.video_tracks)}")
                for v in asset.video_tracks:
                    fmt = f"format={v.video_format}" if v.video_format is not None else ""
                    rate = f"rate={v.frame_rate}" if v.frame_rate is not None else ""
                    print(f"  [{v.index}] PID 0x{v.pid:04x} {fmt} {rate}")
            if asset.audio_tracks:
                print(f"\nAudio tracks: {len(asset.audio_tracks)}")
                for a in asset.audio_tracks:
                    print(f"  [{a.index}] PID 0x{a.pid:04x} lang={a.language}")
            if asset.subtitle_tracks:
                print(f"\nSubtitle tracks: {len(asset.subtitle_tracks)}")
                for s in asset.subtitle_tracks:
                    print(f"  [{s.index}] PID 0x{s.pid:04x} lang={s.language}")
            if asset.clips:
                print(f"\nClips: {len(asset.clips)}")
                for c in asset.clips:
                    in_s = c.in_time / 90_000
                    out_s = c.out_time / 90_000
                    print(
                        f"  [{c.index}] {c.clip_id} "
                        f"{in_s:.3f}s — {out_s:.3f}s"
                    )
    except BlurayTitleError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    """Finalize a Blu-ray source into a lossless MKV."""
    root = Path(args.source).resolve()
    output = Path(args.output).resolve()

    if output.exists() and not args.force:
        print(f"Error: output file already exists: {output}", file=sys.stderr)
        print("Use --force to overwrite.", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        asset = probe_main_title(
            root,
            playlist=args.playlist,
            title_index=args.title_index,
        )
    except BlurayTitleError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    dv = "Dolby Vision" if len(asset.video_tracks) > 1 else "HDR10"
    print(f"Source:   {root}")
    print(f"Title:    MPLS {asset.playlist:05d} ({asset.title_index})")
    print(f"Duration: {_format_duration(asset.duration_90k)}")
    print(f"Size:     {_format_size(asset.size)}")
    print(f"Format:   {dv}")
    print(f"Output:   {output}")
    print()

    builder = MatroskaBuilder(
        executable=args.mkvmerge or "mkvmerge",
        ffmpeg=args.ffmpeg or "ffmpeg",
        dovi_tool=args.dovi_tool or "dovi_tool",
        probe=args.ffprobe or "ffprobe",
        dv_profile=args.dv_profile,
    )

    def progress(pct: int, msg: str) -> None:
        print(f"  [{pct:3d}%] {msg}")

    t0 = time.monotonic()
    max_retries = 2 if args.retry else 0
    attempt = 0

    while attempt <= max_retries:
        try:
            result = builder.build(
                asset,
                output,
                title=f"{asset.title_index}:{asset.playlist:05d}",
                progress=progress,
            )
            elapsed = time.monotonic() - t0
            print(f"\nDone in {elapsed:.1f}s")
            print(f"Output: {output} ({_format_size(output.stat().st_size)})")
            if result.get("dolby_vision"):
                print("Dolby Vision: confirmed (BL+EL+RPU)")
            return 0
        except TransientMatroskaBuildError as error:
            attempt += 1
            if attempt > max_retries:
                print(f"\nFailed after {attempt} attempt(s): {error}", file=sys.stderr)
                return 1
            print(f"  Transient error (attempt {attempt}/{max_retries + 1}): {error}")
            print("  Retrying...")
        except MatroskaBuildError as error:
            print(f"\nFailed: {error}", file=sys.stderr)
            return 1

    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify an existing MKV against its Blu-ray source."""
    mkv_path = Path(args.mkv).resolve()
    source_path = Path(args.source).resolve()

    if not mkv_path.is_file():
        print(f"Error: MKV file not found: {mkv_path}", file=sys.stderr)
        return 1

    try:
        asset = probe_main_title(source_path)
    except BlurayTitleError as error:
        print(f"Error probing source: {error}", file=sys.stderr)
        return 1

    print(f"MKV:     {mkv_path} ({_format_size(mkv_path.stat().st_size)})")
    print(f"Source:  {source_path}")
    print(f"Title:   MPLS {asset.playlist:05d}")
    print()

    # Run ffprobe on the MKV to check basic properties
    probe = args.ffprobe or "ffprobe"
    probe_bin = _find_tool(probe)
    if not probe_bin:
        print(f"Error: {probe} not found", file=sys.stderr)
        return 1

    import json as json_mod
    import subprocess

    try:
        completed = subprocess.run(
            [
                probe_bin, "-v", "error",
                "-show_entries", "format=duration",
                "-show_entries", "stream=codec_type,codec_name",
                "-show_entries", "stream_side_data",
                "-of", "json",
                str(mkv_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        payload = json_mod.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json_mod.JSONDecodeError) as error:
        print(f"Error running ffprobe: {error}", file=sys.stderr)
        return 1

    # Check duration
    duration_s = float(
        (payload.get("format") or {}).get("duration") or 0
    )
    expected_s = asset.duration_90k / 90_000
    if duration_s > 0:
        diff = abs(duration_s - expected_s)
        status = "OK" if diff <= 2.0 else "MISMATCH"
        print(f"Duration:  {duration_s:.1f}s (expected {expected_s:.1f}s) [{status}]")
    else:
        print(f"Duration:  unable to determine (expected {expected_s:.1f}s)")

    # Check tracks
    streams = payload.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    print(f"Video:     {len(videos)} track(s)")
    print(f"Audio:     {len(audio)} track(s)")
    print(f"Subtitle:  {len(subtitles)} track(s)")

    # Check DV
    has_dv = False
    for stream in streams:
        for side in stream.get("side_data_list") or []:
            if side.get("side_data_type") == "DOVI configuration record":
                profile = side.get("dv_profile")
                rpu = side.get("rpu_present_flag")
                el = side.get("el_present_flag")
                bl = side.get("bl_present_flag")
                print(f"DV:        Profile {profile}, RPU={rpu}, EL={el}, BL={bl}")
                has_dv = True
    if not has_dv:
        if len(asset.video_tracks) > 1:
            print("DV:        NOT FOUND (expected Profile 7)")
        else:
            print("DV:        not applicable (single video track)")

    return 0


def cmd_list_tools(args: argparse.Namespace) -> int:
    """List available external tools and their paths."""
    tools = [
        "mkvmerge",
        "ffmpeg",
        "ffprobe",
        "dovi_tool",
        "hdathome-bluray-title-reader",
    ]
    for tool in tools:
        path = _find_tool(tool)
        status = f"found: {path}" if path else "NOT FOUND"
        print(f"  {tool:40s} {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bluray-fidelity",
        description="Dolby Vision Profile 7 保真交付工具链 — 从 Blu-ray BDMV/ISO 精确还原 MKV",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # probe
    p_probe = sub.add_parser("probe", help="Probe a Blu-ray source")
    p_probe.add_argument("source", help="BDMV directory or ISO file")
    p_probe.add_argument(
        "--all", dest="all_titles", action="store_true",
        help="List all available titles",
    )

    # finalize
    p_fin = sub.add_parser("finalize", help="Finalize Blu-ray to lossless MKV")
    p_fin.add_argument("source", help="BDMV directory or ISO file")
    p_fin.add_argument("-o", "--output", required=True, help="Output MKV path")
    p_fin.add_argument("--mplis", dest="playlist", type=int, help="Force MPLS playlist number")
    p_fin.add_argument("--title-index", dest="title_index", type=int, help="Force title index")
    p_fin.add_argument("--force", action="store_true", help="Overwrite existing output")
    p_fin.add_argument("--retry", action="store_true", help="Retry on transient errors")
    p_fin.add_argument("--dv-profile", dest="dv_profile", default=None,
                        help="DV profile: '7' (preserve EL) or '81' (Profile 8.1)")
    p_fin.add_argument("--mkvmerge", help="Path to mkvmerge")
    p_fin.add_argument("--ffmpeg", help="Path to ffmpeg")
    p_fin.add_argument("--ffprobe", help="Path to ffprobe")
    p_fin.add_argument("--dovi-tool", dest="dovi_tool", help="Path to dovi_tool")

    # verify
    p_ver = sub.add_parser("verify", help="Verify MKV against Blu-ray source")
    p_ver.add_argument("mkv", help="MKV file to verify")
    p_ver.add_argument("--source", required=True, help="Original BDMV/ISO source")
    p_ver.add_argument("--ffprobe", help="Path to ffprobe")

    # tools
    sub.add_parser("tools", help="List external tool availability")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    commands = {
        "probe": cmd_probe,
        "finalize": cmd_finalize,
        "verify": cmd_verify,
        "tools": cmd_list_tools,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
