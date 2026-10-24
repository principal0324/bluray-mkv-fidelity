"""直读（BDMV 文件 / ISO UDF）与空包 PID 加固判定的测试。

2026-09-14 生产实证（dlv_eb87606f7cf448afbf86 霸王别姬）：libbluray 的 title
读层把该盘正片 playlist 每个 TS 包的 PID 字段改写成 0x1FFF，载荷却是干净的。
修法是取字节时不依赖该读层（ISO 走镜像内 UDF），并且一旦采样窗口全是空包就
失败关闭并点名——这两件事都必须有可自动验证的不变式。
"""

import importlib
from pathlib import Path

import pytest


def _bluray():
    return importlib.import_module("bluray_fidelity.bluray")


def _udf_media():
    return importlib.import_module("bluray_fidelity.udf_media")


class _FakeClip:
    """内存 clip 文件：按 offset 切片返回，记录调用序列。"""

    def __init__(self, data: bytes):
        self.data = data
        self.offsets: list[tuple[int, int]] = []
        self.closed = False

    def size(self) -> int:
        return len(self.data)

    def read_at(self, offset: int, length: int) -> bytes:
        self.offsets.append((offset, length))
        return self.data[offset:offset + length]

    def close(self) -> None:
        self.closed = True


class _ForbiddenPool:
    """直读可用时任何 libbluray reader 获取都必须让测试失败。"""

    def get(self, *_args, **_kwargs):
        pytest.fail("直读可用时不得启动 libbluray reader")

    def discard(self, *_args, **_kwargs):
        pytest.fail("直读可用时不得触碰 libbluray reader 池")


def _packet(pid: int) -> bytes:
    """192 字节 M2TS 包：4 字节 ATS + 188 字节 TS（PID 字段在包内第 5、6 字节）。"""
    ts_header = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF])
    return bytes([0x14, 0x48, 0x40, 0x00]) + ts_header + b"\x00" * 185


# ---- 直读计划：只有"item 恰好覆盖整个 clip"才允许直读 ----

def test_direct_plan_accepts_single_item_covering_whole_clip(tmp_path):
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc.iso"
    clip = bluray.BlurayClip(index=0, clip_id="00016", in_time=0, out_time=90000)

    plan = udf.build_direct_plan(root, "iso", (clip,), ((0, 4096 * 192),),
                                 size_of=lambda _clip_id: 4096 * 192)

    assert plan is not None
    assert len(plan) == 1
    assert plan[0].clip_id == "00016"
    assert plan[0].offset == 0
    assert plan[0].length == 4096 * 192


def test_direct_plan_rejects_trimmed_item(tmp_path):
    """按时间裁剪的 item（长度 != clip 文件长度）必须拒绝：需要 CPI 映射。"""
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc.iso"
    clip = bluray.BlurayClip(index=0, clip_id="00001", in_time=54000000,
                             out_time=180000000)

    plan = udf.build_direct_plan(root, "iso", (clip,), ((0, 1000 * 192),),
                                 size_of=lambda _clip_id: 5000 * 192)

    assert plan is None


def test_direct_plan_rejects_offset_mismatch_and_count_mismatch(tmp_path):
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc.iso"
    clips = (
        bluray.BlurayClip(index=0, clip_id="00000", in_time=0, out_time=100),
        bluray.BlurayClip(index=1, clip_id="00001", in_time=0, out_time=100),
    )
    sizes = {"00000": 100 * 192, "00001": 200 * 192}

    # 虚拟流偏移不接续（第二个 item 起点不是前一个长度之和）
    assert udf.build_direct_plan(root, "iso", clips, ((0, 100 * 192),
                                                      (50 * 192, 200 * 192)),
                                 size_of=sizes.get) is None
    # 数量不同
    assert udf.build_direct_plan(root, "iso", clips, ((0, 100 * 192),),
                                 size_of=sizes.get) is None
    # clip 不可读（长度未知）绝不猜
    assert udf.build_direct_plan(root, "iso", clips,
                                 ((0, 100 * 192), (100 * 192, 200 * 192)),
                                 size_of=lambda _clip_id: None) is None


