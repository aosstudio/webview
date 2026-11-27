import argparse
from collections import defaultdict
from dataclasses import dataclass
import heapq
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Iterator, TypedDict


ROOT = Path(__file__).resolve().parents[1]
NODE = re.compile(r'^"(0x[0-9a-f]+)" \[label="(.*)"(, shape=ellipse)?\]$')
EDGE = re.compile(r'^"(0x[0-9a-f]+)" -> "(0x[0-9a-f]+)"(?: \[(.*)\])?$')
COMPILE = {"cc", "cxx", "asm"}
APK = "system_webview_apk"
LINK = "libwebviewchromium.so.TOC"
JAVA = (
    "obj/android_webview/system_webview_64_apk/system_webview_64_apk.r8dex.jar",
    "phony/android_webview/system_webview_64_apk__optimize_resources",
)
SMOKE = (
    "obj/third_party/zlib/zlib_data_chunk_simd/inflate.o",
    "obj/base/base/command_line.o",
    "obj/third_party/blink/public/mojom/mojom_platform/permission.mojom.o",
)


class Stage(TypedDict):
    targets: list[str]
    outputs: list[str]


@dataclass
class Graph:
    rules: dict[int, str]
    parents: dict[int, set[int]]
    outputs: dict[int, list[str]]
    producers: dict[str, int]

    def closure(self, starts: set[int]) -> set[int]:
        pending = list(starts)
        visited: set[int] = set()
        while pending:
            action = pending.pop()
            if action not in visited:
                visited.add(action)
                pending.extend(self.parents[action] - visited)
        return visited

    def stage(self, actions: set[int]) -> Stage:
        frontier = actions - set().union(*(self.parents[action] for action in actions))
        return Stage(
            targets=sorted(self.outputs[action][0] for action in frontier),
            outputs=sorted(
                path
                for action in actions
                if self.rules[action] != "phony"
                for path in self.outputs[action]
            ),
        )


def read_graph(lines: Iterator[str]) -> Graph:
    files: dict[int, str] = {}
    rules: dict[int, str] = {}
    inputs: dict[int, set[int]] = defaultdict(set)
    outputs: dict[int, set[int]] = defaultdict(set)
    producers: dict[int, int] = {}
    for line in lines:
        line = line.rstrip("\n")
        if match := NODE.fullmatch(line):
            node = int(match[1], 16)
            if match[3]:
                rules[node] = match[2]
            else:
                files[node] = match[2]
        elif match := EDGE.fullmatch(line):
            left, right = int(match[1], 16), int(match[2], 16)
            style = match[3] or ""
            if style.startswith('label=" '):
                rules[right] = style[8:-1]
                inputs[right].add(left)
                outputs[right].add(right)
                producers[right] = right
            elif style.startswith("arrowhead=none"):
                inputs[right].add(left)
            elif not style:
                outputs[left].add(right)
                producers[right] = left
            else:
                raise ValueError(f"Unexpected Ninja graph edge {line!r}")
        elif line not in {
            "digraph ninja {",
            "}",
            'rankdir="LR"',
            "node [fontsize=10, shape=box, height=0.25]",
            "edge [fontsize=10]",
        }:
            raise ValueError(f"Unexpected Ninja graph line {line!r}")
    return Graph(
        rules=rules,
        parents={
            action: {producers[node] for node in inputs[action] if node in producers}
            for action in rules
        },
        outputs={
            action: sorted(files[node] for node in nodes if node in files)
            for action, nodes in outputs.items()
        },
        producers={
            files[node]: action for node, action in producers.items() if node in files
        },
    )


def ninja_graph(build: Path, targets: list[str]) -> Graph:
    with subprocess.Popen(
        ["ninja", "-C", build, "-t", "graph", *targets],
        stdout=subprocess.PIPE,
        encoding="utf-8",
        errors="surrogateescape",
    ) as process:
        assert process.stdout is not None
        graph = read_graph(iter(process.stdout))
        if process.wait():
            raise RuntimeError("ninja -t graph failed")
    # DOT labels only the outputs that are visited by the traversal. Include
    # sibling outputs of the same edge so the handoff can restore the whole edge.
    for manifest in build.rglob("*.ninja"):
        with manifest.open(encoding="utf-8", errors="surrogateescape") as source:
            for line in source:
                if not line.startswith("build "):
                    continue
                siblings = build_outputs(line)
                for sibling in siblings:
                    if (action := graph.producers.get(sibling)) is not None:
                        graph.outputs[action] = sorted(siblings)
                        graph.producers.update(dict.fromkeys(siblings, action))
                        break
    return graph


def build_outputs(line: str) -> set[str]:
    return {
        str(PurePosixPath(path))
        for path in line[6:].partition(":")[0].split()
        if path != "|"
    }


def weight(path: str, profile: dict[str, int]) -> int:
    parts = PurePosixPath(path).parts
    for prefix in sorted(profile, key=len, reverse=True):
        if parts[: len(prefix.split("/"))] == tuple(prefix.split("/")):
            return profile[prefix]
    return 3096


