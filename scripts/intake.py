import argparse
from concurrent.futures import ThreadPoolExecutor
import base64
from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import NotRequired, TypedDict

from plan import ROOT


class Archive(TypedDict):
    url: str
    sha256: str
    kind: str
    strip: NotRequired[int]
    members: NotRequired[list[str]]
    executable: NotRequired[bool]
    tree_sha256: NotRequired[str]


class Dependencies(TypedDict):
    archives: dict[str, Archive]
    node_packages: list[str]


def tree_sha256(path: Path) -> str:
    # Gitiles can repack the same tree with different tar and gzip metadata.
    entries: dict[str, tuple[str, int, str]] = {}
    with tarfile.open(path) as archive:
        for member in archive:
            if member.isdir():
                continue
            name = member.name.removeprefix("./")
            if name in entries:
                raise ValueError(f"Duplicate path in {path.name}: {name}")
            if member.isfile():
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"Unreadable archive member: {name}")
                with source:
                    digest = sha256()
                    for chunk in iter(lambda: source.read(1024**2), b""):
                        digest.update(chunk)
                    content = digest.hexdigest()
            elif member.issym():
                content = member.linkname
            else:
                raise ValueError(f"Unexpected archive member: {name}")
            entries[name] = (member.type.decode("ascii"), member.mode & 0o777, content)
    return sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def fetch(
    cache: Path,
    url: str,
    digest: str,
    algorithm: str = "sha256",
    *,
    tree_digest: str | None = None,
) -> Path:
    if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]+", digest):
        raise ValueError(f"Invalid locked input for {url}")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / digest

    def verify(candidate: Path) -> None:
        with candidate.open("rb") as stream:
            if file_digest(stream, algorithm).hexdigest() == digest:
                return
        if tree_digest is None or tree_sha256(candidate) != tree_digest:
            raise ValueError(f"Digest mismatch for {url}")

    if path.exists():
        verify(path)
        return path
    with NamedTemporaryFile(dir=cache, delete=False) as scratch:
        temporary = Path(scratch.name)
    try:
        subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--retry",
                "3",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--output",
                temporary,
                "--",
                url,
            ],
            check=True,
        )
        verify(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def extract_tar(archive: Path, root: Path, strip: int = 0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "tar",
            "--extract",
            "--no-same-owner",
            "--file",
            archive,
            "--directory",
            root,
            f"--strip-components={strip}",
        ],
        check=True,
    )


def extract_zip(archive: Path, root: Path, members: list[str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["unzip", "-oq", archive, *(members or []), "-d", root], check=True)


def apply_patch(source: Path, patch: Path) -> None:
    subprocess.run(
        [
            "patch",
            "--directory",
            source,
            "--batch",
            "--forward",
            "--fuzz=0",
            "-p1",
            "--input",
            patch,
        ],
        check=True,
    )


def install(archive: Path, destination: Path, recipe: Archive) -> None:
    match recipe["kind"]:
        case "tar":
            extract_tar(archive, destination, recipe.get("strip", 0))
        case "zip":
            extract_zip(archive, destination, recipe.get("members"))
        case "file":
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, destination)
            if recipe.get("executable"):
                destination.chmod(0o755)
        case kind:
            raise ValueError(f"Unknown archive format {kind}")


def stage_archives(output: Path, cache: Path, recipes: dict[str, Archive]) -> None:
    with ThreadPoolExecutor(max_workers=6) as workers:
        downloads = {
            name: workers.submit(
                fetch,
                cache,
                spec["url"],
                spec["sha256"],
                tree_digest=spec.get("tree_sha256"),
            )
            for name, spec in recipes.items()
        }
        # Parent snapshots are installed first, followed by their nested dependencies.
        for name in sorted(recipes):
            print(f"Stage {name}", flush=True)
            install(downloads[name].result(), output / name, recipes[name])


