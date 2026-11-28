import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from typing import TypedDict

from intake import bootstrap, extract_tar, fetch, setup
from plan import ROOT, SMOKE, Stage
from release_cache import Asset, Receipt, ReleaseCache


OUT = ROOT / "out"
INPUTS = OUT / "inputs"
BUILD = OUT / "build"
STATE = OUT / "hosted"
ARCHIVES = OUT / "archives"
APK_FILES = [
    "apks/SystemWebView64.apk",
    "apks/NOTICE",
    "apks/SystemWebView64.spdx.json",
    "apks/build-inputs.json",
]


class Job(TypedDict):
    name: str
    release: int
    hit: Receipt | None
    upstream: dict[str, Receipt]


class Matrices(TypedDict):
    prepare: list[str]
    native: list[str]


def job_output(name: str, value: str) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        print(f"{name}={value}", file=output)


def emit_matrices() -> None:
    stages = load_plan()
    matrices = Matrices(
        prepare=sorted(name for name in stages if re.fullmatch(r"prepare-\d+", name)),
        native=sorted(name for name in stages if re.fullmatch(r"native-\d+", name)),
    )
    if not all(matrices.values()):
        raise ValueError("The Ninja plan must include preparation and native shards")
    job_output("matrices", json.dumps(matrices, separators=(",", ":")))


def lookup(cache: ReleaseCache, stage: str) -> None:
    upstream: dict[str, Receipt] = {}
    name = "plan.json" if stage == "plan" else f"{stage}.tar.zst"
    if stage == "toolchain":
        release = cache.ensure()
        hit = cache.lookup(release, name)
        job_output("release", str(release))
    else:
        release = int(os.environ["CACHE_RELEASE"])
        assets: dict[str, Asset] = cache.assets(release)
        hit = cache.lookup(release, name, assets=assets)
        if hit is None:
            toolchain = cache.lookup(release, "toolchain.tar.zst", assets=assets)
            if toolchain is None:
                raise ValueError(
                    "Missing build toolchain in the branch's draft Release"
                )
            if stage != "plan":
                plan = cache.lookup(release, "plan.json", assets=assets)
                if plan is None:
                    raise ValueError("The plan was removed during the build")
                cache.fetch(plan, STATE / "plan.json")
                upstream = lookup_prerequisites(cache, release, assets, stage)
            cache.fetch(toolchain, STATE / "toolchain.tar.zst")
        elif stage == "plan":
            cache.fetch(hit, STATE / "plan.json")

    job = Job(name=name, release=release, hit=hit, upstream=upstream)
    (STATE / "job.json").write_text(json.dumps(job), encoding="utf-8")
    job_output("hit", "true" if hit else "false")
    print(f"{'Reuse' if hit else 'Build'} {name} in draft {release}")


def lookup_prerequisites(
    cache: ReleaseCache, release: int, assets: dict[str, Asset], stage: str
) -> dict[str, Receipt]:
    matrices: Matrices = json.loads(os.environ["BUILD_MATRICES"])
    parents: dict[str, list[str]] = {
        "smoke": [],
        "prepare-base": [],
        **{name: ["prepare-base"] for name in matrices["prepare"]},
        "prepare-finish": matrices["prepare"],
        **{name: ["prepare-finish"] for name in matrices["native"]},
        "java": ["prepare-finish"],
        "link": matrices["native"],
        "apk": ["link", "java"],
    }

    upstream: dict[str, Receipt] = {}

    def dependencies(job: str) -> None:
        for parent in parents[job]:
            if parent not in upstream:
                dependencies(parent)
                receipt = cache.lookup(release, f"{parent}.tar.zst", assets=assets)
                if receipt is None:
                    raise ValueError(f"Missing upstream handoff for {parent}")
                upstream[parent] = receipt

    dependencies(stage)
    return upstream


def load_job() -> Job:
    job: Job = json.loads((STATE / "job.json").read_text(encoding="utf-8"))
    return job


