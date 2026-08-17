from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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
