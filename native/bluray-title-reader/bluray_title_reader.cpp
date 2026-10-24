// Persistent libbluray main-title reader for HDAtHome secure packaging.
//
// The helper deliberately exposes small interfaces:
//   --probe SOURCE
//   --list SOURCE
//   --streams SOURCE TITLE_INDEX
//   --item-bounds SOURCE TITLE_INDEX
//   --serve SOURCE TITLE_INDEX
//
// It does not remux or transcode. libbluray applies the MPLS play-item order
// and boundaries and returns the original 192-byte transport packets.
//
// --item-bounds prints one "ITEM\t{index}\t{start}\t{length}\n" line per play
// item (MPLS order). The ranges partition the title virtual byte stream, so a
// clip that occurs K times in the MPLS yields K independent ranges.

#include <libbluray/bluray.h>

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <limits>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

constexpr std::uint64_t kMaxRead = 64ULL * 1024ULL * 1024ULL;
constexpr std::size_t kBufferSize = 1024ULL * 1024ULL;
constexpr std::uint64_t kPacketSize = 192ULL;

struct SelectedTitle {
    std::uint32_t index = 0;
    std::uint32_t playlist = 0;
    std::uint64_t duration = 0;
    std::uint64_t size = 0;
    std::uint32_t clip_count = 0;
};

std::string language_code(const uint8_t lang[4]) {
    std::string value;
    for (int index = 0; index < 3 && lang[index] != 0; ++index) {
        value.push_back(static_cast<char>(lang[index]));
    }
    return value;
}

void usage() {
    std::cerr << "usage:\n"
              << "  hdathome-bluray-title-reader --probe BDMV_ROOT_OR_ISO\n"
              << "  hdathome-bluray-title-reader --streams BDMV_ROOT_OR_ISO TITLE_INDEX\n"
              << "  hdathome-bluray-title-reader --item-bounds BDMV_ROOT_OR_ISO TITLE_INDEX\n"
              << "  hdathome-bluray-title-reader --serve BDMV_ROOT_OR_ISO TITLE_INDEX\n";
}

bool parse_u64(const char* value, std::uint64_t* output) {
    if (value == nullptr || *value == '\0' || value[0] == '-') {
        return false;
    }
    char* end = nullptr;
    errno = 0;
    const unsigned long long parsed = std::strtoull(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0') {
        return false;
    }
    *output = static_cast<std::uint64_t>(parsed);
    return true;
}

bool is_disc_source(const std::string& source) {
    std::error_code error;
    const std::filesystem::path path(source);
    if (std::filesystem::is_regular_file(path, error) && !error) {
        const std::string extension = path.extension().string();
        return extension == ".iso" || extension == ".ISO";
    }
    if (error || !std::filesystem::is_directory(path, error) || error) {
        return false;
    }
    const std::filesystem::path index = path / "BDMV" / "index.bdmv";
    return std::filesystem::is_regular_file(index, error) && !error;
}

BLURAY* open_disc(const std::string& source) {
    if (!is_disc_source(source)) {
        std::cerr << "BDMV/index.bdmv or ISO image not found: " << source << '\n';
        return nullptr;
    }
    // libbluray accepts either a mounted BDMV directory or a Blu-ray image
    // path. Both sources therefore use the same MPLS title selection/order.
    BLURAY* disc = bd_open(source.c_str(), nullptr);
    if (disc == nullptr) {
        std::cerr << "libbluray cannot open Blu-ray source: " << source << '\n';
    }
    return disc;
}

bool collect_titles(BLURAY* disc, std::vector<SelectedTitle>* titles);

bool select_longest_title(BLURAY* disc, SelectedTitle* selected) {
    std::vector<SelectedTitle> titles;
    if (!collect_titles(disc, &titles)) {
        return false;
    }

    SelectedTitle best = titles.front();
    for (const SelectedTitle& value : titles) {
        if (value.duration > best.duration ||
            (value.duration == best.duration && value.index < best.index)) {
            best = value;
        }
    }
    if (!bd_select_title(disc, best.index)) {
        std::cerr << "libbluray cannot select a relevant title\n";
        return false;
    }
    *selected = best;
    return true;
}

