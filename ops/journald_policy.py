"""Render, but never install, the reviewed host-wide journal retention policy."""

from __future__ import annotations

import argparse
import re

_SIZE = re.compile(r"[1-9][0-9]*[KMG]\Z")


def render_policy(days: int = 14, max_use: str = "1G", keep_free: str = "5G") -> str:
    if not 1 <= days <= 365:
        raise ValueError("retention days must be between 1 and 365")
    if not _SIZE.fullmatch(max_use) or not _SIZE.fullmatch(keep_free):
        raise ValueError("journal sizes must be positive K, M or G values")
    return (
        "# Host-wide policy; review competing journal users and disk budget first.\n"
        "[Journal]\n"
        "Storage=persistent\n"
        "Compress=yes\n"
        "Seal=yes\n"
        f"MaxRetentionSec={days}day\n"
        f"SystemMaxUse={max_use}\n"
        f"SystemKeepFree={keep_free}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--max-use", default="1G")
    parser.add_argument("--keep-free", default="5G")
    args = parser.parse_args()
    print(render_policy(args.days, args.max_use, args.keep_free), end="")


if __name__ == "__main__":
    main()
