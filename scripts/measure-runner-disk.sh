#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

workspace=${GITHUB_WORKSPACE:-$PWD}
runner_temp=${RUNNER_TEMP:-${TMPDIR:-/tmp}}

printf '## Runner disk space\n\n'
printf 'Image %s (%s), %s\n\n' "${ImageOS:-unknown}" "${ImageVersion:-unknown}" "$(uname -m)"
printf '### Filesystem capacity in bytes\n\n```text\n'
df -B1 --output=source,fstype,size,used,avail,pcent,target -- "$workspace" "$runner_temp"
printf '```\n\n### Preinstalled software\n\n'
printf '| Category | Path | Allocated bytes | Size |\n| --- | --- | ---: | ---: |\n'

measure() {
    local label=$1 path=$2 usage bytes
    if [[ ! -d $path ]]; then
        printf '| %s | %s | absent | |\n' "$label" "$path"
        return
    fi
    if ! usage=$(du --block-size=1 --one-file-system --summarize --dereference-args -- "$path"); then
        printf '| %s | %s | incomplete | |\n' "$label" "$path"
        return
    fi
    bytes=${usage%%$'\t'*}
    printf '| %s | %s | %s | %s |\n' "$label" "$path" "$bytes" \
        "$(numfmt --to=iec-i --suffix=B --format='%.2f' "$bytes")"
}

measure 'Android SDK' "${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/usr/local/lib/android/sdk}}"
measure 'Hosted tool cache' "${RUNNER_TOOL_CACHE:-/opt/hostedtoolcache}"
measure 'Swift' /usr/share/swift
measure '.NET' /usr/share/dotnet
measure 'GHC' /opt/ghc
measure 'Chromium' /usr/local/share/chromium
measure 'PowerShell' /usr/local/share/powershell
measure 'Java runtimes' /usr/lib/jvm
measure 'Azure CLI' /opt/az
measure 'Google Cloud CLI' /usr/lib/google-cloud-sdk

printf '\nDirectory sizes report allocated filesystem space. Measurements may overlap when a path points into another measured directory.\n'
