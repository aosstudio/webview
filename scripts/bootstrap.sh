#!/usr/bin/env bash
set -euo pipefail

sources=$1
work=$2
output=$3
ndk=$4
jobs=${JOBS:-4}
python=${PYTHON:-python3}
llvm="$sources/llvm-project"
clang="$output/clang"
ndk_tools="$ndk/toolchains/llvm/prebuilt/linux-x86_64"

mkdir -p "$output/gn"
CC=clang-21 CXX=clang++-21 "$python" "$sources/gn/build/gen.py" --out-path="$work/gn"
ninja -C "$work/gn" -j "$jobs" gn
install -m 0755 "$work/gn/gn" "$output/gn/gn"
install -m 0644 "$sources/gn/LICENSE" "$output/gn/LICENSE"

cmake -S "$llvm/llvm" -B "$work/llvm" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=clang-21 -DCMAKE_CXX_COMPILER=clang++-21 \
    -DCMAKE_INSTALL_PREFIX="$clang" -DCLANG_VENDOR=AOSStudio \
    -DLLVM_ENABLE_PROJECTS='clang;lld' -DLLVM_TARGETS_TO_BUILD=X86 \
    -DLLVM_PARALLEL_LINK_JOBS=1 -DLLVM_INSTALL_TOOLCHAIN_ONLY=ON \
    -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF \
    -DLLVM_INCLUDE_EXAMPLES=OFF -DLLVM_INCLUDE_DOCS=OFF \
    -DLLVM_ENABLE_ZLIB=FORCE_ON -DLLVM_ENABLE_ZSTD=FORCE_ON \
    -DLLVM_ENABLE_LIBXML2=OFF -DLLVM_ENABLE_CURL=OFF

tools=(clang clang-resource-headers lld llvm-ar llvm-nm llvm-objcopy llvm-objdump llvm-profdata llvm-readobj llvm-symbolizer)
cmake --build "$work/llvm" --parallel "$jobs" --target "${tools[@]}"
for component in "${tools[@]}" llvm-ranlib llvm-strip; do
    cmake --install "$work/llvm" --strip --component "$component"
done
ln -s llvm-readobj "$clang/bin/llvm-readelf"
install -m 0644 "$llvm/llvm/LICENSE.TXT" "$clang/LICENSE.TXT"

resource=$("$clang/bin/clang" --print-resource-dir)
common=(-G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER="$clang/bin/clang"
    -DCMAKE_CXX_COMPILER="$clang/bin/clang++" -DCMAKE_ASM_COMPILER="$clang/bin/clang"
    -DLLVM_MAIN_SRC_DIR="$llvm/llvm" -DLLVM_CMAKE_DIR="$work/llvm/lib/cmake/llvm"
    -DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON)
host_triple=x86_64-unknown-linux-gnu
android_triple=x86_64-linux-android29
cmake -S "$llvm/compiler-rt/lib/builtins" -B "$work/builtins-host" "${common[@]}" \
    -DCMAKE_C_COMPILER_TARGET="$host_triple" -DCMAKE_ASM_COMPILER_TARGET="$host_triple" \
    -DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=ON
# The compiler triple selects API 29. CMake 1 mode leaves cross-compiler selection to the recipe.
cmake -S "$llvm/compiler-rt/lib/builtins" -B "$work/builtins-android" "${common[@]}" \
    -DCMAKE_C_COMPILER_TARGET="$android_triple" -DCMAKE_CXX_COMPILER_TARGET="$android_triple" \
    -DCMAKE_ASM_COMPILER_TARGET="$android_triple" -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DCMAKE_SYSTEM_NAME=Android -DCMAKE_SYSTEM_VERSION=1 -DCMAKE_SYSTEM_PROCESSOR=x86_64 \
    -DCMAKE_SYSROOT="$ndk_tools/sysroot" -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
    -DCOMPILER_RT_OS_DIR=linux -DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF
for target in host android; do
    cmake --build "$work/builtins-$target" --parallel "$jobs"
done
install -D -m 0644 "$work/builtins-host/lib/$host_triple/libclang_rt.builtins.a" \
    "$resource/lib/$host_triple/libclang_rt.builtins.a"
install -D -m 0644 "$work/builtins-android/lib/linux/libclang_rt.builtins-x86_64-android.a" \
    "$resource/lib/linux/libclang_rt.builtins-x86_64-android.a"
ndk_resource=$("$ndk_tools/bin/clang" --print-resource-dir)
install -D -m 0644 "$ndk_resource/lib/linux/x86_64/libatomic.a" "$resource/lib/linux/x86_64/libatomic.a"

"$output/gn/gn" --version
"$clang/bin/clang" --version