bool collect_titles(BLURAY* disc, std::vector<SelectedTitle>* titles) {
    titles->clear();
    const std::uint32_t count = bd_get_titles(disc, TITLES_RELEVANT, 0);
    if (count == 0) {
        std::cerr << "libbluray found no relevant titles\n";
        return false;
    }

    for (std::uint32_t index = 0; index < count; ++index) {
        BLURAY_TITLE_INFO* info = bd_get_title_info(disc, index, 0);
        if (info == nullptr) {
            continue;
        }
        SelectedTitle value;
        value.index = index;
        value.playlist = info->playlist;
        value.duration = info->duration;
        value.clip_count = info->clip_count;
        bd_free_title_info(info);
        if (value.duration == 0 || value.clip_count == 0 ||
            !bd_select_title(disc, index)) {
            continue;
        }
        value.size = bd_get_title_size(disc);
        if (value.size == 0) {
            continue;
        }
        titles->push_back(value);
    }
    if (titles->empty()) {
        std::cerr << "selected Blu-ray title has no playable data\n";
        return false;
    }
    return true;
}

bool select_title(BLURAY* disc, std::uint32_t title_index,
                  SelectedTitle* selected) {
    const std::uint32_t count = bd_get_titles(disc, TITLES_RELEVANT, 0);
    if (title_index >= count) {
        std::cerr << "title index out of range\n";
        return false;
    }
    BLURAY_TITLE_INFO* info = bd_get_title_info(disc, title_index, 0);
    if (info == nullptr) {
        std::cerr << "libbluray cannot inspect selected title\n";
        return false;
    }
    SelectedTitle value;
    value.index = title_index;
    value.playlist = info->playlist;
    value.duration = info->duration;
    value.clip_count = info->clip_count;
    bd_free_title_info(info);
    if (!bd_select_title(disc, title_index)) {
        std::cerr << "libbluray cannot select requested title\n";
        return false;
    }
    value.size = bd_get_title_size(disc);
    if (value.size == 0) {
        std::cerr << "selected Blu-ray title has zero size\n";
        return false;
    }
    *selected = value;
    return true;
}

