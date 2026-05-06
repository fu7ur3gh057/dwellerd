"""Filesystem and naming constants — single source of truth for the wizard.

Production install:
    /etc/dwellerd/config.yaml          root:dwellerd 640
    /var/lib/dwellerd/data/            dwellerd:dwellerd 750
    /var/lib/dwellerd/logs/            dwellerd:dwellerd 750
    /etc/systemd/system/dwellerd.service

Dev mode (no --install-service):
    <project>/config.yaml
    <project>/data/
    <project>/logs/
"""
from __future__ import annotations

from pathlib import Path

# deploy/scripts/wizard/paths.py → parents[3] = project root
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEV_CONFIG = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG = PROJECT_ROOT / "deploy" / "config.example.yaml"
CLIENT_DIR = PROJECT_ROOT / "client"

SERVICE_NAME = "dwellerd"
DWELLERD_USER = "dwellerd"

DWELLERD_HOME = Path("/var/lib/dwellerd")
DWELLERD_ETC = Path("/etc/dwellerd")
PROD_CONFIG = DWELLERD_ETC / "config.yaml"
PROD_DATA_DIR = DWELLERD_HOME / "data"
PROD_LOGS_DIR = DWELLERD_HOME / "logs"
PROD_DB_PATH = PROD_DATA_DIR / "dwellerd.sqlite"

DEV_DATA_DIR = PROJECT_ROOT / "data"
DEV_LOGS_DIR = PROJECT_ROOT / "logs"
DEV_DB_PATH = DEV_DATA_DIR / "dwellerd.sqlite"

UNIT_PATH = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")

BOT_SERVICE_NAME = f"{SERVICE_NAME}-bot"
BOT_UNIT_PATH = Path(f"/etc/systemd/system/{BOT_SERVICE_NAME}.service")

WEB_PREFIX = "/dwellerd"
