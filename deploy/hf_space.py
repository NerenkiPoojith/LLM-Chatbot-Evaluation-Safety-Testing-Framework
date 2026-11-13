"""Publish the chatbot to a Hugging Face Space (Docker).

    hf auth login                                   # once, with a "Write" token
    python deploy/hf_space.py <username>/<space-name>

Uploads exactly the files tracked by git (never .env, .venv, caches or uploaded
manuals), with the Space settings added to the top of README.md. Hugging Face then
builds the Dockerfile and serves the app at https://<username>-<space-name>.hf.space
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parent.parent

SPACE_CARD = """---
title: Appliance Help Chatbot
emoji: 🔧
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: RAG chatbot over appliance manuals, with safety tests
---

"""


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [f for f in out.stdout.decode("utf-8").split("\0") if f]


def main() -> None:
    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        raise SystemExit("usage: python deploy/hf_space.py <username>/<space-name>")
    space_id = sys.argv[1]
    api = HfApi()
    print(f"Signed in to Hugging Face as {api.whoami()['name']}")

    api.create_repo(space_id, repo_type="space", space_sdk="docker", exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        for name in tracked_files():
            src = ROOT / name
            if src.is_file():
                dest = stage / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        readme = stage / "README.md"
        readme.write_text(SPACE_CARD + readme.read_text(encoding="utf-8"), encoding="utf-8")

        api.upload_folder(
            folder_path=str(stage),
            repo_id=space_id,
            repo_type="space",
            commit_message="Deploy from local repository",
            delete_patterns=["*"],  # mirror the repo: remove files deleted locally
        )

    user, name = space_id.split("/", 1)
    print(f"Uploaded. Build progress: https://huggingface.co/spaces/{space_id}")
    print(f"App URL (when built):    https://{user.lower()}-{name.lower().replace('_', '-')}.hf.space")


if __name__ == "__main__":
    main()
