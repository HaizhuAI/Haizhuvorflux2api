import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("VGW_DB", str(BASE_DIR / "gateway.db"))
WEBUI_DIR = BASE_DIR / "webui"

# Upstream
VORFLUX_BASE = os.environ.get("VORFLUX_BASE", "https://us1.vorflux.com").rstrip("/")

# Auth
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
API_KEY = os.environ.get("API_KEY", "")  # master key for /v1 endpoints

# Pool behavior
GLOBAL_PROXY = os.environ.get("VORFLUX_PROXY", "")  # fallback proxy for all accounts
DEFAULT_MAX_CONCURRENT = int(os.environ.get("VGW_MAX_CONCURRENT", "3"))
RETRY_BUDGET = int(os.environ.get("VGW_RETRY_BUDGET", "2"))
CB_FAIL_THRESHOLD = int(os.environ.get("VGW_CB_FAILS", "3"))
CB_BASE_COOLDOWN = float(os.environ.get("VGW_CB_COOLDOWN", "60"))
CB_MAX_COOLDOWN = float(os.environ.get("VGW_CB_MAX_COOLDOWN", "900"))

# Turn waiting
POLL_INTERVAL = float(os.environ.get("VGW_POLL_INTERVAL", "1.6"))
POLL_FAST_INTERVAL = float(os.environ.get("VGW_POLL_FAST", "0.7"))  # early-turn cadence
POLL_FAST_POLLS = int(os.environ.get("VGW_POLL_FAST_POLLS", "8"))
MAX_TURN_WAIT = float(os.environ.get("VGW_MAX_TURN_WAIT", "600"))
IDLE_GRACE_POLLS = int(os.environ.get("VGW_IDLE_GRACE_POLLS", "2"))
STALL_TIMEOUT = float(os.environ.get("VGW_STALL_TIMEOUT", "90"))   # no new text -> failover
HTTP_TIMEOUT = float(os.environ.get("VGW_HTTP_TIMEOUT", "40"))

# Conversation affinity: prefix-hash -> upstream session continue
CONV_TTL = float(os.environ.get("VGW_CONV_TTL", "7200"))           # 2h idle expiry

# Hedged requests: spawn backup attempt after N ms (0 = off)
HEDGE_MS = int(os.environ.get("VGW_HEDGE_MS", "0"))

HOST = os.environ.get("VGW_HOST", "0.0.0.0")
PORT = int(os.environ.get("VGW_PORT", "8787"))
