"""Stage the locked formatter dependencies used by ts-proto."""

import argparse
from base64 import b64decode
import json
from pathlib import Path

from intake import extract_tar, fetch

PACKAGES = ("node_modules/detect-libc", "node_modules/dprint-node")


def install_ts_proto(source: Path, cache: Path) -> None:
    node = source / "third_party/node"
    packages: dict[str, dict[str, str]] = json.loads(
        (node / "package-lock.json").read_text(encoding="utf-8")
    )["packages"]
    for path in PACKAGES:
        package = packages[path]
        url = package["resolved"]
        if not url.startswith("https://registry.npmjs.org/"):
            raise ValueError(f"Unexpected package registry for {path}")
        algorithm, value = package["integrity"].split("-", 1)
        digest = b64decode(value, validate=True).hex()
        extract_tar(fetch(cache, url, digest, algorithm), node / path, 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("archives", type=Path)
    args = parser.parse_args()
    install_ts_proto(args.source, args.archives)
