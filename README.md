# bluray-mkv-fidelity

Dolby Vision Profile 7 保真交付工具链。从 Blu-ray BDMV/ISO 精确还原 MKV，完整保留 BL+EL+RPU 三层，通过独立证据链校验确保 DV 点亮。

## 为什么做这个

MakeMKV 是优秀的通用 MKV 提取工具，但在 Dolby Vision Profile 7 场景下有已知短板：

- dvcC（DOVIDecoderConfigurationRecord）偶尔丢失
- Enhancement Layer 可能被静默丢弃导致 HDR10 降级
- 缺乏 fail-closed 发布策略

本项目**专注 DV Profile 7 保真交付**：

- **三层全保留**：BL + EL + RPU，禁止 `--discard`，禁止降 Profile 8，禁止丢 EL
- **独立证据链**：RPU 载荷 10%/50%/90% 窗口校验 + EL 有序一致性 + ffprobe dvcC 权威门
- **fail-closed**：任何一环不通过就不发布
- **HDR10+ 精确处理**：NAL 级 T.35 SEI 剥离，保留静态 HDR10
- **EBML 结构化校验**：按元素边界遍历 Cues，非裸字节搜索
- **多点 seek 验证**：10%/50%/90% 时间点 BL 解码正交验证

## 快速开始

### 安装

```bash
# Python 包
pip install .

# C++ 原生辅助工具（需要 libbluray-dev）
cd native/bluray-title-reader && make && sudo make install
```

### 外部依赖

| 工具 | 用途 | 安装 |
|---|---|---|
| `mkvmerge` | TS→MKV 封装 | `apt install mkvtoolix` / `brew install mkvtoolnix` |
| `ffmpeg` / `ffprobe` | 流探测、ES 提取 | `apt install ffmpeg` / `brew install ffmpeg` |
| `dovi_tool` | DV BL+EL 合并 | `cargo install dovi_tool` 或 [GitHub Release](https://github.com/quietvoid/dovi_tool/releases) |
| `hdathome-bluray-title-reader` | Blu-ray 导航 | 本项目 `native/` 目录编译 |

### 使用

```bash
# 探测蓝光
bluray-fidelity probe /path/to/bdmv

# 列出所有标题
bluray-fidelity probe /path/to/bdmv --all

# 生成 MKV（自动选最长标题）
bluray-fidelity finalize /path/to/bdmv -o output.mkv

# 指定 MPLS
bluray-fidelity finalize /path/to/bdmv --mplis 00001 -o output.mkv

# ISO 输入
bluray-fidelity finalize /path/to/movie.iso -o output.mkv

# 校验已有 MKV
bluray-fidelity verify output.mkv --source /path/to/bdmv

# 检查工具可用性
bluray-fidelity tools
```

### Python API

```python
from bluray_fidelity import probe_main_title, MatroskaBuilder

# 探测
asset = probe_main_title(Path("/path/to/bdmv"))
print(f"Title: MPLS {asset.playlist:05d}, {len(asset.audio_tracks)} audio tracks")

# 生成
builder = MatroskaBuilder()
result = builder.build(asset, Path("output.mkv"), progress=lambda p, m: print(f"[{p}%] {m}"))
```

## 与 MakeMKV 的对比

| 维度 | MakeMKV | bluray-mkv-fidelity |
|---|---|---|
| DV Profile 7 | 基本支持，社区反馈 dvcC/EL 问题 | **严格保真**：三层全保留 + 证据链 |
| 发布策略 | 尽量成功 | **fail-closed**：不通过不发布 |
| EL 校验 | 无 | **有序一致性**：源 EL 与成品 EL 严格对齐 |
| RPU 校验 | 无 | **窗口验证**：10%/50%/90% 载荷采样 |
| Cues 校验 | 基本 | **EBML 结构化**：按元素边界遍历 |
| HDR10+ | 不处理 | **精确剥离**：NAL 级 T.35 SEI |
| Seek 验证 | 无 | **正交验证**：BL 单独 demux 解码 |
| 目标用户 | 通用用户 | 技术型用户 / 版权方交付 |

## 架构

```
bluray-fidelity probe/verify   ← CLI 入口
    │
    ├── bluray.py               ← MPLS 解析、libbluray 交互、轨道识别
    │   └── hdathome-bluray-title-reader (C++)
    │
    ├── matroska.py             ← MKV 封装、DV 证据链、校验门
    │   ├── mkvmerge            (外部)
    │   ├── ffmpeg/ffprobe      (外部)
    │   └── dovi_tool           (外部)
    │
    └── m2ts.py                 ← 直接 M2TS 提取（不经封装）
```

## 测试

```bash
pip install -e ".[test]"
pytest tests/
```

## 许可证

Apache-2.0。详见 [LICENSE](LICENSE)。

外部依赖许可证：
- `dovi_tool`: AGPL-3.0 (上游 [quietvoid/dovi_tool](https://github.com/quietvoid/dovi_tool))
- `mkvmerge`: GPL-2.0+
- `ffmpeg`/`ffprobe`: GPL-2.0+
- `libbluray`: LGPL-2.1+

## 致谢

- [quietvoid/dovi_tool](https://github.com/quietvoid/dovi_tool) — Dolby Vision 处理工具
- [MKVToolNix](https://mkvtoolnix.download/) — MKV 封装工具
- [FFmpeg](https://ffmpeg.org/) — 多媒体框架
- [libbluray](https://code.videolan.org/videolan/libbluray) — Blu-ray 导航库
