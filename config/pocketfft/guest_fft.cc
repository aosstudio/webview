// SPDX-License-Identifier: Apache-2.0

#include "guest_fft.h"

#include <algorithm>
#include <complex>
#include <cstddef>
#include <vector>

#define POCKETFFT_NO_MULTITHREADING
#include "pocketfft_hdronly.h"

namespace {

using Bin = std::complex<double>;

pocketfft::stride_t RowStrides(std::size_t columns,
                               std::ptrdiff_t element_size) {
  return {static_cast<std::ptrdiff_t>(columns) * element_size, element_size};
}

std::vector<double> CopyRows(double** rows,
                             std::size_t height,
                             std::size_t width) {
  std::vector<double> values(height * width);
  for (std::size_t row = 0; row < height; ++row) {
    std::copy_n(rows[row], width, values.data() + row * width);
  }
  return values;
}

void WriteRows(const std::vector<double>& values,
               std::size_t height,
               std::size_t width,
               double** rows) {
  for (std::size_t row = 0; row < height; ++row) {
    std::copy_n(values.data() + row * width, width, rows[row]);
  }
}

// The fused 2D inverse in TensorFlow Lite uses DC from the upper half and
// Nyquist from the lower half when the supplied spectrum lacks symmetry.
void NormalizeBoundaryBins(std::vector<Bin>& bins,
                           std::size_t height,
                           std::size_t columns) {
  const auto normalize = [&](std::size_t column, bool use_upper_half) {
    for (std::size_t row = 1; row <= (height - 1) / 2; ++row) {
      Bin& upper = bins[row * columns + column];
      Bin& lower = bins[(height - row) * columns + column];
      if (use_upper_half) {
        lower = std::conj(upper);
      } else {
        upper = std::conj(lower);
      }
    }
    bins[column].imag(0);
    bins[(height / 2) * columns + column].imag(0);
  };
  normalize(0, true);
  if (columns > 1) {
    normalize(columns - 1, false);
  }
}

}  // namespace

extern "C" void guest_rfft2d(int height, int width, double** rows) {
  const auto row_count = static_cast<std::size_t>(height);
  const auto column_count = static_cast<std::size_t>(width);
  const auto bins_per_row = column_count / 2 + 1;
  const auto samples = CopyRows(rows, row_count, column_count);
  std::vector<Bin> bins(row_count * bins_per_row);
  pocketfft::r2c({row_count, column_count},
                 RowStrides(column_count, sizeof(double)),
                 RowStrides(bins_per_row, sizeof(Bin)), {0, 1}, true,
                 samples.data(), bins.data(), 1.0);
  for (std::size_t row = 0; row < row_count; ++row) {
    for (std::size_t column = 0; column < bins_per_row; ++column) {
      const auto bin = bins[row * bins_per_row + column];
      rows[row][2 * column] = bin.real();
      rows[row][2 * column + 1] = bin.imag();
    }
  }
}

extern "C" void guest_irfft2d(int height, int width, double** rows) {
  const auto row_count = static_cast<std::size_t>(height);
  const auto column_count = static_cast<std::size_t>(width);
  const auto bins_per_row = column_count / 2 + 1;
  std::vector<Bin> bins(row_count * bins_per_row);
  for (std::size_t row = 0; row < row_count; ++row) {
    for (std::size_t column = 0; column < bins_per_row; ++column) {
      bins[row * bins_per_row + column] = {rows[row][2 * column],
                                           rows[row][2 * column + 1]};
    }
  }
  NormalizeBoundaryBins(bins, row_count, bins_per_row);
  std::vector<double> samples(row_count * column_count);
  pocketfft::c2r({row_count, column_count},
                 RowStrides(bins_per_row, sizeof(Bin)),
                 RowStrides(column_count, sizeof(double)), {0, 1}, false,
                 bins.data(), samples.data(), 0.5);
  WriteRows(samples, row_count, column_count, rows);
}

extern "C" void rdft(int size, int sign, double* samples, int*, double*) {
  if (size == 1) {
    if (sign < 0) {
      samples[0] *= 0.5;
    }
    return;
  }
  const auto length = static_cast<std::size_t>(size);
  std::vector<Bin> bins(length / 2 + 1);
  if (sign >= 0) {
    pocketfft::r2c({length}, {sizeof(double)}, {sizeof(Bin)}, 0, false, samples,
                   bins.data(), 1.0);
    samples[0] = bins.front().real();
    samples[1] = bins.back().real();
    for (std::size_t k = 1; k < length / 2; ++k) {
      samples[2 * k] = bins[k].real();
      samples[2 * k + 1] = bins[k].imag();
    }
  } else {
    bins.front() = samples[0];
    bins.back() = samples[1];
    for (std::size_t k = 1; k < length / 2; ++k) {
      bins[k] = {samples[2 * k], -samples[2 * k + 1]};
    }
    pocketfft::c2r({length}, {sizeof(Bin)}, {sizeof(double)}, 0, false,
                   bins.data(), samples, 0.5);
  }
}

extern "C" void ddct(int size, int sign, double* samples, int*, double*) {
  if (sign >= 0) {
    samples[0] *= 2;
  }
  pocketfft::dct({static_cast<std::size_t>(size)}, {sizeof(double)},
                 {sizeof(double)}, {0}, sign < 0 ? 2 : 3, samples, samples, 0.5,
                 false);
}

extern "C" void
ddct2d(int height, int width, int sign, double** rows, double*, int*, double*) {
  const auto row_count = static_cast<std::size_t>(height);
  const auto column_count = static_cast<std::size_t>(width);
  auto samples = CopyRows(rows, row_count, column_count);
  if (sign >= 0) {
    for (std::size_t row = 0; row < row_count; ++row) {
      samples[row * column_count] *= 2;
    }
    for (std::size_t column = 0; column < column_count; ++column) {
      samples[column] *= 2;
    }
  }
  const auto strides = RowStrides(column_count, sizeof(double));
  pocketfft::dct({row_count, column_count}, strides, strides, {0, 1},
                 sign < 0 ? 2 : 3, samples.data(), samples.data(), 0.25, false);
  WriteRows(samples, row_count, column_count, rows);
}
