#!/usr/bin/env bash
set -euo pipefail

build=$1
python=$2
lock=$3
target=//android_webview:system_webview_64_apk

"$python" tools/licenses/licenses.py license_file \
    --format notice --target-os android --gn-out-dir "$build" --gn-target "$target" "$build/apks/NOTICE"
"$python" tools/licenses/licenses.py license_file \
    --format spdx --target-os android --gn-out-dir "$build" --gn-target "$target" "$build/apks/SystemWebView64.spdx.json"
cp -- "$lock" "$build/apks/build-inputs.json"