void print_streams(BLURAY* disc, std::uint32_t title_index) {
    BLURAY_TITLE_INFO* info = bd_get_title_info(disc, title_index, 0);
    if (info == nullptr || info->clip_count == 0 || info->clips == nullptr) {
        if (info != nullptr) {
            bd_free_title_info(info);
        }
        return;
    }

    // Ordered clip sequence: the MPLS play-item order and its 90 kHz
    // in/out points are the authoritative playback identity of a title.
    // Edition/Part matching and duplicate-playlist deduplication depend on
    // this fingerprint; multi-clip seamless branching stays one title.
    for (std::uint32_t clip_index = 0; clip_index < info->clip_count; ++clip_index) {
        const BLURAY_CLIP_INFO& clip = info->clips[clip_index];
        char clip_id[6] = {0, 0, 0, 0, 0, 0};
        if (clip.clip_id[0] != 0) {
            for (int i = 0; i < 5 && clip.clip_id[i] != 0; ++i) {
                clip_id[i] = clip.clip_id[i];
            }
        }
        std::cout << "CLIP\t" << clip_index << '\t' << clip_id << '\t'
                  << clip.in_time << '\t' << clip.out_time << '\n';
    }

    // Branching/seamless playlists may introduce streams after the first
    // play-item. Report the stable union across the complete MPLS while
    // leaving every original 192-byte transport packet untouched.
    using StreamKey = std::tuple<std::uint16_t, unsigned int, std::string>;
    std::set<StreamKey> seen_video;
    std::set<StreamKey> seen_audio;
    std::set<StreamKey> seen_subtitles;
    std::uint32_t video_index = 0;
    std::uint32_t audio_index = 0;
    std::uint32_t subtitle_index = 0;
    for (std::uint32_t clip_index = 0; clip_index < info->clip_count; ++clip_index) {
        const BLURAY_CLIP_INFO& clip = info->clips[clip_index];
        // Primary video streams include both base and enhancement layers on
        // Dolby Vision discs; the packaging layer needs the exact count to
        // refuse silent HDR10-only downgrades.
        for (std::uint32_t index = 0; index < clip.video_stream_count; ++index) {
            const BLURAY_STREAM_INFO& stream = clip.video_streams[index];
            const StreamKey key(stream.pid,
                                static_cast<unsigned int>(stream.coding_type),
                                "");
            if (!seen_video.insert(key).second) {
                continue;
            }
            const std::uint32_t current_video_index = video_index++;
            std::cout << "STREAM\tVIDEO\t" << current_video_index << '\t'
                      << stream.pid << '\t'
                      << static_cast<unsigned int>(stream.coding_type) << '\t'
                      << language_code(stream.lang) << '\n';
            // Authoritative video signature fields (libbluray parses the HDMV
            // stream attributes): video format (resolution/interlacing) and
            // frame rate.  Alternate-source matching must compare these real
            // attributes, never a guessed "2160p".  HDR/DV profile, RPU and
            // enhancement-layer status are deliberately NOT guessed here; a
            // source whose signature lacks them is incomplete and can never
            // be merged as an alternate source.
            std::cout << "VINFO\t" << current_video_index << '\t'
                      << static_cast<unsigned int>(stream.format) << '\t'
                      << static_cast<unsigned int>(stream.rate) << '\n';
        }
        for (std::uint32_t index = 0; index < clip.audio_stream_count; ++index) {
            const BLURAY_STREAM_INFO& stream = clip.audio_streams[index];
            const std::string language = language_code(stream.lang);
            const StreamKey key(stream.pid,
                                static_cast<unsigned int>(stream.coding_type),
                                language);
            if (!seen_audio.insert(key).second) {
                continue;
            }
            std::cout << "STREAM\tAUDIO\t" << audio_index++ << '\t'
                      << stream.pid << '\t'
                      << static_cast<unsigned int>(stream.coding_type) << '\t'
                      << language << '\n';
        }
        for (std::uint32_t index = 0; index < clip.pg_stream_count; ++index) {
            const BLURAY_STREAM_INFO& stream = clip.pg_streams[index];
            const std::string language = language_code(stream.lang);
            const StreamKey key(stream.pid,
                                static_cast<unsigned int>(stream.coding_type),
                                language);
            if (!seen_subtitles.insert(key).second) {
                continue;
            }
            std::cout << "STREAM\tSUBTITLE\t" << subtitle_index++ << '\t'
                      << stream.pid << '\t'
                      << static_cast<unsigned int>(stream.coding_type) << '\t'
                      << language << '\n';
        }
    }
    std::cout.flush();
    bd_free_title_info(info);
}

// 追加读取 length 字节：不清空 output，因为定位留下的 lookahead 字节已经
// 排在前面，覆盖它们就等于丢掉真正要读的数据。
bool read_exact(BLURAY* disc, std::uint64_t length,
                std::vector<unsigned char>* output) {
    output->reserve(output->size() + static_cast<std::size_t>(length));
    std::vector<unsigned char> buffer(kBufferSize);
    std::uint64_t remaining = length;
    while (remaining > 0) {
        const int request = static_cast<int>(std::min<std::uint64_t>(
            remaining, static_cast<std::uint64_t>(buffer.size())));
        const int received = bd_read(disc, buffer.data(), request);
        if (received <= 0) {
            return false;
        }
        output->insert(output->end(), buffer.begin(), buffer.begin() + received);
        remaining -= static_cast<std::uint64_t>(received);
    }
    return true;
}

bool discard_exact(BLURAY* disc, std::uint64_t length) {
    std::vector<unsigned char> buffer(kBufferSize);
    std::uint64_t remaining = length;
    while (remaining > 0) {
        const int request = static_cast<int>(std::min<std::uint64_t>(
            remaining, static_cast<std::uint64_t>(buffer.size())));
        const int received = bd_read(disc, buffer.data(), request);
        if (received <= 0) {
            return false;
        }
        remaining -= static_cast<std::uint64_t>(received);
    }
    return true;
}

