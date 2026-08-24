# 与 MakeMKV 的技术对比

## 概述

MakeMKV 是目前最广泛使用的 Blu-ray → MKV 提取工具，拥有20年社区积累和优秀的易用性。本项目不试图替代 MakeMKV，而是专注于一个特定场景：**Dolby Vision Profile 7 的保真交付**。

## 核心差异

### 1. DV Profile 7 处理

**MakeMKV：**
- 支持基本的 DV Profile 5/8.1 提取
- 对 Profile 7（BL+EL+RPU 三层）的支持有限
- 社区长期反馈 dvcC 丢失、EL 被静默丢弃
- 输出后需要用户自行验证 DV 是否点亮

**本项目：**
- **严格 Profile 7 保真**：三层全保留，禁止 `--discard`
- **独立证据链**（fail-closed）：
  - 合并 RPU 载荷 10%/50%/90% 窗口校验
  - `merged_size > bl_size` 验证
  - 源 EL VCL 序列摘要
  - 最终 MKV 回拆 EL 与源 EL 严格有序一致
  - 容器级 dvcC（ffprobe 权威解析）
- 任何一环失败 → 不发布

### 2. 发布策略

**MakeMKV：** "尽量帮你转，出错告诉你"

**本项目：** "任何一环不通过就不发布"（fail-closed）

这对版权方交付场景是刚需——交付一个 DV 未点亮的 MKV 比不交付更糟。

### 3. 校验深度

| 校验项 | MakeMKV | 本项目 |
|---|---|---|
| Cues 存在性 | 基本检查 | EBML 结构化遍历（按元素边界） |
| 时长一致性 | 基本检查 | ±2s 容差 + 视频轨末端探测 |
| 帧率一致性 | 无 | ±0.01 精度校验 |
| 语言一致性 | 无 | ISO 639-2 B/T 等价归一 |
| Seek 可用性 | 无 | 10%/50%/90% 多点验证 |
| DV 完整性 | 无 | 全链证据链 |

### 4. HDR10+ 处理

**MakeMKV：** 不处理 HDR10+ 动态元数据

**本项目：** 精确剥离 T.35 SEI（逐帧 NAL 级扫描），保留静态 HDR10。这对 Zidoo 等固件对动态元数据崩溃的设备是必要的。

### 5. 轨道识别

**MakeMKV：** 基于 libbluray + 内置探测

**本项目：**
- MPLS 精确选片（含近等时长中文优先变体选择）
- PID 解析 + ffprobe 权威验证
- ISO 639-2 B/T 语言等价归一
- TrueHD/AC-3 core PID 拆分处理

## 适用场景

| 场景 | 推荐工具 |
|---|---|
| 通用 MKV 提取（不关心 DV） | MakeMKV |
| DV Profile 7 保真交付 | 本项目 |
| 版权方内容交付 | 本项目 |
| 批量家庭片库 | MakeMKV（更成熟） |
| 技术验证/研究 | 本项目（更透明） |

## 结论

本项目不是 MakeMKV 的竞品，而是补充。MakeMKV 覆盖 90% 的通用场景；本项目解决 MakeMKV 未能覆盖的 10%——DV Profile 7 的工程级保真交付。
