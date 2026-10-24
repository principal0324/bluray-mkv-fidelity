"""bluray-mkv-fidelity: Dolby Vision Profile 7 保真交付工具链。

从 Blu-ray BDMV/ISO 精确还原 MKV，完整保留 BL+EL+RPU 三层，
通过独立证据链校验确保 DV 点亮。

核心能力：
- MPLS 主片精确识别与流式读取
- DV Profile 7 / 8.1 保真合并（dovi_tool）
- HDR10+ T.35 动态元数据精确剥离
- EBML 结构化 Cues 校验（大型 Cues 有界流式计数）
- 多点 seek 正交验证
- 多 m2ts 合辑的 DV 段级封装与接缝校验
- 字幕覆盖证据门（稀疏缺口按包时间线证明）
- ISO/UDF 直读（不经 libbluray 挂载路径，源损伤补包留痕）
- fail-closed 发布策略
"""

__version__ = "0.2.0"

from .bluray import (
    BlurayClip,
    BlurayTitleAsset,
    BlurayTitleError,
    BlurayTrack,
    TransientBlurayReadError,
    bluray_item_bounds,
    cleanup_stale_readers,
    clear_bluray_reader_pool,
    iter_bluray_item,
    iter_bluray_title,
    list_main_titles,
    probe_main_title,
    source_damage_records,
)
from .matroska import (
    BuildArtifacts,
    CoverageWindow,
    CuesParseError,
    FifoMatroskaBuilder,
    FfmpegDvFileMatroskaBuilder,
    FfmpegFileMatroskaBuilder,
    IdentifiedTrack,
    MatroskaBuilder,
    MatroskaBuildError,
    ResolvedTracks,
    SegmentTrackSubset,
    SourceSubtitleEvidence,
    TrackCoverageFact,
    TransientMatroskaBuildError,
    build_append_only_command,
    build_artifacts,
    build_dv_segment_command,
    build_mkvmerge_command,
    build_mkvmerge_command_dv,
    coverage_windows,
    dense_sample_windows,
    describe_multiclip_compilation,
    dual_hevc_video_streams,
    longest_uncovered_gap,
    longest_uncovered_packet_gap,
    multiclip_item_bounds,
    parse_identification,
    require_dovi_side_data,
    require_segment_track_correspondence,
    require_segment_video_correspondence,
    require_track_coverage,
    resolve_official_tracks,
    summarise_track_coverage,
    validate_finalized_identification,
)
from .udf_media import udf_available

__all__ = [
    "BlurayClip",
    "BlurayTitleAsset",
    "BlurayTitleError",
    "BlurayTrack",
    "BuildArtifacts",
    "CoverageWindow",
    "CuesParseError",
    "FifoMatroskaBuilder",
    "FfmpegDvFileMatroskaBuilder",
    "FfmpegFileMatroskaBuilder",
    "IdentifiedTrack",
    "MatroskaBuilder",
    "MatroskaBuildError",
    "ResolvedTracks",
    "SegmentTrackSubset",
    "SourceSubtitleEvidence",
    "TrackCoverageFact",
    "TransientBlurayReadError",
    "TransientMatroskaBuildError",
    "bluray_item_bounds",
    "build_append_only_command",
    "build_artifacts",
    "build_dv_segment_command",
    "build_mkvmerge_command",
    "build_mkvmerge_command_dv",
    "cleanup_stale_readers",
    "clear_bluray_reader_pool",
    "coverage_windows",
    "dense_sample_windows",
    "describe_multiclip_compilation",
    "dual_hevc_video_streams",
    "iter_bluray_item",
    "iter_bluray_title",
    "list_main_titles",
    "longest_uncovered_gap",
    "longest_uncovered_packet_gap",
    "multiclip_item_bounds",
    "parse_identification",
    "probe_main_title",
    "require_dovi_side_data",
    "require_segment_track_correspondence",
    "require_segment_video_correspondence",
    "require_track_coverage",
    "resolve_official_tracks",
    "source_damage_records",
    "summarise_track_coverage",
    "udf_available",
    "validate_finalized_identification",
]