def test_direct_plan_accepts_multi_item_whole_clips(tmp_path):
    """多段但每段都是完整 clip（连放未裁剪结构）同样可证明等价。"""
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc"
    clips = (
        bluray.BlurayClip(index=0, clip_id="00000", in_time=0, out_time=100),
        bluray.BlurayClip(index=1, clip_id="00001", in_time=0, out_time=100),
    )
    sizes = {"00000": 100 * 192, "00001": 200 * 192}

    plan = udf.build_direct_plan(
        root, "bdmv", clips,
        ((0, 100 * 192), (100 * 192, 200 * 192)), size_of=sizes.get)

    assert [item.clip_id for item in plan] == ["00000", "00001"]
    assert [item.length for item in plan] == [100 * 192, 200 * 192]


# ---- 直读尾部松量（2026-09-16；用户裁定 3 帧预算同批） ----


def test_direct_plan_accepts_production_tail_slack_shape(tmp_path):
    """生产实测形态（地球脉动.第三季 Disc.3 playlist 33 item1）→ 必须放行。

    clip 24,515,162,112 字节、item 24,515,161,344 字节（缺口 768 = 4 个包）、
    时长 3236.8 s ⇒ 平均 7.22 MB/s、1 帧预算 315,577 字节 ≫ 768 字节。
    注意 in_time=600s：**in_time 不是字节偏移**（同一标题 item0 同样 in_time=600s
    却与 clip 逐字节等长），所以判据不能建立在 in_time 上。
    """
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc"
    clip = bluray.BlurayClip(index=0, clip_id="00019", in_time=54000000,
                             out_time=345313920)          # 600.0s → 3836.8s
    item_length = 24_515_161_344
    clip_size = 24_515_162_112

    plan = udf.build_direct_plan(root, "iso", (clip,), ((0, item_length),),
                                 size_of=lambda _clip_id: clip_size)

    assert plan is not None, "尾部松量形态必须可直读（此前因 length != size 被整体拒绝）"
    assert plan[0].offset == 0 and plan[0].length == item_length


def test_direct_plan_accepts_small_tail_slack_regardless_of_in_time(tmp_path):
    """松量判据只看缺口量级，不看 in_time（in_time 是 STC 基准，非字节偏移）。"""
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc"
    # 同一形状、仅 in_time 不同：都应放行（判据只看缺口量级，不看 in_time）。
    # 用现实量级：1000 s 的片段、约 1.5 Mbps，则 1 帧预算约 8000 字节；
    # 缺口 1 个包（192 字节）远在预算内。
    one_packet = 192
    clip_size = 1000 * one_packet * 1000          # ≈192 MB / 1000 s ≈ 1.5 Mbps
    for in_time in (0, 54000000, 900000):
        clip = bluray.BlurayClip(index=0, clip_id="00020", in_time=in_time,
                                 out_time=in_time + 90000 * 1000)   # 1000 s
        plan = udf.build_direct_plan(
            root, "iso", (clip,), ((0, clip_size - one_packet),),
            size_of=lambda _clip_id: clip_size)
        assert plan is not None and plan[0].length == clip_size - one_packet


def test_direct_plan_rejects_leading_trim_over_frame_budget(tmp_path):
    """真实前导裁剪（缺口远超 1 帧字节量）→ 必须拒绝（按时间裁剪需要 CPI 映射）。"""
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc"
    # 时长 1400 s、clip 5000*192 字节 ⇒ 1 帧预算约 28 字节；缺口 4000*192 字节
    clip = bluray.BlurayClip(index=0, clip_id="00021", in_time=54000000,
                             out_time=180000000)
    assert udf.build_direct_plan(root, "iso", (clip,), ((0, 1000 * 192),),
                                 size_of=lambda _clip_id: 5000 * 192) is None