struct ReadCursor {
    // 下一个要交付给调用方的标题字节位置（不等于 libbluray 的读取指针：
    // 定位时多读出来的字节先留在 lookahead 里）。
    std::uint64_t position = 0;
    // bd_seek_playitem()/bd_get_title_info() 路径会在游标不知情的情况下移动
    // 底层读取指针；游标未知时下一次读取必须重新定位，不沿用旧状态。
    bool known = true;
    // 定位时多读出来的字节（seek 回退与探测读取的产物），从 position 起有效。
    std::vector<unsigned char> lookahead;

    // 位置作废：lookahead 只对旧位置有效，必须一起丢掉。
    void invalidate() {
        known = false;
        lookahead.clear();
    }
};

// 顺序读取（游标已知且位置一致）不需要重新定位，也就不需要 item 表；
// 位置不可信时必须先解析出包含目标偏移的 play item。
bool needs_reposition(const ReadCursor& cursor, std::uint64_t offset) {
    return !cursor.known || cursor.position != offset;
}

// Per-play-item byte ranges inside the title virtual stream. A clip that
// occurs K times in the MPLS yields K independent ranges: multi-clip
// ("play all"/seamless branching) titles are only muxable item by item.
//
// bd_seek_playitem() is the only byte-positioning primitive libbluray offers
// (bd_seek()/bd_seek_time() are not byte-exact: bd_seek only snaps to the
// closest access unit). It returns bd->s_pos (= clip->title_pkt * 192, the
// byte position of the item start) and does NOT signal failure through its
// return value, so a seek result is only trusted when it equals the
// independently computed packet partition of the selected title. Any
// disagreement fails closed: a wrong boundary would silently shift every
// following item.
class ItemBounds {
public:
    // Resolves the clip list and the title packet partition (no seek yet).
    bool init(BLURAY* disc, std::uint32_t title_index, std::uint64_t title_size);
    std::uint32_t count() const { return count_; }
    // Positions the read pointer at one item start and returns its byte range.
    // bd_seek_playitem() is what moves the read pointer, so every call seeks
    // again; a cached start would leave the pointer at another item.
    bool item(std::uint32_t index, std::uint64_t* start, std::uint64_t* length);
    // Streaming variant: the verified range is cached after the first use.
    // Sequential item reads must not seek once per chunk: start_of() plus
    // discard_exact(offset) makes a 22.9 GB item cost O(n^2) (the byte prefix
    // is walked again for every request). *seeked reports whether the read
    // pointer moved (the caller must then invalidate its cursor); an already
    // verified item leaves the pointer untouched.
    bool item_cached(std::uint32_t index, std::uint64_t* start,
                     std::uint64_t* length, bool* seeked);
    // Positions the read pointer at the play item containing offset and
    // returns that item's start (expected_[k] <= offset < expected_[k+1]).
    bool reposition(std::uint64_t offset, std::uint64_t* start);

private:
    bool start_of(std::uint32_t index, std::uint64_t* start);

    BLURAY* disc_ = nullptr;
    std::uint32_t count_ = 0;
    std::uint64_t title_size_ = 0;
    std::vector<std::uint64_t> expected_;
    // 已验证过的 item 区间（首次使用经 start_of() 的校验式 seek，之后复用）。
    std::vector<char> verified_;
    std::vector<std::uint64_t> verified_start_;
    std::vector<std::uint64_t> verified_length_;
};

bool ItemBounds::init(BLURAY* disc, std::uint32_t title_index,
                      std::uint64_t title_size) {
    disc_ = nullptr;
    count_ = 0;
    title_size_ = 0;
    expected_.clear();
    verified_.clear();
    verified_start_.clear();
    verified_length_.clear();
    if (disc == nullptr || title_size == 0 || title_size % kPacketSize != 0) {
        return false;
    }
    BLURAY_TITLE_INFO* info = bd_get_title_info(disc, title_index, 0);
    if (info == nullptr || info->clip_count == 0 || info->clips == nullptr) {
        if (info != nullptr) {
            bd_free_title_info(info);
        }
        return false;
    }
    const std::uint32_t count = info->clip_count;
    std::vector<std::uint64_t> lengths(count, 0);
    std::uint64_t total = 0;
    for (std::uint32_t index = 0; index < count; ++index) {
        lengths[index] = static_cast<std::uint64_t>(info->clips[index].pkt_count) *
                         kPacketSize;
        total += lengths[index];
    }
    bd_free_title_info(info);
    // The per-item packet counts and the selected title size are two
    // independent views of the same MPLS; a mismatch means the play item
    // list cannot be mapped onto the title byte stream.
    if (total != title_size) {
        return false;
    }
    verified_.assign(count, 0);
    verified_start_.assign(count, 0);
    verified_length_.assign(count, 0);
    expected_.assign(count, 0);
    std::uint64_t position = 0;
    for (std::uint32_t index = 0; index < count; ++index) {
        expected_[index] = position;
        position += lengths[index];
    }
    disc_ = disc;
    count_ = count;
    title_size_ = title_size;
    return true;
}

