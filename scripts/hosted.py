import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from typing import TypedDict

from plan import ROOT, SMOKE, Stage
from release_cache import Asset, Receipt, ReleaseCache, checksum


OUT = ROOT / "out"
INPUTS = OUT / "inputs"
BUILD = OUT / "build"
STATE = OUT / "hosted"
APK_FILES = [
    "apks/SystemWebView64.apk",
    "apks/NOTICE",
    "apks/SystemWebView64.spdx.json",
    "apks/build-inputs.json",
]


class AssetSpec(TypedDict):
    url: str
    sha256: str


class Job(TypedDict):
    name: str
    release: int
    hit: Receipt | None
    upstream: dict[str, Receipt]


class Matrices(TypedDict):
    prepare: list[str]
    native: list[str]


def stable_hash(payload: object) -> str:
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def identity(receipt: Receipt) -> tuple[int, str]:
    return receipt["id"], receipt["digest"]


def plan_name() -> str:
    inputs = [
        "locks/inputs.json",
        "config/webview.gn",
        "ci/compile-weights.json",
        "scripts/build.sh",
        "scripts/plan.py",
        "patches/series",
        *(
            f"patches/{name}"
            for name in (ROOT / "patches/series")
            .read_text(encoding="utf-8")
            .splitlines()
        ),
    ]
    fingerprint = stable_hash(
        {
            "format": 1,
            "inputs": os.environ["INPUTS_SHA256"],
            "host": [
                os.environ["RUNNER_OS"],
                os.environ["RUNNER_ARCH"],
                os.environ["ImageOS"],
                os.environ["ImageVersion"],
            ],
            "python": subprocess.check_output(
                ["python", "--version"], text=True
            ).strip(),
            "recipe": {path: checksum(ROOT / path) for path in inputs},
        }
    )
    return f"plan-{fingerprint}.json"


def stage_name(stage: str, plan: Receipt, parents: dict[str, Receipt]) -> str:
    fingerprint = stable_hash(
        {
            "plan": identity(plan),
            "parents": {name: identity(receipt) for name, receipt in parents.items()},
            "packaging": checksum(ROOT / "scripts/package.sh")
            if stage == "apk"
            else "",
        }
    )
    return f"{stage}-{fingerprint}.tar.zst"


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
    if stage == "plan":
        name = plan_name()
        release = cache.ensure()
        hit = cache.lookup(release, name)
        if hit is not None:
            cache.fetch(hit, STATE / "plan.json")
        job_output("release", str(release))
        job_output("plan", name)
    else:
        release = int(os.environ["CACHE_RELEASE"])
        name = plan_name()
        if name != os.environ["CACHE_PLAN"]:
            raise ValueError("Runner or checkout inputs differ from the plan job")
        assets: dict[str, Asset] = cache.assets(release)
        plan = cache.lookup(release, name, assets=assets)
        if plan is None:
            raise ValueError("The plan was removed during the build")

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

        def dependencies(job: str) -> dict[str, Receipt]:
            direct: dict[str, Receipt] = {}
            for parent in parents[job]:
                if parent not in upstream:
                    parent_name = stage_name(parent, plan, dependencies(parent))
                    receipt = cache.lookup(release, parent_name, assets=assets)
                    if receipt is None:
                        raise ValueError(f"Missing upstream handoff for {parent}")
                    upstream[parent] = receipt
                direct[parent] = upstream[parent]
            return direct

        name = stage_name(stage, plan, dependencies(stage))
        hit = cache.lookup(release, name, assets=assets)
        if hit is None:
            cache.fetch(plan, STATE / "plan.json")
    job = Job(name=name, release=release, hit=hit, upstream=upstream)
    (STATE / "job.json").write_text(json.dumps(job), encoding="utf-8")
    job_output("hit", "true" if hit else "false")
    print(f"{'Reuse' if hit else 'Build'} {name} in draft {release}")


def download(spec: AssetSpec, destination: Path) -> None:
    if not spec["url"].startswith("https://") or not re.fullmatch(
        r"[0-9a-f]{64}", spec["sha256"]
    ):
        raise ValueError("Inputs require an HTTPS URL and a SHA-256 digest")
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
            destination,
            "--",
            spec["url"],
        ],
        check=True,
    )
    if checksum(destination) != f"sha256:{spec['sha256']}":
        raise ValueError(f"Digest mismatch for {spec['url']}")


def extract(archive: Path, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["tar", "--extract", "--no-same-owner", "--file", archive, "--directory", root],
        check=True,
    )


def setup(verify_only: bool) -> None:
    manifest = STATE / "inputs.json"
    download(
        AssetSpec(url=os.environ["INPUTS_URL"], sha256=os.environ["INPUTS_SHA256"]),
        manifest,
    )
    sources: dict[str, list[AssetSpec]] = json.loads(
        manifest.read_text(encoding="utf-8")
    )
    for component in (
        ["sdk", "jdk"] if verify_only else ["sdk", "jdk", "clang", "gn", "chromium"]
    ):
        entries = sources[component]
        if not entries:
            raise ValueError(f"No prepared archive for {component}")
        for entry in entries:
            temporary = STATE / "input.tar"
            download(entry, temporary)
            extract(temporary, INPUTS / component)
            temporary.unlink()
    if not verify_only:
        upstream = INPUTS / "chromium"
        java = upstream / "third_party/jdk/current"
        java.parent.mkdir(parents=True, exist_ok=True)
        java.symlink_to(INPUTS / "jdk", target_is_directory=True)
        for name in (ROOT / "patches/series").read_text(encoding="utf-8").splitlines():
            subprocess.run(
                [
                    "patch",
                    "--directory",
                    upstream,
                    "--batch",
                    "--forward",
                    "--fuzz=0",
                    "-p1",
                    "--input",
                    ROOT / "patches" / name,
                ],
                check=True,
            )


def load_job() -> Job:
    job: Job = json.loads((STATE / "job.json").read_text(encoding="utf-8"))
    return job


def load_plan() -> dict[str, Stage]:
    plan: dict[str, Stage] = json.loads(
        (STATE / "plan.json").read_text(encoding="utf-8")
    )
    return plan


def restore(cache: ReleaseCache, stage: str) -> None:
    job = load_job()
    archive = STATE / "handoff.tar.zst"
    restored: list[str] = []
    if hit := job["hit"]:
        if stage != "apk":
            raise ValueError("Only APK verification restores a cached job's own output")
        cache.fetch(hit, archive)
        extract(archive, BUILD)
        archive.unlink()
        return
    stages = load_plan()
    for name, receipt in job["upstream"].items():
        cache.fetch(receipt, archive)
        extract(archive, BUILD)
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
        "PYTHON": "python",
        "JOBS": "4",
    }


def build(stage: str) -> None:
    env = build_environment()
    if stage == "plan":
        subprocess.run(
            [
                "python",
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
            "python",
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
        case "setup" | "verify-setup":
            setup(args.action == "verify-setup")
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
                    "python",
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