def test_direct_plan_rejects_unusable_duration(tmp_path):
    """时长不可用（out ≤ in）→ 拒绝，不猜松量预算。"""
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc"
    clip = bluray.BlurayClip(index=0, clip_id="00022", in_time=54000000,
                             out_time=54000000)
    assert udf.build_direct_plan(root, "iso", (clip,), ((0, 100 * 192),),
                                 size_of=lambda _clip_id: 200 * 192) is None


def test_direct_plan_rejects_item_longer_than_clip(tmp_path):
    """item 比 clip 文件长（读会越界）→ 拒绝（松量规则不覆盖反向）。"""
    udf = _udf_media()
    bluray = _bluray()
    root = tmp_path / "disc.iso"
    clip = bluray.BlurayClip(index=0, clip_id="00001", in_time=0,
                             out_time=180000000)

    assert udf.build_direct_plan(root, "iso", (clip,), ((0, 300 * 192),),
                                 size_of=lambda _clip_id: 200 * 192) is None


def test_iter_direct_item_streams_prefix_of_larger_file(tmp_path):
    """松量 item 在更大文件上：只产出 item 长度的字节，且正常收尾。"""
    udf = _udf_media()
    data = bytes(range(256)) * 40          # 10240 字节 = clip 文件
    item_len = 4096 * 2                     # item 只覆盖前 8192 字节
    item = udf.DirectItem(item_index=0, clip_id="00000", offset=0, length=item_len)
    clip = _FakeClip(data)

    chunks = list(udf.iter_direct_item((item,), 0, 4096, root=tmp_path,
                                       source_kind="bdmv",
                                       reader_factory=lambda _clip_id: clip))
    assert b"".join(chunks) == data[:item_len]
    assert clip.closed is True


# ---- 直读字节流 ----

def test_iter_direct_item_streams_exact_bytes_and_fails_on_short_read(tmp_path):
    udf = _udf_media()
    data = bytes(range(256)) * 40  # 10240 字节
    item = udf.DirectItem(item_index=0, clip_id="00000", offset=0, length=len(data))
    clip = _FakeClip(data)

    chunks = list(udf.iter_direct_item((item,), 0, 4096, root=tmp_path,
                                       source_kind="bdmv",
                                       reader_factory=lambda _clip_id: clip))
    assert b"".join(chunks) == data
    assert clip.closed is True

    truncated = _FakeClip(data[:100])
    with pytest.raises(udf.DirectReadError):
        list(udf.iter_direct_item((item,), 0, 4096, root=tmp_path,
                                  source_kind="bdmv",
                                  reader_factory=lambda _clip_id: truncated))


def test_iter_direct_item_rejects_out_of_range_index(tmp_path):
    udf = _udf_media()
    with pytest.raises(udf.DirectReadError):
        list(udf.iter_direct_item((), 0, 4096, root=tmp_path, source_kind="bdmv"))


# ---- 空包 PID 加固判定 ----

def test_require_visible_pid_rejects_all_null_stream():
    udf = _udf_media()
    nulls = _packet(0x1FFF) * 512
    with pytest.raises(udf.DirectReadError) as error:
        udf.require_visible_pid([("样本 头 98304 字节", nulls)])
    assert "0x1FFF" in str(error.value)


def test_require_visible_pid_allows_real_stream_and_partial_null():
    udf = _udf_media()
    real = _packet(0x1011) * 500 + _packet(0x1FFF) * 12
    udf.require_visible_pid([("样本 头", real)])  # 不抛

    # 只有一个窗口全空、另一窗口干净 → 不否决（真实盘可能有空包填充区间）
    udf.require_visible_pid([
        ("头", _packet(0x1FFF) * 512),
        ("中段", _packet(0x1011) * 512),
    ])


