"""Regenerate the demo documents and every store screenshot (both themes, both sizes), one app at a time.

    .venv/Scripts/python marketing/shots/shoot_all.py [--only 1,3] [--themes dark,light] [--sizes 2560x1600,1920x1080]

Output: marketing/out/screens/{dark,light}/NN-scene-WxH.png (see marketing/out/screens/README.md).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser()
ap.add_argument("--only", default="")
ap.add_argument("--themes", default="dark,light")
ap.add_argument("--sizes", default="2560x1600,1920x1080")
ap.add_argument("--skip-docs", action="store_true")
args = ap.parse_args()

env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
if not args.skip_docs:
    subprocess.run([sys.executable, os.path.join(HERE, "make_docs.py")], check=True, env=env)
failed = []
for theme in args.themes.split(","):
    for size in args.sizes.split(","):
        cmd = [sys.executable, os.path.join(HERE, "scenes.py"), "--theme", theme, "--size", size]
        if args.only:
            cmd += ["--only", args.only]
        print(">>", " ".join(cmd[2:]), flush=True)
        if subprocess.run(cmd, env=env).returncode != 0:
            failed.append(f"{theme} {size}")
print("failed runs:", failed if failed else "none")
sys.exit(1 if failed else 0)
