import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from platform import python_version
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import TypedDict

from intake import bootstrap, extract_tar, fetch, setup
from plan import ROOT, SMOKE, Stage
from release_cache import Asset, Receipt, ReleaseCache, checksum
from ts_proto import install_ts_proto


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


def stable_hash(payload: object) -> str:
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def identity(receipt: Receipt) -> tuple[int, str]:
    return receipt["id"], receipt["digest"]


def host_platform() -> list[str]:
    return [os.environ[name] for name in ("RUNNER_OS", "RUNNER_ARCH", "ImageOS")]


def toolchain_name() -> str:
    inputs = json.loads((ROOT / "locks/inputs.json").read_text(encoding="utf-8"))
    fingerprint = stable_hash(
        {
            "host": host_platform(),
            "inputs": {name: inputs[name] for name in ("llvm", "gn", "ndk")},
            "bootstrap": checksum(ROOT / "scripts/bootstrap.sh"),
            "intake": checksum(ROOT / "scripts/intake.py"),
        }
    )
    return f"toolchain-{fingerprint}.tar.zst"


def plan_name(toolchain: Receipt) -> str:
    inputs = [
        "locks/inputs.json",
        "locks/dependencies.json",
        *(
            path.relative_to(ROOT).as_posix()
            for path in sorted((ROOT / "config").rglob("*"))
            if path.is_file()
        ),
        "ci/compile-weights.json",
        "scripts/build.sh",
        "scripts/intake.py",
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
            "toolchain": identity(toolchain),
            "host": host_platform(),
            "python": python_version(),
            "recipe": {path: checksum(ROOT / path) for path in inputs},
        }
    )
    return f"plan-{fingerprint}.json"


def needs_ts_proto(stage: Stage) -> bool:
    return any(path.startswith("gen/tsproto/") for path in stage["outputs"])


def stage_name(
    stage: str, plan: Receipt, parents: dict[str, Receipt], definition: Stage
) -> str:
    recipe: dict[str, object] = {
        "plan": identity(plan),
        "parents": {name: identity(receipt) for name, receipt in parents.items()},
        "packaging": checksum(ROOT / "scripts/package.sh") if stage == "apk" else "",
    }
    if needs_ts_proto(definition):
        recipe["ts-proto"] = checksum(ROOT / "scripts/ts_proto.py")
    return f"{stage}-{stable_hash(recipe)}.tar.zst"


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
    if stage == "toolchain":
        name = toolchain_name()
        release = cache.ensure()
        hit = cache.lookup(release, name)
        job_output("release", str(release))
        job_output("toolchain", name)
    else:
        release = int(os.environ["CACHE_RELEASE"])
        assets: dict[str, Asset] = cache.assets(release)
        toolchain = cache.lookup(release, os.environ["CACHE_TOOLCHAIN"], assets=assets)
        if toolchain is None:
            raise ValueError("Missing build toolchain in the branch's draft Release")
        if stage == "plan":
            name = plan_name(toolchain)
            hit = cache.lookup(release, name, assets=assets)
            if hit is not None:
                cache.fetch(hit, STATE / "plan.json")
            job_output("plan", name)
        else:
            plan_key = os.environ["CACHE_PLAN"]
            plan = cache.lookup(release, plan_key, assets=assets)
            if plan is None:
                raise ValueError("The plan was removed during the build")
            cache.fetch(plan, STATE / "plan.json")
            name = lookup_stage(cache, release, assets, stage, plan, upstream)
            hit = cache.lookup(release, name, assets=assets)
            if hit is None:
                if plan_name(toolchain) != plan_key:
                    raise ValueError(
                        "Runner or checkout inputs differ from the plan job"
                    )
        if hit is None:
            cache.fetch(toolchain, STATE / "toolchain.tar.zst")

    job = Job(name=name, release=release, hit=hit, upstream=upstream)
    (STATE / "job.json").write_text(json.dumps(job), encoding="utf-8")
    job_output("hit", "true" if hit else "false")
    print(f"{'Reuse' if hit else 'Build'} {name} in draft {release}")


