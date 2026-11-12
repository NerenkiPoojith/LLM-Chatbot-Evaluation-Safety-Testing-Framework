"""Copy the public web pages into a folder for static hosting (Vercel, GitHub Pages, ...).

    python site/export_static.py [output_dir]      # default: public/

It publishes exactly the files the FastAPI app serves (app.main.PUBLIC_FILES), at the same
URLs, so the pages' relative links keep working. Nothing else in the project is exposed.
Static hosting has no API: the guide, brand kit and sample report work fully, and the chat
page shows its "server isn't running" banner. Run `uvicorn app.main:app` to chat.

Uses only the standard library (no project dependencies), so it runs in any build image.
"""

from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def public_files() -> dict[str, str]:
    """Read PUBLIC_FILES from app/main.py without importing it (that would need FastAPI etc.)."""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "PUBLIC_FILES":
            files: dict[str, str] = {}
            for key, value in zip(node.value.keys, node.value.values):
                files[ast.literal_eval(key)] = value.elts[0].value  # (path, media_type) -> path
            return files
    raise SystemExit("PUBLIC_FILES not found in app/main.py")


def main() -> None:
    out = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "public")
    if out.exists():
        shutil.rmtree(out)
    for url, source in public_files().items():
        if url == "/":
            continue  # served as /index.html by the static host
        src = ROOT / source
        if not src.is_file():
            raise SystemExit(f"missing {source} (for {url})")
        dest = out / url.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        print(f"{url:55} <- {source}")
    print(f"exported to {out}")


if __name__ == "__main__":
    main()
