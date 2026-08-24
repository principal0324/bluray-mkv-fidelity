"""bluray-mkv-fidelity: Dolby Vision Profile 7 保真交付工具链。

从 Blu-ray BDMV/ISO 精确还原 MKV，完整保留 BL+EL+RPU 三层，
通过独立证据链校验确保 DV 点亮。

核心能力：
- MPLS 主片精确识别与流式读取
- DV Profile 7 / 8.1 保真合并（dovi_tool）
- HDR10+ T.35 动态元数据精确剥离
- EBML 结构化 Cues 校验
- 多点 seek 正交验证
- fail-closed 发布策略
"""

__version__ = "0.1.0"

from .bluray import (
    BlurayClip,
    BlurayTitleAsset,
    BlurayTitleError,
    BlurayTrack,
    clear_bluray_reader_pool,
    iter_bluray_title,
    list_main_titles,
    probe_main_title,
)
from .matroska import (
    BuildArtifacts,
    IdentifiedTrack,
    MatroskaBuilder,
    MatroskaBuildError,
    ResolvedTracks,
    TransientMatroskaBuildError,
    build_artifacts,
    build_mkvmerge_command,
    build_mkvmerge_command_dv,
    parse_identification,
    resolve_official_tracks,
    require_dovi_side_data,
    validate_finalized_identification,
)

__all__ = [
    "BlurayClip",
    "BlurayTitleAsset",
    "BlurayTitleError",
    "BlurayTrack",
    "BuildArtifacts",
    "IdentifiedTrack",
    "MatroskaBuilder",
    "MatroskaBuildError",
    "ResolvedTracks",
    "TransientMatroskaBuildError",
    "build_artifacts",
    "build_mkvmerge_command",
    "build_mkvmerge_command_dv",
    "clear_bluray_reader_pool",
    "iter_bluray_title",
    "list_main_titles",
    "parse_identification",
    "probe_main_title",
    "resolve_official_tracks",
    "require_dovi_side_data",
    "validate_finalized_identification",
]
