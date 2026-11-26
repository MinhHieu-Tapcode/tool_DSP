"""Configuration and constants for the gsolution-auto CLI tool."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STORAGE_STATE_PATH = BASE_DIR / "storage_state.json"
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"

GSOLUTION_BASE_URL = "https://g-solution.vn"
GSOLUTION_LOGIN_URL = f"{GSOLUTION_BASE_URL}/login"
GSOLUTION_ORDER_URL = f"{GSOLUTION_BASE_URL}/admin/order"
GSOLUTION_TELESALE_URL = f"{GSOLUTION_BASE_URL}/admin/order/telesale"
MARKET_COUNTRY_CODE = "PH"
COUNTRY_CALLING_CODE = "63"

ATTENDANCE_SPREADSHEET_ID = "1fiQPVzcAFJOZ-q1_cysh_MfGS7IZuXHjaaSiFRFO0W0"
ATTENDANCE_DEFAULT_GID = "820808572"

# Step 2: 3 POS Shops to check for Return Rate >= 50%
STEP2_POS_SHOPS: List[Tuple[str, str]] = [
    ("GIP 85", "GIP 85 - DH GLOBAL"),
    ("GIP 319", "GIP 319 - HỒNG SƠN"),
    ("GIP 244", "GIP 244 - XUÂN VŨ"),
]
STEP2_RETURN_RATE_THRESHOLD = 50.0  # inclusive: >= 50%

# Step 3 rules
DUPLICATE_WINDOW_DAYS = 30
MULTIPLE_RETURNED_THRESHOLD = 3  # >= 3 Returned orders
# GIPs whose empty orders receive products/notes later (never auto-cancel as empty DUPLICATE)
DEFERRED_FILL_GIPS = {"GIP 244", "GIP 300"}
# Cooldown before auto-canceling empty orders on other GIPs (avoids racing late webhooks)
EMPTY_ORDER_COOLDOWN_MINUTES = 15

# Saved filter & filter options on /admin/order/telesale
TELESALE_SAVED_FILTER = "FILL IN DATA"

# Fill-in telesales staff mapped to attendance sheet & G-solution accounts
FILLIN_STAFF: List[Dict[str, str]] = [
    {
        "code": "G238",
        "name": "Fresca May Mariano Dorado",
        "account": "Frescadorado",
        "aliases": "fresca,frescadorado,g238",
    },
    {
        "code": "G293",
        "name": "Angelica Agarin Salimbao",
        "account": "angie0987654321.gip@gmail.com",
        "aliases": "angelica,angie,angie0987654321,angie0987654321.gip@gmail.com,g293",
    },
]

# Order statuses
FINAL_STATUSES = {
    "Delivered",
    "Canceled",
    "Cancelled",
    "Returning",
    "Returned",
}

ACTIVE_STATUSES = {
    "New",
    "Need attention",
    "Restocking",
    "Need confirmation",
    "Pending",
    "Confirmed",
    "Wait Print",
    "Printed",
    "Packing",
    "Waiting for pick up",
    "Shipped",
}

# Tags & Cancel reasons under ORDER STATUS group
TAG_GROUP = "ORDER STATUS"
TAG_CALL_TO_CF = "CALL TO CF"
TAG_TEST = "TEST"
TAG_DUPLICATE = "DUPLICATE"
TAG_MULTIPLE_RETURNED = "MULTIPLE RETURNED"
TAG_DIFFERENT_PRODUCT = "DIFFERENT PRODUCT"
TAG_RE_ORDER = "RE ORDER"
TAG_NEED_PAY_ADVANCED = "NEED PAY ADVANCED"
TAG_MULTIPLE_ORDERS = "MULTIPLE ORDERS"

CANCEL_REASON_PATTERNS = {
    TAG_TEST: r"^Test",
    TAG_DUPLICATE: r"^Duplicate",
    TAG_MULTIPLE_RETURNED: r"^Multiple returned",
}

# GIP 287 (WeUpBook) dedicated check rules (independent from FILL IN DATA)
GIP287_SHOP_QUERY = "287"
GIP287_SHOP_LABEL = "408023150 - GIP 287"
GIP287_GIP_KEY = "GIP 287"
GIP287_RETURN_RATE_THRESHOLD = 60.0  # inclusive: >= 60%
GIP287_MIN_RETURNED_COUNT = 4  # strictly > 3 returned orders (from Return Rate tooltip)
GIP287_DUPLICATE_WINDOW_DAYS = 30
GIP287_FINAL_STATUSES = {"Delivered", "Returning", "Returned"}
GIP287_CANCELABLE_SMALL_STATUSES = {"New", "Need attention", "Need confirmation", "Pending"}


def load_dotenv(env_file: Path = ENV_PATH) -> None:
    """Load key=value pairs from a local .env file without overriding existing env vars."""
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def get_credentials() -> Tuple[str, str]:
    """Return G-solution login email and password from environment/.env."""
    load_dotenv()
    email = os.environ.get("GSOLUTION_EMAIL", "").strip()
    password = os.environ.get("GSOLUTION_PASSWORD", "").strip()
    if not email or not password:
        raise RuntimeError(
            "Thiếu GSOLUTION_EMAIL hoặc GSOLUTION_PASSWORD trong file .env hoặc biến môi trường."
        )
    return email, password


def ensure_runtime_dirs() -> None:
    """Create output report and log directories if missing."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
