// SPDX-License-Identifier: Apache-2.0

#ifndef AOSSTUDIO_GUEST_FFT_H_
#define AOSSTUDIO_GUEST_FFT_H_

#ifdef __cplusplus
extern "C" {
#endif

// These transforms consume or produce rows of interleaved real/imaginary
// pairs, in negative-exponent order. The inverse uses a factor of 1/2 so
// TensorFlow Lite can keep its existing output normalization.
void guest_rfft2d(int height, int width, double** rows);
void guest_irfft2d(int height, int width, double** rows);

void rdft(int size, int sign, double* samples, int* workspace, double* table);
void ddct(int size, int sign, double* samples, int* workspace, double* table);
void ddct2d(int height,
            int width,
            int sign,
            double** rows,
            double* scratch,
            int* workspace,
            double* table);

#ifdef __cplusplus
}
#endif

#endif  // AOSSTUDIO_GUEST_FFT_H_
