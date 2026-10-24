"""ISO / BDMV 素材文件的直读（绕开 libbluray 的 title 读层）。

2026-09-14 生产实证（dlv_eb87606f7cf448afbf86 霸王别姬）：libbluray 的 **title
读层**在该盘正片 playlist 上把每个 192 字节 TS 包的第 5、6 字节（13 位 PID 字段）
写成 ``0x1FFF``（TS 空包 PID），而载荷逐字节等于用户手工挂载 ISO 复制出的副本
——即盘面内容干净、**问题在我们的读取路径**。下游因此只见 PMT 里的字幕、抽
``0x1FFF`` 得到空视频流、ffmpeg 报 "dimensions not set"。

用户 2026-09-14 裁定："不接 m2ts，修霸王别姬的 ISO，别偷懒"——修法不是换来源，
而是**取字节时不再依赖 libbluray 的 title 读层**：

- libbluray 继续只做**元数据**（playlist / clip 列表 / 轨道表 / play item 区间）；
- 字节从素材文件直读：BDMV 目录读 ``BDMV/STREAM/<clip>.m2ts``；ISO 用镜像内的
  UDF 文件系统读同一个成员文件（libudfread——libbluray 自身的 UDF 后端，随
  libbluray 一起装在执行器镜像里）。

只在**可证明等价**时启用（:func:`build_direct_plan`）：逐 item 要求"虚拟流偏移
== 之前全部 clip 长度之和"且"item 长度 == 该 clip 文件长度"，也就是该 item 恰好
覆盖整个 clip 文件的全部字节。任何一项对不上就返回 ``None``，调用方继续走
libbluray 读路径——绝不猜偏移（按时间段落点需要 CPI 表，未实现的能力必须
失败关闭，不能近似）。

配套加固（同一条实证）：取到的字节若采样窗口内**全是空包 PID**，必须失败关闭并
点名原因，而不是让 mkvmerge/ffmpeg 的天书报错承担诊断（:func:`require_visible_pid`）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

# TS 包固定 192 字节（4 字节 ATS + 188 字节 TS），PID 字段在包内第 5、6 字节。
PACKET_SIZE = 192
NULL_PID = 0x1FFF
PID_WINDOW_PACKETS = 512

# ---- 源损伤预算（2026-09-16 用户裁定：「每秒 24 帧，所以，3 帧以内都修吧」）----
# M2TS 的 ATS 是 27 MHz 时钟（低 30 位；高 2 位是 copy permission）。
ATS_CLOCK_HZ = 27_000_000
ATS_MASK = 0x3FFFFFFF
# 单处预算：3 帧 @24fps = 125 ms。用**时长**而非字节做判据——同一字节数在低码率
# 流上是几十帧、在高码率流上不到一帧（生产标本：87.94 KiB @78.3 Mbps = 9.2 ms）。
DAMAGE_BUDGET_MILLIS = 125.0
# 整单累计预算：多处小洞各自达标也能静默劣化一整片；超过这个量级就不是"流式文件
# 的偶发瑕疵"，而是该定位坏源文件的对象。
DAMAGE_TOTAL_BUDGET_MILLIS = 500.0
# 直读尾部松量的分界（秒）：1 帧 @24fps。真实前导裁剪切掉整帧起步（≥1 帧），
# 而尾部包化松量是亚毫秒级——以此分界，两侧相隔一个数量级（见 build_direct_plan）。
FRAME_BUDGET_SECONDS = 1.0 / 24.0
# 合法空包（PID 0x1FFF）：4 字节 TS 头 + 184 字节 0xFF。
NULL_TS_BYTES = bytes([0x47, NULL_PID >> 8, NULL_PID & 0xFF, 0x10]) + b"\xFF" * 184

_UDFREAD_LIBRARY = "libudfread.so.0"
_UDF_SEEK_SET = 0


class UdfUnavailable(RuntimeError):
    """镜像里没有 libudfread（执行器镜像应随 libbluray 一起带）。"""


class DirectReadError(RuntimeError):
    """直读失败：命名原因，绝不静默回退到可能被改写的读路径。"""


@dataclass(frozen=True)
class DirectItem:
    """一个 play item 的直读事实（item 恰好覆盖整个 clip 文件）。"""

    item_index: int
    clip_id: str
    offset: int
    length: int


def clip_member_path(clip_id: str) -> str:
    return f"BDMV/STREAM/{clip_id}.m2ts"


# ---- libudfread 绑定（懒加载；镜像缺库时明确不可用，不在导入期失败） ----

_udf_lock = threading.Lock()
_udf_library = None


class _UdfLibrary:
    def __init__(self, library: ctypes.CDLL):
        self.lib = library

    def open(self, iso_path: str):
        lib = self.lib
        handle = lib.udfread_init()
        if not handle:
            raise DirectReadError(f"libudfread 初始化失败：{iso_path}")
        code = lib.udfread_open(handle, iso_path.encode())
        if code != 0:
            lib.udfread_close(handle)
            raise DirectReadError(
                f"ISO 的 UDF 卷打不开（libudfread rc={code}）：{iso_path}")
        return handle

    def close(self, handle) -> None:
        try:
            self.lib.udfread_close(handle)
        except Exception:
            logger.warning("libudfread 关闭失败", exc_info=False)

    def open_member(self, handle, member: str):
        fh = self.lib.udfread_file_open(handle, member.encode())
        if not fh:
            raise DirectReadError(f"ISO 内找不到成员文件：{member}")
        return fh

    def size(self, fh) -> int:
        value = int(self.lib.udfread_file_size(fh))
        if value < 0:
            raise DirectReadError("ISO 成员文件长度读取失败")
        return value

    def read_at(self, fh, offset: int, length: int) -> bytes:
        pos = int(self.lib.udfread_file_seek(fh, int(offset), _UDF_SEEK_SET))
        if pos != int(offset):
            raise DirectReadError(
                f"ISO 成员文件定位失败：期望 {offset}，实际 {pos}")
        buffer = ctypes.create_string_buffer(length)
        got = int(self.lib.udfread_file_read(fh, buffer, length))
        if got < 0:
            raise DirectReadError(f"ISO 成员文件读取失败：offset={offset}")
        return buffer.raw[:got]

    def close_member(self, fh) -> None:
        try:
            self.lib.udfread_file_close(fh)
        except Exception:
            logger.warning("libudfread 成员文件关闭失败", exc_info=False)


def _load_udf_library() -> _UdfLibrary | None:
    """绑定 libudfread；不可用返回 None（调用方给出命名原因）。"""
    global _udf_library
    with _udf_lock:
        if isinstance(_udf_library, _UdfLibrary):
            return _udf_library
        if _udf_library is False:
            return None
        try:
            lib = ctypes.CDLL(_UDFREAD_LIBRARY, use_errno=True)
        except OSError:
            _udf_library = False
            return None
        lib.udfread_init.restype = ctypes.c_void_p
        lib.udfread_open.restype = ctypes.c_int
        lib.udfread_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.udfread_close.argtypes = [ctypes.c_void_p]
        lib.udfread_file_open.restype = ctypes.c_void_p
        lib.udfread_file_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.udfread_file_close.argtypes = [ctypes.c_void_p]
        lib.udfread_file_size.restype = ctypes.c_int64
        lib.udfread_file_size.argtypes = [ctypes.c_void_p]
        lib.udfread_file_seek.restype = ctypes.c_int64
        lib.udfread_file_seek.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                                          ctypes.c_int]
        lib.udfread_file_read.restype = ctypes.c_ssize_t
        lib.udfread_file_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t]
        _udf_library = _UdfLibrary(lib)
        return _udf_library


def udf_available() -> bool:
    return _load_udf_library() is not None


# ---- 直读句柄 ----

class _BdmvClip:
    """BDMV 目录内的 clip 文件（普通文件读，零依赖）。"""

    def __init__(self, path: Path):
        self.path = path
        self.handle = open(path, "rb")

    def size(self) -> int:
        return os.fstat(self.handle.fileno()).st_size

    def read_at(self, offset: int, length: int) -> bytes:
        self.handle.seek(offset)
        return self.handle.read(length)

    def close(self) -> None:
        self.handle.close()


class _IsoClip:
    """ISO 内的 clip 成员文件（libudfread 流式读）。"""

    def __init__(self, library: _UdfLibrary, volume, member: str, iso_path: str):
        self.library = library
        self.volume = volume
        self.member = member
        self.iso_path = iso_path
        self.handle = library.open_member(volume, member)

    def size(self) -> int:
        return self.library.size(self.handle)

    def read_at(self, offset: int, length: int) -> bytes:
        return self.library.read_at(self.handle, offset, length)

    def close(self) -> None:
        self.library.close_member(self.handle)


def clip_reader(root: Path, source_kind: str, clip_id: str):
    """打开一个 clip 的直读句柄（BDMV 目录 / ISO 两种素材形态）。

    调用方负责 close()；任何一步失败都抛 :class:`DirectReadError`（命名原因）。
    """
    root = Path(root)
    if source_kind == "bdmv":
        path = root / "BDMV" / "STREAM" / f"{clip_id}.m2ts"
        if not path.is_file():
            raise DirectReadError(f"BDMV 内找不到 clip 文件：{path}")
        return _BdmvClip(path)
    if source_kind == "iso":
        library = _load_udf_library()
        if library is None:
            raise UdfUnavailable(
                "执行器镜像缺少 libudfread，无法直读 ISO 内的素材文件"
                f"（需要它来绕开 libbluray 的 title 读层）：{root}")
        volume = library.open(str(root))
        try:
            return _IsoClip(library, volume, clip_member_path(clip_id), str(root))
        except Exception:
            library.close(volume)
            raise
    raise DirectReadError(f"未知素材形态：{source_kind}")


def clip_size(root: Path, source_kind: str, clip_id: str) -> int | None:
    """clip 文件长度（不可读返回 None——调用方据此放弃直读，不猜）。"""
    try:
        reader = clip_reader(root, source_kind, clip_id)
    except Exception:
        return None
    try:
        return reader.size()
    except Exception:
        return None
    finally:
        reader.close()


def build_direct_plan(
    root: Path,
    source_kind: str,
    clips: tuple,
    item_bounds: tuple[tuple[int, int], ...],
    *,
    size_of: Callable[[str], int | None] | None = None,
) -> tuple[DirectItem, ...] | None:
    """逐 item 证明"直读整个 clip 文件"与"按 item 读"等价；否则 None。

    等价条件（全部满足才成立）：
    - clip 数与 play item 数相同（多段合辑的裁剪式 item 不在此路径上）；
    - 每个 item 的虚拟流偏移 == 之前全部 clip 长度之和（bounds 已由解析层
      证明首尾相接）；
    - 该 item 的长度 == 对应 clip 文件的**全部字节**（pkt_count*192 与文件
      长度逐一相等——裁剪过的 item 必然对不上，因为包数由区间时长决定）；
      **或**是该 clip 的**前缀**（2026-09-16 新增，见下）。

    前缀形态（尾部松量）：允许 ``length < size``，条件是**缺口小到只可能是尾部
    包化松量**。判据用片子自己的事实算，不猜：

    - 该 item 的时长 = ``(out_time - in_time) / 90000``（两者同在 STC 轴上，
      差值即时长；**in_time 本身不是字节偏移**——生产实证：同一标题的 item0
      也有 in_time=600s，却与 clip 文件逐字节等长并被本管线长期接受）；
    - 平均码率 = ``size / 时长``；缺口预算 = **1 帧的时长 × 平均码率**
      （``FRAME_BUDGET_SECONDS``）。真实前导裁剪切掉的是**整帧起步**（≥1 帧），
      而尾部松量是包化产物（亚毫秒级）——两侧相隔一个数量级，故以 1 帧分界。
    - 生产标本（地球脉动.第三季 Disc.3 playlist 33 item1）：clip 24,515,162,112
      字节、item 24,515,161,344 字节（缺口 **768 字节 = 4 个包**），时长 3236.8 s
      ⇒ 平均 60.6 Mbps、1 帧约 316 KiB，缺口远在预算内；而该 item 此前因
      ``length != size`` 被整体拒绝 → 回落 libbluray → 被一个 88 KiB 的零填充洞
      挡死、整单失败。

    任何一项不满足都返回 None：按时间落点做区间裁剪需要 CPI 表映射，
    未实现的能力必须失败关闭。
    """
    if not clips or not item_bounds or len(clips) != len(item_bounds):
        return None
    sizes = size_of or (lambda clip_id: clip_size(root, source_kind, clip_id))
    plan: list[DirectItem] = []
    expected_start = 0
    for index, (clip, (start, length)) in enumerate(zip(clips, item_bounds)):
        size = sizes(clip.clip_id)
        tail_slack = None
        if size is not None and length < size:
            duration = (int(clip.out_time) - int(clip.in_time)) / 90000.0
            if duration > 0:
                average_rate = size / duration                 # 字节/秒
                slack_budget = FRAME_BUDGET_SECONDS * average_rate
                tail_slack = size - length
                if tail_slack <= slack_budget:
                    logger.info(
                        "直读尾部松量：item=%d clip=%s item 长度=%d clip 长度=%s "
                        "缺口=%d 字节（1 帧预算=%.0f 字节，时长=%.1fs）",
                        index, clip.clip_id, length, size, tail_slack,
                        slack_budget, duration)
                else:
                    logger.info(
                        "直读不可用：item=%d clip=%s 缺口=%d 字节超过 1 帧预算"
                        "（%.0f 字节，时长=%.1fs）——形如按时间裁剪的 item，"
                        "需要 CPI 映射，本管线不做近似",
                        index, clip.clip_id, tail_slack, slack_budget, duration)
                    return None
            else:
                logger.info(
                    "直读不可用：item=%d clip=%s 时长不可用"
                    "（in_time=%d out_time=%d）",
                    index, clip.clip_id, clip.in_time, clip.out_time)
                return None
        if size is None or start != expected_start or (
                length != size and tail_slack is None):
            logger.info(
                "直读不可用：item=%d clip=%s 期望起点=%d 实际起点=%d "
                "item 长度=%d clip 长度=%s in_time=%d",
                index, clip.clip_id, expected_start, start, length, size,
                clip.in_time)
            return None
        plan.append(DirectItem(item_index=index, clip_id=clip.clip_id,
                               offset=0, length=length))
        expected_start += length
    return tuple(plan)


def iter_direct_item(plan: tuple[DirectItem, ...], item_index: int,
                     chunk_size: int, *, root: Path, source_kind: str,
                     reader_factory: Callable | None = None) -> Iterator[bytes]:
    """按 chunk 产出某个 item 的字节（整个 clip 文件，或其前缀）。

    前缀形态：``plan`` 里 item 的长度可能**小于** clip 文件（见
    :func:`build_direct_plan` 的前缀规则），此时只读 ``[offset, offset+length)``
    即 item 自身的区间；文件**变小**（size < item.length）仍是硬错误。
    """
    if item_index < 0 or item_index >= len(plan):
        raise DirectReadError(f"直读 item 序号越界：{item_index}")
    item = plan[item_index]
    factory = reader_factory or (
        lambda clip_id: clip_reader(root, source_kind, clip_id))
    reader = factory(item.clip_id)
    try:
        size = reader.size()
        if size < item.length:
            raise DirectReadError(
                f"直读 clip 长度不足：{item.clip_id} 期望 {item.length} 实际 {size}")
        offset = item.offset
        remaining = item.length
        while remaining:
            request = min(chunk_size, remaining)
            data = reader.read_at(offset, request)
            if not data:
                raise DirectReadError(
                    f"直读提前结束：{item.clip_id} offset={offset}")
            if len(data) > request:
                data = data[:request]
            yield data
            offset += len(data)
            remaining -= len(data)
            if len(data) < request:
                raise DirectReadError(
                    f"直读短读：{item.clip_id} offset={offset} 期望 {request}")
    finally:
        reader.close()


@dataclass(frozen=True)
class PacketDamage:
    """一处已修补（或待修补）的源损伤事实——D3 证据面的最小单元。"""

    clip_id: str
    byte_offset: int
    packet_count: int
    lost_millis: float

    def as_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "byte_offset": self.byte_offset,
            "packet_count": self.packet_count,
            "lost_millis": round(self.lost_millis, 3),
        }


def _packet_sync_ok(packet: bytes) -> bool:
    """TS 同步字节（包内第 5 字节 == 0x47）。零填充/失同步的槽位必然不满足。"""
    return len(packet) >= PACKET_SIZE and packet[4] == 0x47


def _packet_ats(packet: bytes) -> tuple[int, int]:
    """返回 (copy_permission 高 2 位, 30 位 ATS)。"""
    prefix = int.from_bytes(packet[:4], "big")
    return prefix >> 30, prefix & ATS_MASK


def normalize_packet_damage(
    chunks: Iterator[bytes],
    *,
    clip_id: str,
    budget_millis: float = DAMAGE_BUDGET_MILLIS,
    total_budget_millis: float = DAMAGE_TOTAL_BUDGET_MILLIS,
    spent_millis: float = 0.0,
    on_repair: Callable[[PacketDamage], None] | None = None,
) -> Iterator[bytes]:
    """把「整包对齐的失同步槽位」就地改写成合法空包，长度逐字节不变。

    生产标本（dlv_93c13946d7c0eac7c0b9，地球脉动.第三季 第 3 张碟）：源文件里
    469 个整包被**就地零填充**（网格/PID/时间轴都完好），按本地码率折合 9.2 ms
    ≈ 0.22 帧；但下游（mkvmerge/ffprobe/拼接门）看到的是一段**结构非法**的字节。

    修法（用户 2026-09-16 裁定预算 3 帧）：把该区段改写为合法空包
    （PID 0x1FFF + 184 字节 0xFF），**总长度不变**——item 长度、clip 长度、
    包网格、字节偏移、bounds 全部不变，下游不需要为"修复过的文件"开任何特例；
    解码器看到的就是一段载荷缺失。ATS 按洞前后两端的**真实到达时间线性插值**
    （并沿用洞前包的 copy-permission 位），保证时间轴单调、无 0 值回跳。

    判定与失败关闭（宁可不修）：
    - 洞的**时长**取"洞前最后一个合法包 ATS → 洞后第一个合法包 ATS"之差
      （27 MHz），不用字节估算码率——这是容器自己的时钟，精确且无需猜测；
    - 单处 ≤ ``budget_millis`` 且累计 ≤ ``total_budget_millis`` 才修；
    - 洞**贴着 item 首包或尾包**（没有可用的两侧锚点）→ 失败关闭；
    - ATS 不可用（缺失、回退、越界）→ 失败关闭；
    - 非整包对齐的尾部余数原样透传（不参与改写）。
    """
    buffer = bytearray()
    spent = [spent_millis]
    prev_good: bytes | None = None          # 洞前最后一个合法包
    pending_bad: list[bytes] = []           # 连续失同步槽位（等待右侧锚点）
    pending_offset = 0

    def flush_bad(next_good: bytes | None) -> bytes:
        """处理一段连续坏包：能修则返回改写后的字节，否则抛错。"""
        if not pending_bad:
            return b""
        head = pending_bad[0]
        left = prev_good
        if left is None:
            raise DirectReadError(
                f"直读源损伤位于 item 起始处（clip={clip_id} offset={pending_offset}，"
                f"{len(pending_bad)} 个包）：无可信左侧锚点，拒绝修补")
        if next_good is None:
            raise DirectReadError(
                f"直读源损伤位于 item 末尾（clip={clip_id} offset={pending_offset}，"
                f"{len(pending_bad)} 个包）：无可信右侧锚点，拒绝修补")
        _, ats_left = _packet_ats(left)
        _, ats_right = _packet_ats(next_good)
        span = ats_right - ats_left
        if span <= 0 or ats_left == 0 or ats_right == 0:
            raise DirectReadError(
                f"直读源损伤两侧 ATS 不可用（clip={clip_id} offset={pending_offset}，"
                f"ats_left={ats_left} ats_right={ats_right}）：拒绝修补")
        lost_millis = span / ATS_CLOCK_HZ * 1000.0
        if lost_millis > budget_millis:
            raise DirectReadError(
                f"直读源损伤超预算（clip={clip_id} offset={pending_offset}，"
                f"{len(pending_bad)} 个包，{lost_millis:.1f} ms > {budget_millis:.0f} ms）："
                f"拒绝修补，按失败关闭处理")
        if spent[0] + lost_millis > total_budget_millis:
            raise DirectReadError(
                f"直读源损伤累计超预算（clip={clip_id} offset={pending_offset}，"
                f"本次 {lost_millis:.1f} ms，累计 {spent[0] + lost_millis:.1f} ms > "
                f"{total_budget_millis:.0f} ms）：拒绝修补，按失败关闭处理")
        flags, _ = _packet_ats(head)
        count = len(pending_bad)
        out = bytearray()
        for index in range(count):
            # 线性插值：把 [ats_left, ats_right] 均分给 count 个槽位
            ats = ats_left + span * (index + 1) // (count + 1)
            out += ((flags << 30) | (ats & ATS_MASK)).to_bytes(4, "big") + NULL_TS_BYTES
        spent[0] += lost_millis
        damage = PacketDamage(clip_id=clip_id, byte_offset=pending_offset,
                              packet_count=count, lost_millis=lost_millis)
        logger.info(
            "源损伤已就地修补：clip=%s offset=%d 包数=%d 丢失=%.1f ms（累计 %.1f ms）",
            clip_id, pending_offset, count, lost_millis, spent[0])
        if on_repair is not None:
            on_repair(damage)
        pending_bad.clear()
        return bytes(out)

    consumed = 0
    for chunk in chunks:
        buffer += chunk
        while len(buffer) >= PACKET_SIZE:
            packet = bytes(buffer[:PACKET_SIZE])
            del buffer[:PACKET_SIZE]
            offset_of_packet = consumed
            consumed += PACKET_SIZE
            if _packet_sync_ok(packet):
                out = flush_bad(packet)
                if out:
                    yield out
                prev_good = packet
                yield packet
            else:
                if not pending_bad:
                    pending_offset = offset_of_packet
                pending_bad.append(packet)
    if pending_bad:
        flush_bad(None)                      # 尾部坏包：失败关闭
    if buffer:
        yield bytes(buffer)                  # 非整包余数原样透传


def null_pid_ratio(data: bytes) -> float:
    """采样窗口内空包 PID 占比（非整包长度按整包截断）。"""
    packets = len(data) // PACKET_SIZE
    if packets == 0:
        return 0.0
    nulls = 0
    for index in range(packets):
        base = index * PACKET_SIZE
        pid = (data[base + 5] << 8 | data[base + 6]) & 0x1FFF
        if pid == NULL_PID:
            nulls += 1
    return nulls / packets


def require_visible_pid(windows: list[tuple[str, bytes]]) -> None:
    """采样窗口全为空包 PID → 失败关闭并点名（读路径被改写的典型签名）。

    只否决"**每个**窗口的**每个**采样包都是空包"这一种情况：真实流在某个
    区间可能以空包填充，但不会所有采样窗口都如此。全空包意味着下游只会
    拿到 mkvmerge/ffmpeg 的天书报错，必须在取字节这一步就失败并说清原因。
    """
    if not windows:
        return
    evaluated = [(label, null_pid_ratio(data)) for label, data in windows
                 if len(data) >= PACKET_SIZE]
    if not evaluated:
        return
    if all(ratio >= 1.0 for _label, ratio in evaluated):
        detail = "；".join(f"{label}: 空包 PID {ratio:.0%}"
                           for label, ratio in evaluated)
        raise DirectReadError(
            "取到的字节全部是 TS 空包 PID(0x1FFF)：读路径被改写或源不可用，"
            f"拒绝把空流交给下游封装（样本：{detail}）")
