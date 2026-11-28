# Build WebView for Android x86_64

The initial build target is Chromium `156.0.8078.93`, using the Android 17 / API 37.0 platform and SDK Build Tools 36.0.0. Use Linux x86_64 and disk-backed storage. The preparation script uses Clang 21, CMake, Ninja, curl, unzip and Python 3.14. Configure and compile with Bubblewrap and jq. [Chromium Android build requirements](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/android_build_instructions.md)

## Prepare inputs

Run the input preparation script with an empty output directory. It builds GN, Clang and the required compiler-rt libraries, installs the AOSStudio SDK platform and Build Tools from the locked SDK Release, and stages the Chromium dependency tree. It verifies each download against the repository locks and Chromium's pinned npm integrity values. Gitiles archives also have a source-tree digest so changes in compression metadata preserve the same pin. The script then writes the version metadata and applies the WebView patch series. [Input lock](../locks/inputs.json) · [Dependency lock](../locks/dependencies.json)

```sh
python3 scripts/intake.py
```

## Compile and verify

Use the prepared input paths for a local build.

```sh
export CHROMIUM_SRC="$PWD/out/inputs/chromium"
export ANDROID_HOME="$PWD/out/inputs/sdk"
export JAVA_HOME="$PWD/out/inputs/jdk"
export CLANG_ROOT="$PWD/out/inputs/clang"
export GN="$PWD/out/inputs/gn/gn"
make build JOBS=8
```

The build runs GN and Ninja in an isolated network namespace and writes `out/build/apks/SystemWebView64.apk`, the Chromium NOTICE, an SPDX inventory and the recorded input lock. Use `make verify APK=/path/to/SystemWebView64.apk` to inspect a candidate with the installed AOSStudio SDK and `JAVA_HOME` set to an audited JDK. It checks ZIP integrity, Android package metadata, the native ABI, 16 KiB alignment and an APK Signature Scheme v2 signature.

The AVD build imports `com.android.webview` as its system provider and handles product signing. Before public distribution, verify the image's provider and a WebView rendering test, and qualify access to the corresponding FFmpeg source and the material needed to relink WebView with a modified FFmpeg build. [GNU LGPL 2.1](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html)
