# config.py — Load settings from .env

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")


def get_config() -> dict:
    token      = os.getenv("GITLAB_TOKEN", "").strip()
    url        = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    master_dir = os.getenv("MASTER_DIR", "MasterGroups").strip()

    if not token:
        raise EnvironmentError(
            "GITLAB_TOKEN is not set!\n"
            "  Check your .env file:\n\n"
            "  GITLAB_TOKEN=your-token-here\n"
            "  GITLAB_URL=https://gitlab.com\n"
            "  MASTER_DIR=MasterGroups\n"
        )

    return {
        "token":      token,
        "url":        url,
        "master_dir": master_dir,
        "headers":    {"PRIVATE-TOKEN": token},
    }
