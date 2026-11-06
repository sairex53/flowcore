#pragma once
#include <array>
#include <cstdint>
#include <expected>
#include <optional>
#include <span>

namespace flowcore::telemetry {
struct Bin { std::uint16_t index; std::uint64_t count; };
enum class Error { invalid_bin, duplicate_bin, invalid_total, overflow };
using Summary = std::array<std::optional<std::uint64_t>, 3>;
// Input is borrowed for the duration of the call. No allocation or shared state.
[[nodiscard]] std::expected<Summary, Error>
summarize(std::span<const Bin> bins, std::uint64_t total) noexcept;
}