def lookup_stage(
    cache: ReleaseCache,
    release: int,
    assets: dict[str, Asset],
    stage: str,
    plan: Receipt,
    upstream: dict[str, Receipt],
) -> str:
    stages = load_plan()
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
                name = stage_name(parent, plan, dependencies(parent), stages[parent])
                receipt = cache.lookup(release, name, assets=assets)
                if receipt is None:
                    raise ValueError(f"Missing upstream handoff for {parent}")
                upstream[parent] = receipt
            direct[parent] = upstream[parent]
        return direct

    return stage_name(stage, plan, dependencies(stage), stages[stage])


def load_job() -> Job:
    job: Job = json.loads((STATE / "job.json").read_text(encoding="utf-8"))
    return job


def chromium_cache_digest(reference: str, auth: list[str], token: str) -> str | None:
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


def cache_chromium(stage: str) -> None:
    source = json.loads((ROOT / "locks/inputs.json").read_text(encoding="utf-8"))[
        "chromium"
    ]
    revision = source["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"Invalid Chromium commit {revision}")
    repo = os.environ["GITHUB_REPOSITORY"]
    package = f"ghcr.io/{repo.lower()}-chromium-source"
    reference = f"{package}:{revision}"
    auth = ["--username", os.environ["GITHUB_ACTOR"], "--password-stdin"]
    token = os.environ["GHCR_TOKEN"] + "\n"
    digest = chromium_cache_digest(reference, auth, token)
    if digest is None and stage != "plan":
        raise ValueError(f"Missing {reference}; the plan job fills the source cache")

    filename = "chromium.tar.gz"
    ARCHIVES.mkdir(parents=True, exist_ok=True)
    if digest is None:
        print(f"Fetch Chromium {revision} from Gitiles", flush=True)
        archive = fetch(
            ARCHIVES,
            source["url"],
            source["sha256"],
            tree_digest=source["tree_sha256"],
        )
        # Another branch can publish the same snapshot during the upstream fetch.
        if chromium_cache_digest(reference, auth, token) is not None:
            print(f"Reuse {reference} published by another job", flush=True)
            return
        with TemporaryDirectory(dir=ARCHIVES) as directory:
            (Path(directory) / filename).hardlink_to(archive)
            print(f"Publish {reference}", flush=True)
            subprocess.run(
                [
                    "oras",
                    "push",
                    *auth,
                    "--artifact-type",
                    "application/vnd.aosstudio.chromium.source.v1",
                    "--annotation",
                    f"org.opencontainers.image.source=https://github.com/{repo}",
                    reference,
                    f"{filename}:application/gzip",
                ],
                input=token,
                text=True,
                cwd=directory,
                check=True,
            )
        return

    print(f"Reuse {reference}@{digest}", flush=True)
    if load_job()["hit"] is None:
        with TemporaryDirectory(dir=ARCHIVES) as directory:
            subprocess.run(
                ["oras", "pull", *auth, "--output", directory, f"{package}@{digest}"],
                input=token,
                text=True,
                check=True,
            )
            # The input staging code checks the locked archive or tree digest.
            (Path(directory) / filename).replace(ARCHIVES / source["sha256"])


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
        extract_tar(archive, BUILD)
        archive.unlink()
        return
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
            "chromium-cache",
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
        case "chromium-cache":
            cache_chromium(args.stage)
        case "setup" | "verify-setup":
            verify_only = args.action == "verify-setup"
            if not verify_only:
                toolchain = STATE / "toolchain.tar.zst"
                extract_tar(toolchain, INPUTS)
                toolchain.unlink()
            setup(INPUTS, ARCHIVES, verify_only)
            if (
                not verify_only
                and args.stage != "plan"
                and needs_ts_proto(load_plan()[args.stage])
            ):
                install_ts_proto(INPUTS / "chromium", ARCHIVES)
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