def source_archive(lock: dict[str, str], strip: int = 0) -> Archive:
    archive = Archive(url=lock["url"], sha256=lock["sha256"], kind="tar", strip=strip)
    if digest := lock.get("tree_sha256"):
        archive["tree_sha256"] = digest
    return archive


def stage_node(source: Path, cache: Path, allowed: list[str]) -> None:
    node = source / "third_party/node"
    packages: dict[str, dict[str, str]] = json.loads(
        (node / "package-lock.json").read_text(encoding="utf-8")
    )["packages"]
    with ThreadPoolExecutor(max_workers=6) as workers:
        downloads = {}
        for path in allowed:
            spec = packages[path]
            if not spec["resolved"].startswith("https://registry.npmjs.org/"):
                raise ValueError(f"Unexpected package registry for {path}")
            algorithm, value = spec["integrity"].split("-", 1)
            downloads[path] = workers.submit(
                fetch,
                cache,
                spec["resolved"],
                base64.b64decode(value, validate=True).hex(),
                algorithm,
            )
        for path in sorted(allowed):
            extract_tar(downloads[path].result(), node / path, 1)
    for name in ("types_trusted_types.patch", "typescript.patch"):
        apply_patch(node, node / "patches" / name)


def install_sdk(output: Path, cache: Path, lock: dict[str, str]) -> None:
    version, tools_version = lock["platform"], lock["build_tools"]
    platform = f"feed/aosstudio-sdk-platform-{version}.zip"
    tools = f"feed/aosstudio-build-tools-{tools_version}-linux-x86_64.zip"
    bundle = fetch(cache, lock["url"], lock["sha256"])
    with TemporaryDirectory(dir=output.parent) as scratch:
        temp = Path(scratch)
        subprocess.run(
            [
                "tar",
                "--extract",
                "--file",
                bundle,
                "--directory",
                temp,
                platform,
                tools,
            ],
            check=True,
        )
        sdk = output / "sdk"
        extract_zip(temp / platform, sdk / "platforms")
        extract_zip(temp / tools, temp)
        (sdk / "build-tools").mkdir()
        (temp / "build-tools").rename(sdk / "build-tools" / tools_version)


def install_ndk(output: Path, cache: Path, lock: dict[str, str]) -> Path:
    archive = fetch(cache, lock["url"], lock["sha256"])
    extract_zip(archive, output)
    return output / f"ndk-{lock['version']}"


def bootstrap(output: Path, work: Path, cache: Path) -> None:
    lock: dict[str, dict[str, str]] = json.loads(
        (ROOT / "locks/inputs.json").read_text(encoding="utf-8")
    )
    work.mkdir(parents=True, exist_ok=True)
    recipes = {
        destination: source_archive(lock[name], strip)
        for name, destination, strip in (("gn", "gn", 0), ("llvm", "llvm-project", 1))
    }
    sources = work / "source"
    stage_archives(sources, cache, recipes)
    ndk = install_ndk(work, cache, lock["ndk"])
    subprocess.run(
        ["bash", ROOT / "scripts/bootstrap.sh", sources, work, output, ndk], check=True
    )


def stage_source_metadata(source: Path, revision: dict[str, str]) -> None:
    shutil.copyfile(
        ROOT / "config/gclient_args.gni", source / "build/config/gclient_args.gni"
    )
    shutil.copytree(
        ROOT / "config/pocketfft",
        source / "third_party/aosstudio_pocketfft",
        dirs_exist_ok=True,
    )
    typescript = source / "third_party/typescript/linux-amd64"
    apply_patch(
        typescript / "src", typescript / "3pp/patches/typescript_native_preview.patch"
    )
    (source / "build/util/LASTCHANGE").write_text(
        f"LASTCHANGE={revision['commit']}-{revision['position']}\nLASTCHANGE_YEAR={revision['year']}\n",
        encoding="utf-8",
    )
    (source / "build/util/LASTCHANGE.committime").write_text(
        revision["timestamp"], encoding="utf-8"
    )
    (source / "gpu/webgpu/DAWN_VERSION").write_text(
        revision["dawn_revision"], encoding="utf-8"
    )
    (source / "gpu/config/gpu_lists_version.h").write_text(
        "/* Generated by lastchange.py, do not edit.*/\n\n"
        "#ifndef GPU_CONFIG_GPU_LISTS_VERSION_H_\n#define GPU_CONFIG_GPU_LISTS_VERSION_H_\n\n"
        f'#define GPU_LISTS_VERSION "{revision["commit"]}"\n\n'
        "#endif  // GPU_CONFIG_GPU_LISTS_VERSION_H_\n",
        encoding="utf-8",
    )