bool ItemBounds::start_of(std::uint32_t index, std::uint64_t* start) {
    if (disc_ == nullptr || index >= count_) {
        return false;
    }
    const std::int64_t position = bd_seek_playitem(disc_, index);
    if (position < 0 ||
        static_cast<std::uint64_t>(position) != expected_[index]) {
        return false;
    }
    *start = static_cast<std::uint64_t>(position);
    return true;
}

bool ItemBounds::item(std::uint32_t index, std::uint64_t* start,
                      std::uint64_t* length) {
    std::uint64_t item_start = 0;
    if (!start_of(index, &item_start)) {
        return false;
    }
    const std::uint64_t end =
        index + 1 < count_ ? expected_[index + 1] : title_size_;
    if (end <= item_start || end > title_size_) {
        return false;
    }
    *start = item_start;
    *length = end - item_start;
    return true;
}

bool ItemBounds::item_cached(std::uint32_t index, std::uint64_t* start,
                             std::uint64_t* length, bool* seeked) {
    if (seeked != nullptr) {
        *seeked = false;
    }
    if (disc_ == nullptr || index >= count_) {
        return false;
    }
    if (index < verified_.size() && verified_[index]) {
        *start = verified_start_[index];
        *length = verified_length_[index];
        return true;
    }
    std::uint64_t item_start = 0;
    std::uint64_t item_length = 0;
    if (!item(index, &item_start, &item_length)) {
        return false;
    }
    verified_[index] = 1;
    verified_start_[index] = item_start;
    verified_length_[index] = item_length;
    *start = item_start;
    *length = item_length;
    if (seeked != nullptr) {
        *seeked = true;
    }
    return true;
}

bool ItemBounds::reposition(std::uint64_t offset, std::uint64_t* start) {
    if (disc_ == nullptr || offset >= title_size_) {
        return false;
    }
    // 包含 offset 的 play item 是最后一个满足 expected_[k] <= offset 的段；
    // offset 0 恒落在第 0 段（expected_[0] == 0），丢弃量因此为 0。
    std::uint32_t index = 0;
    while (index + 1 < count_ && expected_[index + 1] <= offset) {
        ++index;
    }
    return start_of(index, start);
}

bool collect_item_bounds(BLURAY* disc, std::uint32_t title_index,
                         std::uint64_t title_size,
                         std::vector<std::pair<std::uint64_t, std::uint64_t>>* bounds) {
    // Every item is resolved, so a silent seek failure anywhere in the MPLS
    // is rejected instead of printing a shifted table.
    ItemBounds table;
    if (!table.init(disc, title_index, title_size)) {
        return false;
    }
    bounds->clear();
    for (std::uint32_t index = 0; index < table.count(); ++index) {
        std::uint64_t start = 0;
        std::uint64_t length = 0;
        if (!table.item(index, &start, &length)) {
            bounds->clear();
            return false;
        }
        bounds->push_back(std::make_pair(start, length));
    }
    return true;
}

