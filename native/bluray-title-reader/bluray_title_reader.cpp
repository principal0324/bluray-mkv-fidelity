// Persistent libbluray main-title reader for HDAtHome secure packaging.
//
// The helper deliberately exposes small interfaces:
//   --probe SOURCE
//   --list SOURCE
//   --serve SOURCE TITLE_INDEX
//
// It does not remux or transcode. libbluray applies the MPLS play-item order
// and boundaries and returns the original 192-byte transport packets.

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
#include <vector>

namespace {

constexpr std::uint64_t kMaxRead = 64ULL * 1024ULL * 1024ULL;
constexpr std::size_t kBufferSize = 1024ULL * 1024ULL;

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

bool read_exact(BLURAY* disc, std::uint64_t length,
                std::vector<unsigned char>* output) {
    output->clear();
    output->reserve(static_cast<std::size_t>(length));
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

bool read_range(BLURAY* disc, std::uint64_t title_size,
                std::uint64_t offset, std::uint64_t length,
                std::uint64_t* current_position,
                std::vector<unsigned char>* output) {
    if (offset > title_size || length > title_size - offset ||
        length > kMaxRead) {
        return false;
    }
    if (length == 0) {
        output->clear();
        *current_position = offset;
        return true;
    }
    if (*current_position != offset) {
        const std::int64_t actual = bd_seek(disc, offset);
        if (actual < 0 || static_cast<std::uint64_t>(actual) > offset) {
            return false;
        }
        const std::uint64_t prefix = offset - static_cast<std::uint64_t>(actual);
        if (!discard_exact(disc, prefix)) {
            return false;
        }
        *current_position = offset;
    }
    if (!read_exact(disc, length, output)) {
        return false;
    }
    *current_position += length;
    return true;
}

void send_error(const std::string& message) {
    std::string safe = message;
    std::replace(safe.begin(), safe.end(), '\t', ' ');
    std::replace(safe.begin(), safe.end(), '\n', ' ');
    std::cout << "ERR\t" << safe << '\n' << std::flush;
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

    std::uint64_t current_position = 0;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "QUIT") {
            break;
        }
        std::istringstream request(line);
        std::string command;
        std::string raw_offset;
        std::string raw_length;
        if (!std::getline(request, command, '\t') ||
            !std::getline(request, raw_offset, '\t') ||
            !std::getline(request, raw_length) || command != "READ") {
            send_error("invalid request");
            continue;
        }
        std::uint64_t offset = 0;
        std::uint64_t length = 0;
        if (!parse_u64(raw_offset.c_str(), &offset) ||
            !parse_u64(raw_length.c_str(), &length)) {
            send_error("invalid range");
            continue;
        }

        std::vector<unsigned char> output;
        if (!read_range(disc, selected.size, offset, length,
                        &current_position, &output)) {
            send_error("read failed");
            continue;
        }
        std::cout << "OK\t" << output.size() << '\n';
        if (!output.empty()) {
            std::cout.write(reinterpret_cast<const char*>(output.data()),
                            static_cast<std::streamsize>(output.size()));
        }
        std::cout.flush();
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
    usage();
    return 2;
}
