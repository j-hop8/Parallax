from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from .env into the environment, never overriding.

    Kept to a dozen lines rather than a dependency: the only secret this
    project holds is one API key, and every job -- launchd crawl included --
    must pick it up without a wrapper script. A variable already exported in
    the shell wins, so `STANCE_RPM=10 make stance` behaves as expected.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
RAW_DIR = ROOT / "raw"
LOG_DIR = ROOT / "logs"
EVAL_DIR = ROOT / "eval"
OUTLETS_YAML = CONFIG_DIR / "outlets.yaml"
USERDICT = CONFIG_DIR / "userdict.txt"
# Traditional-Chinese dictionary. Not shipped in jieba's wheel; `make dict` fetches it.
BIG_DICT = CONFIG_DIR / "dict.txt.big"

# Defaults to the docker-compose service. Port 5433 on the host, so a Postgres
# installed natively later cannot collide with it.
DATABASE_URL = os.environ.get(
    "PARALLAX_DATABASE_URL",
    "postgresql://parallax:parallax@localhost:5433/parallax",
)

# Every day-bucket in the system is Taipei local. Bucketing by UTC would push
# everything published after 08:00 local into the wrong day and quietly corrupt
# the coverage-weight denominator.
TIMEZONE = "Asia/Taipei"

# Coverage weight is only meaningful once an outlet's daily denominator is large
# enough for the ratio to be stable. Below this, the UI suppresses the number
# rather than showing a noisy one.
MIN_DAILY_DENOMINATOR = 20

# Q1 stance classifier (T-007). Free-tier quotas are per model and per day and
# no longer published; measured 2026-09-13: gemini-3.8-flash allows 20
# requests/day (useless for a 184-article keyword), gemini-3.5-flash-lite ran
# 184 at 15/min with no 429. The client paces itself to STANCE_RPM and backs
# off on 429; a per-day quota stops the run cleanly. Check your own limits at
# https://aistudio.google.com/rate-limit before raising either value.
STANCE_MODEL = os.environ.get("STANCE_MODEL", "gemini-3.5-flash-lite")
STANCE_RPM = float(os.environ.get("STANCE_RPM", "10"))