// 从流中取走 length 字节：先消费定位留下的 lookahead，不足的部分再 bd_read。
// output 为 nullptr 表示只丢弃。取走后游标位置前进 length 字节。
bool take_exact(BLURAY* disc, std::uint64_t length, ReadCursor* cursor,
                std::vector<unsigned char>* output) {
    if (output != nullptr) {
        output->clear();
        output->reserve(static_cast<std::size_t>(length));
    }
    const std::size_t buffered = std::min<std::size_t>(
        cursor->lookahead.size(), static_cast<std::size_t>(length));
    if (buffered > 0) {
        if (output != nullptr) {
            output->insert(output->end(), cursor->lookahead.begin(),
                           cursor->lookahead.begin() + buffered);
        }
        cursor->lookahead.erase(cursor->lookahead.begin(),
                               cursor->lookahead.begin() + buffered);
    }
    const std::uint64_t remaining = length - buffered;
    if (remaining > 0) {
        if (output != nullptr) {
            if (!read_exact(disc, remaining, output)) {
                return false;
            }
        } else if (!discard_exact(disc, remaining)) {
            return false;
        }
    }
    cursor->position += length;
    return true;
}

// 把读取位置精确落到 offset。按字节定位只能经 bd_seek_playitem()：
// bd_seek()/bd_seek_time() 都不以字节为单位（bd_seek 只对齐到最近的接入
// 单元），而且任何 seek 之后 libbluray 的第一次读取会把定位点之前的
// PAT/PMT/PCR 包一起返回（最多一个 6144 字节对齐单元），使流位置早于请求
// 的偏移。所以定位以 bd_tell() 报告的实际位置为准：多读出来的字节留在
// lookahead，仍不足才继续丢弃（丢弃量至多一段 play item 加一个探测包），
// 位置对不上就不供字节。
bool position_at(BLURAY* disc, ItemBounds* item_bounds,
                 std::uint64_t title_size, std::uint64_t offset,
                 ReadCursor* cursor) {
    std::uint64_t item_start = 0;
    if (item_bounds == nullptr ||
        !item_bounds->reposition(offset, &item_start) ||
        item_start > offset || title_size <= item_start) {
        return false;
    }
    cursor->invalidate();
    // item 起点附近读一个包，让 seek 回退在正式读取之前先发生。
    const std::uint64_t probe_length =
        std::min<std::uint64_t>(kPacketSize, title_size - item_start);
    std::vector<unsigned char> probe;
    if (!read_exact(disc, probe_length, &probe) || probe.empty()) {
        return false;
    }
    const std::uint64_t actual = bd_tell(disc);
    if (actual < probe.size()) {
        return false;
    }
    const std::uint64_t probe_start = actual - probe.size();
    if (offset < probe_start) {
        return false;
    }
    if (offset <= actual) {
        // 目标落在已读出的字节里：只丢弃它之前的字节，其余留给下一次读取。
        cursor->lookahead.assign(
            probe.begin() + static_cast<std::ptrdiff_t>(offset - probe_start),
            probe.end());
    } else if (!discard_exact(disc, offset - actual) ||
               bd_tell(disc) != offset) {
        // 丢弃后位置仍不是目标（越过标题结尾或计数异常）就失败，不猜位置。
        return false;
    }
    cursor->position = offset;
    cursor->known = true;
    return true;
}

bool read_range(BLURAY* disc, std::uint64_t title_size,
                std::uint64_t offset, std::uint64_t length,
                ItemBounds* item_bounds, ReadCursor* cursor,
                std::vector<unsigned char>* output) {
    if (offset > title_size || length > title_size - offset ||
        length > kMaxRead) {
        return false;
    }
    if (length == 0) {
        output->clear();
        // 零长度请求不移动读取位置：位置相符时游标仍然有效，否则连同
        // lookahead 一起作废，由下一次读取重新定位。
        if (needs_reposition(*cursor, offset)) {
            cursor->invalidate();
        }
        return true;
    }
    if (needs_reposition(*cursor, offset) &&
        !position_at(disc, item_bounds, title_size, offset, cursor)) {
        return false;
    }
    return take_exact(disc, length, cursor, output);
}

void send_error(const std::string& message) {
    std::string safe = message;
    std::replace(safe.begin(), safe.end(), '\t', ' ');
    std::replace(safe.begin(), safe.end(), '\n', ' ');
    std::cout << "ERR\t" << safe << '\n' << std::flush;
}