def registry_digest(reference: str, auth: list[str], token: str) -> str | None:
    result = subprocess.run(
        ["oras", "manifest", "fetch", "--descriptor", *auth, reference],
        input=token,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        failure = result.stderr.strip()
        if failure.endswith(": not found"):
            return None
        raise RuntimeError(f"Could not look up {reference}: {failure}")
    digest: str = json.loads(result.stdout)["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"Invalid manifest digest for {reference}: {digest}")
    return digest


def push_cache(
    reference: str,
    archive: Path,
    artifact_type: str,
    layer_type: str,
    auth: list[str],
    token: str,
) -> None:
    if registry_digest(reference, auth, token) is not None:
        print(f"Reuse {reference} published by another job", flush=True)
        return
    print(f"Publish {reference}", flush=True)
    subprocess.run(
        [
            "oras",
            "push",
            *auth,
            "--artifact-type",
            artifact_type,
            "--annotation",
            f"org.opencontainers.image.source=https://github.com/{os.environ['GITHUB_REPOSITORY']}",
            reference,
            f"{archive.name}:{layer_type}",
        ],
        input=token,
        text=True,
        cwd=archive.parent,
        check=True,
    )


def pull_cache(reference: str, output: Path, auth: list[str], token: str) -> None:
    print(f"Reuse {reference}", flush=True)
    subprocess.run(
        ["oras", "pull", *auth, "--output", output, reference],
        input=token,
        text=True,
        check=True,
    )


def cache_chromium(
    stage: str,
    package: str,
    source: dict[str, str],
    auth: list[str],
    token: str,
    *,
    needed: bool,
) -> None:
    revision = source["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"Invalid Chromium commit {revision}")
    reference = f"{package}:{revision}"
    digest = registry_digest(reference, auth, token)
    if digest is None and stage != "plan":
        raise ValueError(f"Missing {reference}; the plan job fills the source cache")

    filename = "chromium.tar.gz"
    if digest is None:
        print(f"Fetch Chromium {revision} from Gitiles", flush=True)
        archive = fetch(
            ARCHIVES,
            source["url"],
            source["sha256"],
            tree_digest=source["tree_sha256"],
        )
        with TemporaryDirectory(dir=ARCHIVES) as directory:
            artifact = Path(directory) / filename
            artifact.hardlink_to(archive)
            push_cache(
                reference,
                artifact,
                "application/vnd.aosstudio.chromium.source.v1",
                "application/gzip",
                auth,
                token,
            )
    elif needed:
        with TemporaryDirectory(dir=ARCHIVES) as directory:
            pull_cache(f"{package}@{digest}", Path(directory), auth, token)
            (Path(directory) / filename).replace(ARCHIVES / source["sha256"])
    else:
        print(f"Reuse {reference}@{digest}", flush=True)


def restore_input_archives(archive: Path, chromium_digest: str) -> None:
    with tarfile.open(archive, "r:") as bundle:
        members = bundle.getmembers()
        names = {member.name for member in members}
        if (
            not members
            or len(members) != len(names)
            or chromium_digest in names
            or any(
                not member.isfile()
                or not re.fullmatch(r"[0-9a-f]{40,128}", member.name)
                for member in members
            )
        ):
            raise ValueError("Invalid WebView input archive cache")
        bundle.extractall(ARCHIVES, members=members, filter="data")


def setup_cached_inputs(stage: str) -> None:
    lock: dict[str, dict[str, str]] = json.loads(
        (ROOT / "locks/inputs.json").read_text(encoding="utf-8")
    )
    chromium = lock["chromium"]
    inputs = {
        "chromium": chromium["commit"],
        "dependencies": json.loads(
            (ROOT / "locks/dependencies.json").read_text(encoding="utf-8")
        ),
        **{name: lock[name] for name in ("jdk", "sdk", "ndk", "system_stubs")},
    }
    tag = sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    registry = f"ghcr.io/{os.environ['GITHUB_REPOSITORY'].lower()}"
    package = f"{registry}-build-inputs"
    reference = f"{package}:{tag}"
    auth = ["--username", os.environ["GITHUB_ACTOR"], "--password-stdin"]
    token = os.environ["GHCR_TOKEN"] + "\n"
    digest = registry_digest(reference, auth, token)
    if digest is None and stage != "plan":
        raise ValueError(
            f"Missing {reference}; the plan job fills the input archive cache"
        )

    needs_build = load_job()["hit"] is None
    ARCHIVES.mkdir(parents=True, exist_ok=True)
    cache_chromium(
        stage,
        f"{registry}-chromium-source",
        chromium,
        auth,
        token,
        needed=needs_build or digest is None,
    )
    if digest is None:
        with TemporaryDirectory(dir=ARCHIVES) as directory:
            work = Path(directory)
            seed = work / "archives"
            seed.mkdir()
            chromium_archive = ARCHIVES / chromium["sha256"]
            (seed / chromium_archive.name).hardlink_to(chromium_archive)
            setup(INPUTS, seed)
            archive = work / "build-inputs.tar"
            with tarfile.open(archive, "w") as bundle:
                for path in sorted(seed.iterdir()):
                    if path.name == chromium_archive.name:
                        continue
                    entry = tarfile.TarInfo(path.name)
                    with path.open("rb") as stream:
                        entry.size = path.stat().st_size
                        bundle.addfile(entry, stream)
            push_cache(
                reference,
                archive,
                "application/vnd.aosstudio.webview.build-inputs.v1",
                "application/vnd.oci.image.layer.v1.tar",
                auth,
                token,
            )
    elif needs_build:
        with TemporaryDirectory(dir=ARCHIVES) as directory:
            work = Path(directory)
            pull_cache(f"{package}@{digest}", work, auth, token)
            restore_input_archives(work / "build-inputs.tar", chromium["sha256"])
        # Intake verifies each restored file by its pinned archive or tree digest.
        setup(INPUTS, ARCHIVES)
    else:
        print(f"Reuse {reference}@{digest}", flush=True)


def load_plan() -> dict[str, Stage]:
    plan: dict[str, Stage] = json.loads(
        (STATE / "plan.json").read_text(encoding="utf-8")
    )
    return plan


def restore(cache: ReleaseCache, stage: str) -> None:
    job = load_job()
    if hit := job["hit"]:
        if stage != "apk":
            raise ValueError("Only APK verification restores a cached job's own output")
        archive = STATE / "result.tar.zst"
        cache.fetch(hit, archive)
        extract_tar(archive, BUILD)
        return

    archive = STATE / "handoff.tar.zst"
    restored: list[str] = []
    stages = load_plan()
    for name, receipt in job["upstream"].items():
        cache.fetch(receipt, archive)
        extract_tar(archive, BUILD)
        archive.unlink()
        restored.extend(stages[name]["outputs"])
    (STATE / "restored.txt").write_text("\n".join(restored), encoding="utf-8")


def build_environment() -> dict[str, str]:
    return os.environ | {
        "CHROMIUM_SRC": str(INPUTS / "chromium"),
        "ANDROID_HOME": str(INPUTS / "sdk"),
        "ANDROID_SDK_ROOT": str(INPUTS / "sdk"),
        "CLANG_ROOT": str(INPUTS / "clang"),
        "GN": str(INPUTS / "gn/gn"),
        "JAVA_HOME": str(INPUTS / "jdk"),
        "PYTHON": sys.executable,
        "JOBS": "4",
    }


def build(stage: str) -> None:
    env = build_environment()
    if stage == "toolchain":
        bootstrap(INPUTS, OUT / "bootstrap", ARCHIVES)
        subprocess.run(
            [
                "tar",
                "--zstd",
                "--create",
                "--file",
                STATE / "result.tar.zst",
                "--directory",
                INPUTS,
                "gn",
                "clang",
            ],
            check=True,
        )
        return
    if stage == "plan":
        subprocess.run(
            [
                sys.executable,
                ROOT / "scripts/plan.py",
                "generate",
                "--build",
                BUILD,
                "--plan",
                STATE / "plan.json",
            ],
            check=True,
            env=env,
        )
        return
    subprocess.run(
        [
            sys.executable,
            ROOT / "scripts/plan.py",
            "run",
            "--build",
            BUILD,
            "--plan",
            STATE / "plan.json",
            "--stage",
            stage,
            "--restored",
            STATE / "restored.txt",
        ],
        check=True,
        env=env,
    )
    outputs = load_plan()[stage]["outputs"]
    if stage == "apk":
        subprocess.run(
            ["bash", ROOT / "scripts/build.sh", "notices"], check=True, env=env
        )
        outputs = APK_FILES
    elif stage == "smoke":
        outputs = list(SMOKE)
    files = STATE / "outputs.txt"
    files.write_bytes(b"\0".join(os.fsencode(path) for path in outputs) + b"\0")
    subprocess.run(
        [
            "tar",
            "--zstd",
            "--create",
            "--file",
            STATE / "result.tar.zst",
            "--directory",
            BUILD,
            "--null",
            "--files-from",
            files,
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a stage of the GitHub-hosted WebView build."
    )
    parser.add_argument(
        "action",
        choices=(
            "lookup",
            "setup",
            "verify-setup",
            "configure",
            "restore",
            "build",
            "upload",
            "matrices",
            "verify",
        ),
    )
    parser.add_argument("stage")
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    match args.action:
        case "lookup" | "restore" | "upload":
            cache = ReleaseCache(
                os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF_NAME"]
            )
            if args.action == "lookup":
                lookup(cache, args.stage)
            elif args.action == "restore":
                restore(cache, args.stage)
            else:
                job = load_job()
                path = STATE / (
                    "plan.json" if args.stage == "plan" else "result.tar.zst"
                )
                cache.store(job["release"], job["name"], path)
        case "setup":
            if load_job()["hit"] is None:
                toolchain = STATE / "toolchain.tar.zst"
                extract_tar(toolchain, INPUTS)
                toolchain.unlink()
            setup_cached_inputs(args.stage)
        case "verify-setup":
            setup(INPUTS, ARCHIVES, verify_only=True)
        case "configure":
            subprocess.run(
                ["bash", ROOT / "scripts/build.sh", "configure"],
                check=True,
                env=build_environment(),
            )
        case "build":
            build(args.stage)
        case "matrices":
            emit_matrices()
        case "verify":
            subprocess.run(
                [
                    sys.executable,
                    ROOT / "scripts/verify.py",
                    "--apk",
                    BUILD / APK_FILES[0],
                    "--sdk",
                    INPUTS / "sdk",
                ],
                check=True,
                env=build_environment(),
            )


if __name__ == "__main__":
    main()