def setup(output: Path, cache: Path, verify_only: bool = False) -> None:
    lock: dict[str, dict[str, str]] = json.loads(
        (ROOT / "locks/inputs.json").read_text(encoding="utf-8")
    )
    dependencies: Dependencies = json.loads(
        (ROOT / "locks/dependencies.json").read_text(encoding="utf-8")
    )
    recipes = {"jdk": source_archive(lock["jdk"], strip=1)}
    install_sdk(output, cache, lock["sdk"])
    if verify_only:
        stage_archives(output, cache, recipes)
        return
    recipes.update(dependencies["archives"])
    recipes["chromium"] = source_archive(lock["chromium"])
    recipes["chromium/third_party/android_system_sdk/cipd/android_system.jar"] = (
        Archive(
            url=lock["system_stubs"]["url"],
            sha256=lock["system_stubs"]["sha256"],
            kind="file",
        )
    )
    stage_archives(output, cache, recipes)
    source = output / "chromium"
    stage_node(source, cache, dependencies["node_packages"])
    launcher = output / "jdk/bin/java"
    launcher.rename(launcher.with_suffix(".chromium"))
    launcher.write_text(
        '#!/bin/sh\nexec "$0.chromium" -XX:+PerfDisableSharedMem "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    ndk = install_ndk(output, cache, lock["ndk"])
    ndk_link = source / "third_party/android_toolchain/ndk"
    ndk_link.symlink_to(ndk, target_is_directory=True)
    ndk_sysroot = ndk / "toolchains/llvm/prebuilt/linux-x86_64/sysroot"
    shutil.copyfile(
        fetch(
            cache, lock["ndk"]["media_header_url"], lock["ndk"]["media_header_sha256"]
        ),
        ndk_sysroot / "usr/include/media/NdkMediaCodecInfo.h",
    )
    (source / "third_party/jdk/current").symlink_to(
        output / "jdk", target_is_directory=True
    )
    python = source / "third_party/cpython3/host/bin/python3"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.symlink_to(sys.executable)
    gn = source / "buildtools/linux64/gn"
    gn.parent.mkdir(parents=True, exist_ok=True)
    gn.symlink_to(output / "gn/gn")
    clang = source / "third_party/llvm-build/Release+Asserts"
    clang.parent.mkdir(parents=True, exist_ok=True)
    clang.symlink_to(output / "clang", target_is_directory=True)
    stage_source_metadata(source, lock["chromium"])
    for name in (ROOT / "patches/series").read_text(encoding="utf-8").splitlines():
        apply_patch(source, ROOT / "patches" / name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare the locked Chromium WebView inputs."
    )
    parser.add_argument("--output", type=Path, default=ROOT / "out/inputs")
    parser.add_argument("--archives", type=Path, default=ROOT / "out/archives")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if subprocess.check_output(
        ["findmnt", "-n", "-o", "FSTYPE", "--target", output], text=True
    ).strip() in {"tmpfs", "ramfs"}:
        raise ValueError(f"Input directory must be disk backed: {output}")
    if any(output.iterdir()):
        raise ValueError(f"Use an empty input directory: {output}")
    cache = args.archives.resolve()
    bootstrap(output, output.parent / "bootstrap", cache)
    setup(output, cache)


if __name__ == "__main__":
    main()