void send_payload(const std::vector<unsigned char>& payload) {
    std::cout << "OK\t" << payload.size() << '\n';
    if (!payload.empty()) {
        std::cout.write(reinterpret_cast<const char*>(payload.data()),
                        static_cast<std::streamsize>(payload.size()));
    }
    std::cout.flush();
}

int probe(const std::string& root) {
    BLURAY* disc = open_disc(root);
    if (disc == nullptr) {
        return 3;
    }
    SelectedTitle selected;
    const bool success = select_longest_title(disc, &selected);
    if (success) {
        std::cout << "SELECTED\t" << selected.index << '\t'
                  << selected.playlist << '\t' << selected.duration << '\t'
                  << selected.size << '\t' << selected.clip_count << '\n';
        // Keep automatic title selection consistent with the manual
        // --streams path.  The selected title's elementary-stream metadata
        // is additive; the original TS/M2TS bytes are still untouched.
        print_streams(disc, selected.index);
    }
    bd_close(disc);
    return success ? 0 : 4;
}

int list_titles(const std::string& root) {
    BLURAY* disc = open_disc(root);
    if (disc == nullptr) {
        return 3;
    }
    std::vector<SelectedTitle> titles;
    const bool success = collect_titles(disc, &titles);
    if (success) {
        for (const SelectedTitle& title : titles) {
            std::cout << "TITLE\t" << title.index << '\t'
                      << title.playlist << '\t' << title.duration << '\t'
                      << title.size << '\t' << title.clip_count << '\n';
        }
        std::cout.flush();
    }
    bd_close(disc);
    return success ? 0 : 4;
}

int streams(const std::string& root, std::uint32_t title_index) {
    BLURAY* disc = open_disc(root);
    if (disc == nullptr) {
        return 3;
    }
    const std::uint32_t count = bd_get_titles(disc, TITLES_RELEVANT, 0);
    if (title_index >= count) {
        bd_close(disc);
        return 4;
    }
    print_streams(disc, title_index);
    bd_close(disc);
    return 0;
}

int print_item_bounds(const std::string& root, std::uint32_t title_index) {
    BLURAY* disc = open_disc(root);
    if (disc == nullptr) {
        return 3;
    }
    SelectedTitle selected;
    if (!select_title(disc, title_index, &selected)) {
        bd_close(disc);
        return 4;
    }
    std::vector<std::pair<std::uint64_t, std::uint64_t>> bounds;
    if (!collect_item_bounds(disc, title_index, selected.size, &bounds)) {
        std::cerr << "libbluray cannot map MPLS play items to title bytes\n";
        bd_close(disc);
        return 4;
    }
    for (std::size_t index = 0; index < bounds.size(); ++index) {
        std::cout << "ITEM\t" << index << '\t' << bounds[index].first << '\t'
                  << bounds[index].second << '\n';
    }
    std::cout.flush();
    bd_close(disc);
    return 0;
}

