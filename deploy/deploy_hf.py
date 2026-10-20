"""Upload this folder to a private Hugging Face Space (Docker) and set its secrets.

    HF_TOKEN=hf_xxx HF_SPACE=ABDULHODIY/tg-autoreply python deploy/deploy_hf.py

Optional env (copied into the Space's secrets when present):
    ADMIN_PASSWORD, SECRET_KEY, PROXY_SECRET, STATE_REPO, ADMIN_USERNAME

PROXY_SECRET must be the same value as in the Vercel project: the Space then
answers API calls only when they come through the Vercel panel.
"""

import os
import secrets
import sys
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parent.parent

IGNORE = [
    ".git/**", ".github/**", ".env", ".env.*", "data/**", "sessions/**", "*.session", "*.string",
    "__pycache__/**", "**/__pycache__/**", ".pytest_cache/**", "tests/**",
    "space/**", "deploy/**", ".venv/**", "venv/**", "README.md",
]


def main() -> int:
    token = os.environ.get("HF_TOKEN", "").strip()
    space = os.environ.get("HF_SPACE", "").strip()
    if not token or not space:
        print("HF_TOKEN va HF_SPACE kerak", file=sys.stderr)
        return 1
    api = HfApi(token=token)
    owner = space.split("/")[0]

    api.create_repo(space, repo_type="space", space_sdk="docker", private=True, exist_ok=True)
    if not api.space_info(space).private:
        print("XAVFSIZLIK: Space ommaviy! Avval Settings'da private qiling.", file=sys.stderr)
        return 2

    # Secrets first, so the first build already starts with them.
    wanted = {
        "HF_TOKEN": token,
        "STATE_REPO": os.environ.get("STATE_REPO", "").strip() or f"{owner}/tg-autoreply-state",
        "ADMIN_USERNAME": os.environ.get("ADMIN_USERNAME", "").strip() or "abu",
    }
    for key in ("ADMIN_PASSWORD", "SECRET_KEY", "PROXY_SECRET"):
        if os.environ.get(key, "").strip():
            wanted[key] = os.environ[key].strip()
    try:
        existing = {s.key for s in api.get_space_secrets(space)}
    except Exception:  # noqa: BLE001 - older huggingface_hub
        existing = set()
    if "SECRET_KEY" not in wanted and "SECRET_KEY" not in existing:
        # Generated once; later deploys keep the existing one (changing it
        # would make the encrypted state backup unreadable).
        wanted["SECRET_KEY"] = secrets.token_urlsafe(48)
        print("SECRET_KEY yaratildi va Space secret sifatida saqlandi.")
    for key, value in wanted.items():
        api.add_space_secret(space, key, value)
    if "ADMIN_PASSWORD" not in wanted and "ADMIN_PASSWORD" not in existing:
        print("Eslatma: ADMIN_PASSWORD berilmadi - vaqtinchalik parol Space loglarida chiqadi.")

    api.upload_folder(repo_id=space, repo_type="space", folder_path=str(ROOT),
                      ignore_patterns=IGNORE, commit_message="deploy")
    api.upload_file(repo_id=space, repo_type="space",
                    path_or_fileobj=str(ROOT / "space" / "README.md"),
                    path_in_repo="README.md", commit_message="space config")
    print(f"Tayyor: https://huggingface.co/spaces/{space}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
