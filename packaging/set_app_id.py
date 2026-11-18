"""Set Leaf's reverse-DNS app id and GitHub owner in the packaging files, once the GitHub repository exists.

    python packaging/set_app_id.py GITHUB_OWNER [--repo leaf] [--app-id io.example.Leaf] [--dry-run]

The app id is the name of packaging/linux/<app-id>.metainfo.xml (the single source the build scripts and tests read).
Default new id: io.github.<owner>.Leaf, with "-" in the owner turned into "_" (Flathub's rule for io.github ids).
The script renames every file under packaging/ whose name contains the old id and rewrites, in packaging/ and
.github/: the app id, the developer id (io.github.<owner>), and the owner/repo in github.com,
api.github.com/repos and raw.githubusercontent.com URLs. Run it once; running it again with another owner works the same way.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LINUX = os.path.join(ROOT, "packaging", "linux")
SCAN = (os.path.join(ROOT, "packaging"), os.path.join(ROOT, ".github"))
TEXT_EXT = {".xml", ".desktop", ".yml", ".yaml", ".json", ".md", ".sh", ".ps1", ".py", ".spec", ".iss", ".txt", ""}
TEXT_NAMES = {"PKGBUILD", ".SRCINFO"}


def current() -> tuple[str, str, str, str]:
    """(app id, developer id, owner, repo) as found in the metainfo file."""
    files = [f for f in os.listdir(LINUX) if f.endswith(".metainfo.xml")]
    if len(files) != 1:
        sys.exit(f"expected exactly one *.metainfo.xml in {LINUX}, found {files}")
    app_id = files[0][: -len(".metainfo.xml")]
    text = open(os.path.join(LINUX, files[0]), encoding="utf-8").read()
    dev = re.search(r'<developer id="([^"]+)"', text)
    home = re.search(r'<url type="homepage">https://github\.com/([^/<]+)/([^/<]+)</url>', text)
    if not (dev and home):
        sys.exit("metainfo has no <developer id> or GitHub homepage URL")
    return app_id, dev.group(1), home.group(1), home.group(2)


def default_id(owner: str) -> str:
    part = owner.replace("-", "_")
    if part[:1].isdigit():
        part = "_" + part
    return f"io.github.{part}.Leaf"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("owner", help="GitHub user or organization that hosts the repository")
    ap.add_argument("--repo", default=None, help="repository name (default: keep the current one)")
    ap.add_argument("--app-id", default=None, help="app id to use instead of io.github.<owner>.Leaf")
    ap.add_argument("--dry-run", action="store_true", help="only print what would change")
    args = ap.parse_args(argv)

    old_id, old_dev, old_owner, old_repo = current()
    new_id = args.app_id or default_id(args.owner)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){2,}", new_id):
        sys.exit(f"not a valid reverse-DNS app id: {new_id}")
    new_dev = "io.github." + args.owner.lower().replace("-", "_")
    new_repo = args.repo or old_repo
    subs = [
        (old_id, new_id),
        (f'<developer id="{old_dev}">', f'<developer id="{new_dev}">'),
        (f"<name>{old_owner}</name>", f"<name>{args.owner}</name>"),
        # github.com/<o>/<r>, raw.githubusercontent.com/<o>/<r>, api.github.com/repos/<o>/<r>
        (f"/{old_owner}/{old_repo}", f"/{args.owner}/{new_repo}"),
    ]
    changed = 0
    for base in SCAN:
        for dirpath, _dirs, names in os.walk(base):
            for name in names:
                path = os.path.join(dirpath, name)
                if os.path.splitext(name)[1] not in TEXT_EXT and name not in TEXT_NAMES:
                    continue
                if os.path.abspath(path) == os.path.abspath(__file__):
                    continue
                try:
                    text = open(path, encoding="utf-8").read()
                except (UnicodeDecodeError, OSError):
                    continue
                new = text
                for a, b in subs:
                    new = new.replace(a, b)
                if new != text:
                    changed += 1
                    print("rewrite", os.path.relpath(path, ROOT))
                    if not args.dry_run:
                        with open(path, "w", encoding="utf-8", newline="\n") as f:
                            f.write(new)
    for base in SCAN:
        for dirpath, _dirs, names in os.walk(base):
            for name in names:
                if old_id in name:
                    src = os.path.join(dirpath, name)
                    dst = os.path.join(dirpath, name.replace(old_id, new_id))
                    print("rename ", os.path.relpath(src, ROOT), "->", os.path.basename(dst))
                    if not args.dry_run:
                        os.replace(src, dst)
    print(f"{old_id} -> {new_id}: {changed} file(s) rewritten" + (" (dry run)" if args.dry_run else ""))
    print("Also update the app id that leaf/__main__.py passes to setDesktopFileName() (Wayland window matching).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
