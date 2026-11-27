import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import TypedDict
from zipfile import ZIP_STORED, ZipFile


ROOT = Path(__file__).resolve().parents[1]


class APK(TypedDict):
    filename: str
    package: str
    abi: str
    target_sdk: int


def check_sources() -> None:
    patch_root = ROOT / "patches"
    series = (patch_root / "series").read_text(encoding="utf-8").splitlines()
    if sorted(series) != sorted(path.name for path in patch_root.glob("*.patch")):
        raise ValueError("The patch series must list every patch exactly once")
    for filename in series:
        subprocess.run(
            ["git", "apply", "--numstat", patch_root / filename],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            check=True,
        )

    lock: dict[str, dict[str, str | int]] = json.loads(
        (ROOT / "locks/inputs.json").read_text(encoding="utf-8")
    )
    for name, values in lock.items():
        for key, value in values.items():
            if key.endswith("sha256") and not re.fullmatch(r"[0-9a-f]{64}", str(value)):
                raise ValueError(f"Invalid SHA-256 for {name}.{key}")
            if key.endswith("url") and not str(value).startswith("https://"):
                raise ValueError(f"Expected an HTTPS URL for {name}")


def verify_apk(apk: Path, sdk: Path) -> None:
    lock = json.loads((ROOT / "locks/inputs.json").read_text(encoding="utf-8"))
    expected: APK = lock["apk"]
    tools = sdk / "build-tools" / lock["sdk"]["build_tools"]
    with ZipFile(apk) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"Invalid ZIP CRC in {apk}")
        abis = {
            entry.split("/")[1]
            for entry in archive.namelist()
            if entry.startswith("lib/") and entry.endswith(".so")
        }
        if abis != {expected["abi"]}:
            raise ValueError(f"Unexpected native ABIs: {sorted(abis)}")
        for name in ("libwebviewchromium.so", "libcrashpad_handler_trampoline.so"):
            if (
                archive.getinfo(f"lib/{expected['abi']}/{name}").compress_type
                != ZIP_STORED
            ):
                raise ValueError(f"Expected an uncompressed native library: {name}")

    badging = subprocess.check_output(
        [tools / "aapt2", "dump", "badging", apk], text=True
    )
    manifest_fields = (
        f"package: name='{expected['package']}'",
        f"versionName='{lock['chromium']['version']}'",
        f"targetSdkVersion:'{expected['target_sdk']}'",
    )
    for field in manifest_fields:
        if field not in badging:
            raise ValueError(f"APK metadata does not contain {field}")
    subprocess.run([tools / "zipalign", "-c", "-P", "16", "4", apk], check=True)
    signing_env = os.environ.copy()
    if java_home := signing_env.get("JAVA_HOME"):
        signing_env["PATH"] = os.pathsep.join(
            (str(Path(java_home) / "bin"), signing_env.get("PATH", os.defpath))
        )
    signing = subprocess.check_output(
        [tools / "apksigner", "verify", "--verbose", apk], text=True, env=signing_env
    )
    if "Verified using v2 scheme (APK Signature Scheme v2): true" not in signing:
        raise ValueError("Expected an APK Signature Scheme v2 signature")
    print(
        f"Verified {expected['package']} {lock['chromium']['version']} ({expected['abi']})."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check the build inputs or an existing WebView APK."
    )
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--sdk", type=Path)
    args = parser.parse_args()
    if args.apk is None:
        check_sources()
    elif args.sdk is None:
        parser.error("--apk requires --sdk")
    else:
        verify_apk(args.apk, args.sdk)


if __name__ == "__main__":
    main()
