#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
src=$(realpath -e -- "${CHROMIUM_SRC:?Set CHROMIUM_SRC to the prepared Chromium tree}")
sdk=$(realpath -e -- "${ANDROID_HOME:?Set ANDROID_HOME to the installed AOSStudio SDK}")
clang=$(realpath -e -- "${CLANG_ROOT:?Set CLANG_ROOT to the prepared Chromium Clang tree}")
gn=$(realpath -e -- "$(command -v "${GN:-gn}")")
python=$(realpath -e -- "$(command -v "${PYTHON:-python3}")")
output=$(realpath -m -- "${WEBVIEW_OUT:-$repo/out}")
build="$output/build"
lock="$repo/locks/inputs.json"
target=//android_webview:system_webview_apk
ndk_sysroot="$src/third_party/android_toolchain/ndk/toolchains/llvm/prebuilt/linux-x86_64/sysroot"
mode=${1:-all}
if (( $# )); then shift; fi

mkdir -p -- "$build" "$output/tmp" "$output/home"
for path in "$src" "$output"; do
    case $(findmnt -n -o FSTYPE --target "$path") in
        tmpfs | ramfs) printf 'Use disk-backed storage for %s.\n' "$path" >&2; exit 1 ;;
    esac
done

for path in "$sdk" "$clang"; do
    case $path in
        *[!a-zA-Z0-9/._+-]*) printf 'Unsupported path for GN: %s\n' "$path" >&2; exit 1 ;;
    esac
done

platform=$(jq -er .sdk.platform "$lock")
tools=$(jq -er .sdk.build_tools "$lock")
printf '%s  %s\n' "$(jq -er .chromium.deps_sha256 "$lock")" "$src/DEPS" | sha256sum --check --status
printf '%s  %s\n' "$(jq -er .system_stubs.sha256 "$lock")" \
    "$src/third_party/android_system_sdk/cipd/android_system.jar" | sha256sum --check --status
printf '%s  %s\n' "$(jq -er .ndk.media_header_sha256 "$lock")" \
    "$ndk_sysroot/usr/include/media/NdkMediaCodecInfo.h" | sha256sum --check --status
test -f "$sdk/platforms/android-$platform/android.jar"
test -x "$sdk/build-tools/$tools/aapt2"
test -x "$clang/bin/clang"
export JAVA_HOME="$src/third_party/jdk/current"
test -x "$JAVA_HOME/bin/java"
export PATH="${gn%/*}:$JAVA_HOME/bin:$PATH"

# The JDK uses /tmp for in-place APK signing even when TMPDIR is set.
sandbox=(bwrap --unshare-net --die-with-parent --ro-bind / / --dev /dev --proc /proc
    --bind "$output" "$output" --bind "$output/tmp" /tmp --ro-bind "$src" "$src" --ro-bind "$sdk" "$sdk"
    --ro-bind "$clang" "$clang" --chdir "$src" --setenv HOME "$output/home"
    --setenv TMPDIR "$output/tmp" --setenv PYTHONDONTWRITEBYTECODE 1)

configure() {
    cat "$repo/config/webview.gn" >"$build/args.gn"
    {
        printf 'clang_base_path = "%s"\n' "$clang"
        printf 'android_sdk_root = "%s"\npublic_android_sdk_root = "%s"\n' "$sdk" "$sdk"
        printf 'public_android_sdk_platform_version = "%s"\n' "$platform"
        printf 'android_sdk_build_tools_version = "%s"\npublic_android_sdk_build_tools_version = "%s"\n' "$tools" "$tools"
        printf 'android_sdk_tools_bundle_aapt2_dir = "%s/build-tools/%s"\n' "$sdk" "$tools"
    } >>"$build/args.gn"
    "${sandbox[@]}" "$gn" gen "$build" --root-target="$target" --threads="${JOBS:-8}" --script-executable="$python"
}

build_targets() {
    "${sandbox[@]}" ninja -C "$build" -j "${JOBS:-8}" "$@"
}

notices() {
    "${sandbox[@]}" bash "$repo/scripts/package.sh" "$build" "$python" "$lock"
}

case "$mode" in
    all)
        configure
        build_targets system_webview_apk
        notices
        "$python" "$repo/scripts/verify.py" --apk "$build/apks/SystemWebView64.apk" --sdk "$sdk"
        ;;
    configure) configure ;;
    ninja) build_targets "$@" ;;
    notices) notices ;;
    *) printf 'Unknown build mode: %s\n' "$mode" >&2; exit 2 ;;
esac
