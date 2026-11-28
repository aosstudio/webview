import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import re
from shutil import copyfileobj
import subprocess
import tempfile
from typing import Any, TypedDict
from urllib.parse import quote, urlencode
from uuid import uuid4


class Asset(TypedDict):
    id: int
    name: str
    size: int
    digest: str


class Receipt(TypedDict):
    id: int
    digest: str
    parts: list[Asset]


def api(endpoint: str, *options: str, missing_is_ok: bool = False) -> Any:
    result = subprocess.run(
        ["gh", "api", *options, endpoint], capture_output=True, text=True
    )
    if result.returncode:
        if missing_is_ok and result.stderr.rstrip().endswith("(HTTP 404)"):
            return None
        raise RuntimeError(result.stderr.rstrip())
    return json.loads(result.stdout) if result.stdout else None


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + file_digest(stream, "sha256").hexdigest()


def read_asset(entry: dict[str, Any]) -> Asset | None:
    digest = entry.get("digest")
    if (
        entry["state"] != "uploaded"
        or not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
        or entry["size"] <= 0
    ):
        return None
    return Asset(id=entry["id"], name=entry["name"], size=entry["size"], digest=digest)


class ReleaseCache:
    def __init__(self, repo: str, branch: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Expected owner/repository")
        subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"], check=True)
        self.repo = f"repos/{repo}"
        self.branch = branch
        stem = re.sub(r"[^a-z0-9-]", "-", branch.lower())[:40]
        ref_digest = sha256(f"refs/heads/{branch}".encode()).hexdigest()[:24]
        self.tag = f"ci-cache-{stem}-{ref_digest}"
        self.marker = f"WebView CI cache for refs/heads/{branch}"

    def branch_exists(self) -> bool:
        ref = quote(f"heads/{self.branch}", safe="/")
        result = api(f"{self.repo}/git/ref/{ref}", missing_is_ok=True)
        return result is not None and result["ref"] == f"refs/heads/{self.branch}"

    def find(self) -> int | None:
        pages = api(f"{self.repo}/releases?per_page=100", "--paginate", "--slurp")
        matching = [
            release
            for page in pages
            for release in page
            if release["tag_name"] == self.tag
        ]
        if not matching:
            return None
        if len(matching) != 1 or not (
            matching[0]["draft"] and matching[0]["body"] == self.marker
        ):
            raise ValueError(f"Unexpected release for {self.tag}")
        return int(matching[0]["id"])

    def ensure(self) -> int:
        if not self.branch_exists():
            raise ValueError(f"Branch {self.branch} no longer exists")
        release_id = self.find()
        if release_id is not None:
            return release_id
        result = api(
            f"{self.repo}/releases",
            "--method",
            "POST",
            "-f",
            f"tag_name={self.tag}",
            "-f",
            f"name={self.tag}",
            "-f",
            f"target_commitish={self.branch}",
            "-f",
            f"body={self.marker}",
            "-F",
            "draft=true",
        )
        return int(result["id"])

    def cleanup(self) -> None:
        if self.branch_exists():
            return
        release_id = self.find()
        if release_id is not None and not self.branch_exists():
            api(
                f"{self.repo}/releases/{release_id}",
                "--method",
                "DELETE",
                missing_is_ok=True,
            )

    def asset_entries(self, release_id: int) -> list[dict[str, Any]]:
        pages = api(
            f"{self.repo}/releases/{release_id}/assets?per_page=100",
            "--paginate",
            "--slurp",
        )
        return [entry for page in pages for entry in page]

    def assets(self, release_id: int) -> dict[str, Asset]:
        return {
            asset["name"]: asset
            for entry in self.asset_entries(release_id)
            if (asset := read_asset(entry)) is not None
        }

    def download(self, asset: Asset, destination: Path) -> None:
        with destination.open("wb") as stream:
            subprocess.run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/octet-stream",
                    f"{self.repo}/releases/assets/{asset['id']}",
                ],
                stdout=stream,
                check=True,
            )
        if (
            destination.stat().st_size != asset["size"]
            or checksum(destination) != asset["digest"]
        ):
            raise ValueError(f"Digest mismatch for {asset['name']}")

    def lookup(
        self, release_id: int, name: str, assets: dict[str, Asset] | None = None
    ) -> Receipt | None:
        if assets is None:
            assets = self.assets(release_id)
        if asset := assets.get(name):
            return Receipt(id=asset["id"], digest=asset["digest"], parts=[asset])
        if not (index := assets.get(f"{name}.json")):
            return None
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "manifest.json"
            self.download(index, manifest)
            parts: list[Asset] = json.loads(manifest.read_text(encoding="utf-8"))
        if not parts or any(assets.get(part["name"]) != part for part in parts):
            return None
        return Receipt(id=index["id"], digest=index["digest"], parts=parts)

    def upload(self, release_id: int, name: str, path: Path) -> Asset:
        # GitHub's uploads host accepts the file stream and returns the stored digest.
        url = f"https://uploads.github.com/{self.repo}/releases/{release_id}/assets?"
        headers = f"Authorization: Bearer {os.environ['GH_TOKEN']}\nContent-Type: application/octet-stream\n"
        run = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        label = f"{os.environ['GITHUB_SHA']} {run}"
        result = subprocess.run(
            [
                "curl",
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--header",
                "@-",
                "--data-binary",
                f"@{path}",
                url + urlencode({"name": name, "label": label}),
            ],
            input=headers,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            details = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Could not upload {name}: {details}")
        entry = read_asset(json.loads(result.stdout))
        if entry is None or entry["name"] != name or entry["digest"] != checksum(path):
            raise ValueError(f"Upload could not be verified for {name}")
        return entry

    def store(self, release_id: int, name: str, path: Path) -> Receipt:
        if not self.branch_exists() or self.find() != release_id:
            raise ValueError(f"Branch {self.branch} or its draft no longer exists")
        entries = self.asset_entries(release_id)
        assets = {
            asset["name"]: asset
            for entry in entries
            if (asset := read_asset(entry)) is not None
        }
        if existing := self.lookup(release_id, name, assets=assets):
            return existing
        # An interrupted upload can leave a starter asset or an index whose
        # parts were deleted. Clear the exact unfinished entry before retrying.
        for entry in entries:
            if entry["name"] in {name, f"{name}.json"}:
                api(
                    f"{self.repo}/releases/assets/{entry['id']}",
                    "--method",
                    "DELETE",
                    missing_is_ok=True,
                )
        if path.stat().st_size < 2 * 1024**3:
            asset = self.upload(release_id, name, path)
            return Receipt(id=asset["id"], digest=asset["digest"], parts=[asset])
        with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
            scratch = Path(temporary)
            subprocess.run(
                ["split", "-b", "1900M", "--", path, scratch / "part-"], check=True
            )
            attempt = uuid4().hex
            parts = [
                self.upload(release_id, f"{name}.{attempt}.{part.name}", part)
                for part in sorted(scratch.glob("part-*"))
            ]
            manifest = scratch / "manifest.json"
            manifest.write_text(json.dumps(parts), encoding="utf-8")
            asset = self.upload(release_id, f"{name}.json", manifest)
        return Receipt(id=asset["id"], digest=asset["digest"], parts=parts)

    def fetch(self, receipt: Receipt, destination: Path) -> None:
        if len(receipt["parts"]) == 1:
            self.download(receipt["parts"][0], destination)
            return
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            part_file = Path(temporary) / "part"
            with destination.open("wb") as archive:
                for part in receipt["parts"]:
                    self.download(part, part_file)
                    with part_file.open("rb") as stream:
                        copyfileobj(stream, archive, length=1024**2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove the draft Release for a deleted build branch."
    )
    parser.add_argument("--branch", required=True)
    args = parser.parse_args()
    cache = ReleaseCache(os.environ["GITHUB_REPOSITORY"], args.branch)
    cache.cleanup()


if __name__ == "__main__":
    main()
