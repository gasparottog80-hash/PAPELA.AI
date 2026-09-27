"""Verify installed extractor provenance against uv.lock without reading its code."""

from __future__ import annotations

import json
import re
import tomllib
from importlib.metadata import distribution
from pathlib import Path


def main() -> None:
    lock_path = Path(__file__).resolve().parents[1] / "uv.lock"
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    package = next(
        p for p in lock["package"] if p["name"] == "papela-fiscal-extractor"
    )
    locked_commit = package["source"]["git"].rsplit("#", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{40}", locked_commit):
        raise SystemExit("FAIL: extractor lock source must contain a full commit SHA")

    installed = distribution("papela-fiscal-extractor")
    provenance = installed.read_text("direct_url.json")
    if provenance is None:
        raise SystemExit("FAIL: extractor installation has no VCS provenance")
    direct_url = json.loads(provenance)
    vcs = direct_url.get("vcs_info", {})
    installed_commit = vcs.get("commit_id")
    if (
        vcs.get("vcs") != "git"
        or installed_commit != locked_commit
        or installed.version != package["version"]
        or direct_url.get("url") != package["source"]["git"].split("?", 1)[0]
    ):
        raise SystemExit("FAIL: installed extractor provenance differs from uv.lock")

    print(
        json.dumps(
            {
                "status": "PASS",
                "version": installed.version,
                "installed_commit": installed_commit,
                "lock_commit": locked_commit,
            }
        )
    )


if __name__ == "__main__":
    main()