def split(
    graph: Graph, actions: set[int], count: int, profile: dict[str, int]
) -> list[set[int]]:
    weighted = [
        (weight(graph.outputs[action][0], profile), graph.outputs[action][0], action)
        for action in actions
    ]
    loads = [(0, slot) for slot in range(count)]
    batches: list[set[int]] = [set() for _ in loads]
    for duration, _, action in sorted(weighted, reverse=True):
        total, slot = heapq.heappop(loads)
        batches[slot].add(action)
        heapq.heappush(loads, (total + duration, slot))
    if not all(batches):
        raise ValueError("Ninja graph cannot fill the requested compile matrix")
    return batches


def plan(graph: Graph, profile: dict[str, int]) -> dict[str, Stage]:
    apk = graph.closure({graph.producers[APK]})
    link = graph.closure({graph.producers[LINK]})
    java = graph.closure({graph.producers[path] for path in JAVA})
    native = {action for action in apk if graph.rules[action] in COMPILE}
    prepare = (
        graph.closure(set().union(*(graph.parents[action] for action in native)))
        | (link & java)
        | (native & java)
    )
    compiles = {
        action
        for action in prepare
        if graph.rules[action].removeprefix("clang_x64_") in COMPILE
    }
    base = graph.closure(set().union(*(graph.parents[action] for action in compiles)))
    early = split(graph, compiles - base, 4, profile)
    finish = prepare - base - compiles
    batches = split(graph, native - prepare, 24, profile)
    stages: dict[str, tuple[set[int], set[int]]] = {
        "prepare-base": (base, set()),
        **{f"prepare-{i:02}": (batch, base) for i, batch in enumerate(early, 1)},
        "prepare-finish": (finish, base | compiles),
        **{f"native-{i:02}": (batch, prepare) for i, batch in enumerate(batches, 1)},
        "java": (java - prepare, prepare),
        "link": (link - prepare - native, prepare | native),
        "apk": (apk - prepare - native - link - java, prepare | native | link | java),
    }
    used = [actions for actions, _ in stages.values()]
    if set().union(*used) != apk or sum(map(len, used)) != len(apk):
        raise ValueError("The stages do not partition the APK graph")
    stages["smoke"] = (graph.closure({graph.producers[path] for path in SMOKE}), set())
    result = {}
    for name, (actions, available) in stages.items():
        stage = graph.stage(actions)
        if (
            not stage["targets"]
            or graph.closure({graph.producers[path] for path in stage["targets"]})
            - available
            != actions
        ):
            raise ValueError(f"Unexpected prerequisites for stage {name}")
        for output in stage["outputs"]:
            path = PurePosixPath(output)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Unsafe Ninja output {output}")
        result[name] = stage
    return result


def remove_restored_edges(build: Path, restored: set[str]) -> None:
    # Restored files are external inputs to this invocation, so Ninja needs no
    # compilation log or depfile database from a worker that produced them.
    found: set[str] = set()
    for manifest in sorted(build.rglob("*.ninja")):
        source = manifest.read_text(
            encoding="utf-8", errors="surrogateescape"
        ).splitlines(keepends=True)
        rewritten: list[str] = []
        skip_bindings = False
        for line in source:
            if not line.startswith((" ", "\t")):
                skip_bindings = False
            if line.startswith("build "):
                fields = build_outputs(line)
                if fields & restored:
                    if not fields <= restored or line.rstrip().endswith("$"):
                        raise ValueError(
                            f"Partial or continued Ninja edge {line.rstrip()}"
                        )
                    found.update(fields)
                    skip_bindings = True
            if not skip_bindings:
                rewritten.append(line)
        if source != rewritten:
            manifest.write_text(
                "".join(rewritten), encoding="utf-8", errors="surrogateescape"
            )
    if missing := restored - found:
        raise ValueError(
            f"Could not bind restored outputs to Ninja edges: {sorted(missing)[:5]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Partition and execute the WebView Ninja graph."
    )
    parser.add_argument("mode", choices=("generate", "run"))
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--stage")
    parser.add_argument("--restored", type=Path)
    args = parser.parse_args()
    if args.mode == "generate":
        profile = json.loads(
            (ROOT / "ci/compile-weights.json").read_text(encoding="utf-8")
        )
        result = plan(ninja_graph(args.build, [APK]), profile)
        args.plan.write_text(
            json.dumps(result, separators=(",", ":")), encoding="utf-8"
        )
        return
    stages: dict[str, Stage] = json.loads(args.plan.read_text(encoding="utf-8"))
    stage = stages[args.stage]
    restored = set(args.restored.read_text(encoding="utf-8").splitlines())
    if restored:
        remove_restored_edges(args.build, restored)
    graph = ninja_graph(args.build, stage["targets"])
    if graph.stage(set(graph.rules))["outputs"] != stage["outputs"]:
        raise ValueError(f"Ninja graph after restoring inputs differs for {args.stage}")
    subprocess.run(
        ["bash", ROOT / "scripts/build.sh", "ninja", *stage["targets"]], check=True
    )


if __name__ == "__main__":
    main()
