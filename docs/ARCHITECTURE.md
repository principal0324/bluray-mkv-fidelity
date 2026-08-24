# 架构说明

## 组件

```
bluray-mkv-fidelity/
├── src/bluray_fidelity/          # Python 包
│   ├── __init__.py               # 公共 API
│   ├── cli.py                    # 命令行入口
│   ├── bluray.py                 # MPLS 解析、libbluray 交互
│   ├── matroska.py               # MKV 封装、DV 证据链、校验门
│   └── m2ts.py                   # 直接 M2TS 提取
├── native/bluray-title-reader/   # C++ 原生辅助工具
│   ├── bluray_title_reader.cpp   # libbluray 持久进程
│   └── Makefile
├── tests/                        # 测试
└── docs/                         # 文档
```

## 数据流

### Probe（探测）

```
BDMV/ISO
  → hdathome-bluray-title-reader --probe
  → libbluray bd_open → bd_get_titles → bd_select_title
  → SELECTED/TITLE/STREAM/CLIP/VINFO 行
  → Python 解析 → BlurayTitleAsset
```

### Finalize（生成）

```
BlurayTitleAsset
  → [可选] HDR10+ 检测 → T.35 SEI 剥离
  → 物化临时 M2TS（通过 --serve 模式流式读取）
  → [可选] DV Profile 7:
  │   → ffmpeg 提取 BL/EL PID
  │   → dovi_tool mux BL+EL → merged.hevc
  │   → RPU 证据采集
  │   → EL 有序一致性校验
  → mkvmerge 封装 → 临时 MKV
  → 统一输出校验:
  │   → ffprobe 时长/帧率/轨道/DV dvcC
  │   → EBML Cues 结构化计数
  │   → 多点 seek 正交验证
  │   → 语言一致性校验
  → 原子发布最终 MKV
  → 清理临时文件
```

## 校验门层次

1. **源指纹**：MPLS 选片后记录源特征
2. **DV 检测**：PMT 双 HEVC PID → Profile 7 候选
3. **EL 一致性**：源 EL VCL 摘要 vs 成品 EL 回拆
4. **RPU 证据**：合并流 RPU 载荷窗口采样
5. **dvcC 权威**：ffprobe DOVI configuration record
6. **Cues 结构化**：EBML 元素边界遍历
7. **Seek 正交**：10%/50%/90% BL demux 解码
8. **语言归一**：ISO 639-2 B/T 等价比较
9. **帧率精度**：±0.01 一致性
10. **时长容差**：±2s MPLS 对齐

## 外部工具依赖

| 工具 | 角色 | 调用方式 |
|---|---|---|
| `hdathome-bluray-title-reader` | Blu-ray 导航 | subprocess（持久进程 + stdin/stdout 协议） |
| `mkvmerge` | TS→MKV 封装 | subprocess（一次性命令） |
| `ffmpeg` | ES 提取、流复制 | subprocess（管道 + 一次性命令） |
| `ffprobe` | 流探测、帧计数 | subprocess（JSON 输出） |
| `dovi_tool` | DV BL+EL 合并、RPU 提取 | subprocess（一次性命令） |

## 错误处理

- `MatroskaBuildError`：不可恢复错误（门失败），不重试
- `TransientMatroskaBuildError`：瞬态错误（进程中断、超时），有界重试
- 任何门失败 → 不发布（fail-closed）
- 临时文件 → finally 清理