int serve(const std::string& root, std::uint32_t title_index) {
    BLURAY* disc = open_disc(root);
    if (disc == nullptr) {
        return 3;
    }
    SelectedTitle selected;
    if (!select_title(disc, title_index, &selected)) {
        bd_close(disc);
        return 4;
    }

    ReadCursor cursor;
    ItemBounds item_bounds;
    bool item_bounds_ready = false;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "QUIT") {
            break;
        }
        std::istringstream request(line);
        std::string command;
        if (!std::getline(request, command, '\t')) {
            send_error("invalid request");
            continue;
        }
        std::string raw_item;
        std::string raw_offset;
        std::string raw_length;
        if (command == "READ") {
            // READ keeps its original field validation: the last field is
            // read to end of line, so a surplus tab makes it non-numeric.
            if (!std::getline(request, raw_offset, '\t') ||
                !std::getline(request, raw_length)) {
                send_error("invalid request");
                continue;
            }
        } else if (command == "READITEM") {
            if (!std::getline(request, raw_item, '\t') ||
                !std::getline(request, raw_offset, '\t') ||
                !std::getline(request, raw_length)) {
                send_error("invalid request");
                continue;
            }
        } else {
            send_error("invalid request");
            continue;
        }
        std::uint64_t item_index = 0;
        std::uint64_t offset = 0;
        std::uint64_t length = 0;
        if (!parse_u64(raw_offset.c_str(), &offset) ||
            !parse_u64(raw_length.c_str(), &length) ||
            (command == "READITEM" &&
             (!parse_u64(raw_item.c_str(), &item_index) ||
              item_index > std::numeric_limits<std::uint32_t>::max()))) {
            send_error("invalid range");
            continue;
        }

        if (command == "READ") {
            // 顺序读取不重新定位，也就不需要 item 表；只有会真正重定位的
            // 跳跃读取才解析包含目标偏移的 play item，失败则不供字节。
            if (length > 0 && needs_reposition(cursor, offset) &&
                !item_bounds_ready) {
                item_bounds_ready =
                    item_bounds.init(disc, title_index, selected.size);
                if (!item_bounds_ready) {
                    send_error("item bounds unavailable");
                    continue;
                }
            }
            std::vector<unsigned char> output;
            if (!read_range(disc, selected.size, offset, length,
                            &item_bounds, &cursor, &output)) {
                send_error("read failed");
                continue;
            }
            send_payload(output);
            continue;
        }

        if (!item_bounds_ready) {
            // Lazy: the MPLS play items are mapped to title bytes on the
            // first READITEM, then reused for every later request.
            item_bounds_ready =
                item_bounds.init(disc, title_index, selected.size);
            if (!item_bounds_ready) {
                send_error("item bounds unavailable");
                continue;
            }
        }
        if (item_index >= item_bounds.count()) {
            send_error("item out of range");
            continue;
        }
        std::uint64_t item_start = 0;
        std::uint64_t item_length = 0;
        bool seeked = false;
        if (!item_bounds.item_cached(static_cast<std::uint32_t>(item_index),
                                     &item_start, &item_length, &seeked)) {
            send_error("item bounds unavailable");
            continue;
        }
        // The item range must stay inside its own play item: reading past an
        // item end would splice the next item's bytes into this one.
        if (offset > item_length || length > item_length - offset) {
            send_error("item range invalid");
            continue;
        }
        std::vector<unsigned char> output;
        // 只有刚刚为验证本段区间而 seek 过时才作废游标：已验证过的 item 不再
        // 每个 chunk 重新 seek + 丢弃段内前缀（那是 O(n^2)：22.9 GB 的 item 在
        // 1 MB/请求下要重复走过累计约 260 TB 字节）。位置不符时 read_range()
        // 仍会按同一张表重新定位并用 bd_tell() 校准。
        if (seeked) {
            cursor.invalidate();
        }
        if (!read_range(disc, selected.size, item_start + offset, length,
                        &item_bounds, &cursor, &output)) {
            send_error("read failed");
            continue;
        }
        send_payload(output);
    }

    bd_close(disc);
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 3 && std::string(argv[1]) == "--probe") {
        return probe(argv[2]);
    }
    if (argc == 3 && std::string(argv[1]) == "--list") {
        return list_titles(argv[2]);
    }
    if (argc == 4 && std::string(argv[1]) == "--serve") {
        std::uint64_t title_index = 0;
        if (!parse_u64(argv[3], &title_index) ||
            title_index > std::numeric_limits<std::uint32_t>::max()) {
            usage();
            return 2;
        }
        return serve(argv[2], static_cast<std::uint32_t>(title_index));
    }
    if (argc == 4 && std::string(argv[1]) == "--streams") {
        std::uint64_t title_index = 0;
        if (!parse_u64(argv[3], &title_index) ||
            title_index > std::numeric_limits<std::uint32_t>::max()) {
            usage();
            return 2;
        }
        return streams(argv[2], static_cast<std::uint32_t>(title_index));
    }
    if (argc == 4 && std::string(argv[1]) == "--item-bounds") {
        std::uint64_t title_index = 0;
        if (!parse_u64(argv[3], &title_index) ||
            title_index > std::numeric_limits<std::uint32_t>::max()) {
            usage();
            return 2;
        }
        return print_item_bounds(argv[2], static_cast<std::uint32_t>(title_index));
    }
    usage();
    return 2;
}
