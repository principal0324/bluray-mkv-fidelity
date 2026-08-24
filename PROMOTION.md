# 推广文案

## V2EX 帖子

标题：[开源] 做了个 Dolby Vision Profile 7 保真工具链，比 MakeMKV 更靠谱

正文：

```
项目地址：https://github.com/principal0324/bluray-mkv-fidelity

## 做了什么

一个 Blu-ray BDMV/ISO → MKV 的工具链，专门解决 MakeMKV 处理 Dolby Vision 时的问题：

- dvcC（DOVIDecoderConfigurationRecord）可能丢失
- Enhancement Layer 被静默丢弃，导致 HDR10 降级
- MakeMKV 是"尽量成功"哲学，不是"失败即停止"

## 核心特点

1. **三层保留**：BL + EL + RPU（DV Profile 7 完整保留）
2. **10 层验证链**：源指纹、dvcC 权威验证、EL 一致性、RPU 证据采样...
3. **HDR10+ 精确剥离**：NAL 级 T.35 SEI 移除，保留静态 HDR10
4. **零第三方 Python 依赖**：只用标准库

## 适合谁

- 对 DV 画质有极致要求的影音发烧友
- PT 站压制组
- 需要档案级备份的用户

## 安装

```bash
pip install .
bluray-fidelity probe /path/to/bdmv
bluray-fidelity finalize /path/to/bdmv --output movie.mkv
```

## 外部依赖

- dovi_tool（BL+EL 合并）
- mkvmerge（TS→MKV 封装）
- ffmpeg/ffprobe（流探测）
- libbluray（蓝光导航）

欢迎试用反馈，有问题直接提 Issue。
```

## 知乎文章

标题：为什么我做了一个比 MakeMKV 更严格的 Dolby Vision 工具

（需要的话我可以展开写）

## Reddit 帖子（英文）

标题：[Tool] Dolby Vision Profile 7 Fidelity Toolchain — Blu-ray BDMV/ISO to MKV with fail-closed verification

正文：
```
GitHub: https://github.com/principal0324/bluray-mkv-fidelity

Built a toolchain that extracts Blu-ray content to MKV with strict Dolby Vision Profile 7 integrity guarantees.

Why? MakeMKV can silently drop the Enhancement Layer or lose the dvcC record, downgrading DV to HDR10 without warning.

What it does:
- Preserves BL + EL + RPU (full DV Profile 7)
- 10-layer verification chain (fail-closed: if any gate fails, output is rejected)
- HDR10+ T.35 SEI stripping at NAL level
- EBML Cues validation

Zero Python dependencies — only stdlib. External tools: dovi_tool, mkvmerge, ffmpeg, libbluray.

Tested with various DV Profile 7 discs. Feedback welcome.
```