def test_bluray_reader_fails_closed_on_null_pid_from_reader_path(tmp_path, monkeypatch):
    """libbluray 读路径给出全空包时必须在取字节这一步失败并点名（不是下游报错）。"""
    bluray = _bluray()
    root = tmp_path / "disc"
    (root / "BDMV").mkdir(parents=True)
    (root / "BDMV/index.bdmv").write_bytes(b"index")
    nulls = _packet(0x1FFF) * 512

    with pytest.raises(bluray.BlurayTitleError) as error:
        bluray._require_visible_pid(
            "主片第 1 段（libbluray 读取 disc）", 10 * 1024 * 1024,
            lambda offset, length: nulls[:length])
    assert "0x1FFF" in str(error.value)


# ---- 管线接线：可选直读时不得再走 libbluray 读层 ----

def test_iter_bluray_item_prefers_direct_read(monkeypatch, tmp_path):
    bluray = _bluray()
    udf = _udf_media()
    root = tmp_path / "disc.iso"
    root.write_bytes(b"\x00" * 16)
    data = _packet(0x1011) * 4000
    clip = _FakeClip(data)
    asset = bluray.BlurayTitleAsset(
        root=root, entry_path=bluray._ENTRY_PATH, title_index=2, playlist=4,
        duration_90k=90000, size=len(data), clip_count=1,
        source_kind="iso",
        clips=(bluray.BlurayClip(index=0, clip_id="00016", in_time=0,
                                 out_time=90000),),
    )
    plan = (udf.DirectItem(item_index=0, clip_id="00016", offset=0,
                           length=len(data)),)
    monkeypatch.setattr(bluray, "bluray_item_bounds", lambda _asset: ((0, len(data)),))
    monkeypatch.setattr(bluray, "_direct_item_plan", lambda _asset: plan)
    monkeypatch.setattr(bluray, "_direct_read_at",
                        lambda _asset, _clip_id, at, size: clip.read_at(at, size))
    monkeypatch.setattr(udf, "clip_reader", lambda *_args, **_kwargs: clip)
    monkeypatch.setattr(bluray, "_reader_pool", _ForbiddenPool())

    chunks = list(bluray.iter_bluray_item(asset, 0, chunk_size=1 << 20))

    assert b"".join(chunks) == data
    # 只读一次头窗口采样 + 顺序读；采样后从 0 开始顺序读
    assert [offset for offset, _size in clip.offsets][0] == 0


def test_iter_bluray_title_direct_only_for_single_whole_clip(monkeypatch, tmp_path):
    """整片直读只在"主片 = 单个未裁剪 clip"时成立；多段结构回落原路径。"""
    bluray = _bluray()
    udf = _udf_media()
    root = tmp_path / "disc.iso"
    root.write_bytes(b"\x00" * 16)
    multi = (
        udf.DirectItem(item_index=0, clip_id="00000", offset=0, length=100),
        udf.DirectItem(item_index=1, clip_id="00001", offset=0, length=200),
    )
    asset = bluray.BlurayTitleAsset(
        root=root, entry_path=bluray._ENTRY_PATH, title_index=3, playlist=3,
        duration_90k=90000, size=300, clip_count=2, source_kind="iso")
    monkeypatch.setattr(bluray, "_direct_item_plan", lambda _asset: multi)
    assert bluray._direct_title_plan(asset) is None

    single = (udf.DirectItem(item_index=0, clip_id="00016", offset=0,
                             length=300),)
    monkeypatch.setattr(bluray, "_direct_item_plan", lambda _asset: single)
    assert bluray._direct_title_plan(asset) == single


def test_udf_media_reports_unavailable_library(monkeypatch):
    """镜像缺 libudfread 时必须给出命名原因，绝不静默回落。"""
    udf = _udf_media()
    monkeypatch.setattr(udf, "_udf_library", False)
    assert udf.udf_available() is False
    with pytest.raises(udf.UdfUnavailable):
        udf.clip_reader(Path("/nonexistent.iso"), "iso", "00016")
