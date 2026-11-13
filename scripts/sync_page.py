"""Copies band3's library page (web/library.html in a band3_recomp checkout) into
this repo's web/library.html, which the Docker image serves at /browse. band3's
repo is private, so the image can't fetch the page itself.

  python scripts/sync_page.py ../band3_recomp
  python scripts/sync_page.py ../band3_recomp --check    exit 1 if the copy differs
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "web" / "library.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("band3", type=Path, help="a band3_recomp checkout")
    parser.add_argument("--check", action="store_true", help="only say whether the copy differs")
    args = parser.parse_args()

    source = args.band3 / "web" / "library.html"
    if not source.is_file():
        print(f"no page at {source}", file=sys.stderr)
        return 2
    # LF, as band3's build embeds it
    page = source.read_bytes().replace(b"\r\n", b"\n")
    current = HERE.read_bytes() if HERE.is_file() else b""
    commit = subprocess.run(["git", "-C", str(args.band3), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip() or "unknown"

    if page == current:
        print(f"web/library.html matches band3 {commit}")
        return 0
    if args.check:
        print(f"web/library.html differs from band3 {commit}")
        return 1
    HERE.write_bytes(page)
    print(f"web/library.html copied from band3 {commit}; commit it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
