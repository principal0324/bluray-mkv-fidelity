# 外部依赖安装指南

## 必需依赖

### mkvmerge（MKVToolNix）

MKV 封装工具。用于将 MPEG-TS 流封装为 Matroska 容器。

```bash
# Debian/Ubuntu
sudo apt install mkvtoolix

# macOS
brew install mkvtoolnix

# 验证
mkvmerge --version
```

### ffmpeg / ffprobe

多媒体框架。用于流探测、ES 提取、帧率/时长校验。

```bash
# Debian/Ubuntu
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# 验证
ffmpeg -version
ffprobe -version
```

## 可选依赖（DV 内容必需）

### dovi_tool

Dolby Vision 处理工具。用于 BL+EL 合并、RPU 提取/验证。

```bash
# 安装 Rust（如果还没装）
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# 安装 dovi_tool
cargo install dovi_tool

# 或从 GitHub Release 下载预编译二进制
# https://github.com/quietvoid/dovi_tool/releases

# 验证
dovi_tool --version
```

**注意**：dovi_tool 使用 AGPL-3.0 许可证。如果你将其链接到你的项目中，请遵守 AGPL 要求。

### hdathome-bluray-title-reader

Blu-ray 标题读取辅助工具。基于 libbluray，提供 MPLS 解析和原始 TS 包读取。

```bash
# 安装 libbluray 开发库
# Debian/Ubuntu
sudo apt install libbluray-dev

# macOS
brew install libbluray

# 编译
cd native/bluray-title-reader
make

# 安装（可选）
sudo make install

# 验证
hdathome-bluray-title-reader --probe /path/to/bdmv
```

## Python 依赖

本项目无第三方 Python 依赖，仅使用标准库。

```bash
# 安装
pip install .

# 开发模式
pip install -e .

# 测试
pip install -e ".[test]"
pytest tests/
```

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `HDATHOME_BLURAY_TITLE_READER` | 自动搜索 | C++ helper 路径 |
| `HDATHOME_DV_PROFILE` | `7` | DV 策略：`7`（保留 EL）或 `81`（Profile 8.1） |

## 系统要求

- Python ≥ 3.10
- C++17 编译器（编译 C++ helper 时）
- libbluray（C++ helper 链接时）
