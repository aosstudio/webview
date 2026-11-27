# Build WebView for Android x86_64

The initial build target is Chromium `156.0.8078.93`, using the Android 17 / API 37.0 platform and SDK Build Tools 36.0.0. Use Linux x86_64, disk-backed storage, and at least 100 GiB of free space for initial qualification. Install GNU Make, Ninja, Bubblewrap, jq and Python 3.14. [Chromium Android build requirements](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/android_build_instructions.md)

## Prepare inputs

Use [the input lock](../locks/inputs.json) to verify the Chromium source snapshot, GN and LLVM inputs, and the Temurin, SDK, System stubs and NDK archives. Install the AOSStudio SDK packages in a dedicated SDK directory. Use the separate System API stubs JAR from the SDK Platform Release.

The build entry takes a prepared Chromium tree. Populate the reviewed Git, Maven, Node and Rust inputs at the paths selected by the pinned `DEPS` and lockfiles, including generated version metadata. Stage the approved NDK r28c sysroot with the API 37 media codec header and the compiler-rt compatibility input used in the pilot. In that tree, the JDK belongs at `third_party/jdk/current`, the NDK at `third_party/android_toolchain/ndk`, and the released System stubs at `third_party/android_system_sdk/cipd/android_system.jar`. Use the reviewed source-built Clang and the matching compiler-rt builtins. Prepare dependencies without running unfiltered upstream download hooks. The cold intake and complete staging recipe are pending.

Apply the patches once, in [series order](../patches/series), in the prepared Chromium source tree after its nested source dependencies have been populated. The patch set preserves Android platform location, face detection and the available Credential Manager WebAuthn path while removing the Play Services dependencies reached by this WebView target. The PocketFFT patch replaces the earlier FFT implementation. The build script applies [the WebView GN configuration](../config/webview.gn).

```sh
export CHROMIUM_SRC=/path/to/prepared/chromium
(
    set -e
    while IFS= read -r name; do
        patch --directory "$CHROMIUM_SRC" --batch --forward --fuzz=0 -p1 \
            --input "$PWD/patches/$name"
    done < patches/series
)
```

## Compile and verify

Set `GN` to the pinned GN executable if it is outside `PATH`. Use the same prepared sources and tools for a local build and any future hosted qualification.

```sh
export ANDROID_HOME=/path/to/aosstudio/sdk
export CLANG_ROOT=/path/to/prepared/clang
export GN=/path/to/prepared/gn
make build JOBS=8
```

The build runs GN and Ninja in an isolated network namespace and writes `out/build/apks/SystemWebView64.apk`, the Chromium NOTICE, an SPDX inventory and the recorded input lock. Use `make verify APK=/path/to/SystemWebView64.apk` to inspect a candidate with the installed AOSStudio SDK and `JAVA_HOME` set to an audited JDK. It checks ZIP integrity, Android package metadata, the native ABI, 16 KiB alignment and an APK Signature Scheme v2 signature.

The AVD build imports `com.android.webview` as its system provider and handles product signing. Before public distribution, verify the image's provider and a WebView rendering test, and qualify access to the corresponding FFmpeg source and the material needed to relink WebView with a modified FFmpeg build. [GNU LGPL 2.1](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html)
