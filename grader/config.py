"""Shared configuration for the grader service."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CODE_RUBRIC_DIR = BASE_DIR / "rubrics"
# The service ships a generic rubric set. A deployment can point at a
# read-only overlay (for custom rubrics/queries) without rebuilding the image.
RUBRIC_DIR = Path(os.environ.get("GRADER_RUBRIC_DIR", "").strip() or str(CODE_RUBRIC_DIR))
_overlay = os.environ.get("GRADER_RUBRIC_OVERLAY", "").strip()
RUBRIC_OVERLAY_DIR = Path(_overlay) if _overlay else None
GENERIC_GLOBAL_PATH = Path(os.environ.get("GRADER_RUBRIC_GENERIC_GLOBAL", "").strip() or str(BASE_DIR / "rubric_global_generic.md"))
_queries_file = os.environ.get("GRADER_QUERIES_FILE", "").strip()
QUERIES_PATH = Path(_queries_file) if _queries_file else None
_RUNTIME_DIR = os.environ.get("GRADER_DATA_DIR", "").strip()
DATA_DIR = Path(_RUNTIME_DIR) if _RUNTIME_DIR else BASE_DIR
CONFIG_PATH = Path(os.environ.get("GRADER_CONFIG", str(BASE_DIR / "config.json")))
DB_PATH = Path(os.environ.get("GRADER_DB", str(DATA_DIR / "grades.db")))
TOKEN_PATH = Path(os.environ.get("GRADER_TOKEN_FILE", str(DATA_DIR / "api_token")))
STATE_PATH = Path(os.environ.get("GRADER_STATE_FILE", str(DATA_DIR / "watcher_state.json")))
REPORTS_DIR = Path(os.environ.get("GRADER_REPORTS_DIR", str(DATA_DIR / "reports")))
BACKUP_DIR = Path(os.environ.get("GRADER_BACKUP_DIR", str(DATA_DIR / "backups")))
LDR_COOKIE_PATH = Path(os.environ.get("GRADER_LDR_COOKIE", str(DATA_DIR / "ldr_cookies")))

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 9131,
    "min_judges": 2,
    "max_judge_workers": 4,
    "max_job_attempts": 3,
    "judge_attempts": 4,
    "judge_retry_base_seconds": 1.0,
    "judge_retry_max_seconds": 8.0,
    "worker_threads": 2,
    "judge_failure_threshold": 3,
    "judge_cooldown_seconds": 300,
    "max_cost_per_grade_usd": 0.05,
    "max_cost_per_batch_usd": 1.00,
    "disagreement_overall": 1.0,
    "disagreement_criterion": 2.0,
    "ldr": {
        "enabled": False,
        "mode": "on",
        "base": "http://127.0.0.1:5000",
        "poll_seconds": 60,
        "baseline_existing": True,
    },
    "judges": [
        {
            "id": "primary",
            "base": "https://openrouter.ai/api/v1",
            "model": "deepseek/deepseek-v4-flash",
            "family": "deepseek",
            "env_key": "OPENROUTER_API_KEY",
            "timeout": 180,
            "max_tokens": 8000,
        },
        {
            "id": "secondary",
            "base": "https://openrouter.ai/api/v1",
            "model": "z-ai/glm-5.3-flash",
            "family": "z-ai",
            "env_key": "OPENROUTER_API_KEY",
            "timeout": 180,
            "max_tokens": 8000,
        },
    ],
    "adjudicator": {
        "id": "tiebreaker",
        "base": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-v4-pro",
        "family": "deepseek",
        "env_key": "OPENROUTER_API_KEY",
        "timeout": 300,
        "max_tokens": 16000,
    },
}


def load_env() -> None:
    """Optionally load a dotenv file explicitly named by GRADER_ENV_FILE.

    Container deployments inject environment variables through Compose. There
    are no implicit host-specific dotenv paths in the portable service.
    """
    raw = os.environ.get("GRADER_ENV_FILE", "").strip()
    if not raw:
        return
    p = Path(os.path.expanduser(raw))
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def load_config() -> dict:
    load_env()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = CONFIG_PATH
    if p.exists():
        try:
            user = json.loads(p.read_text())
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            raise RuntimeError(f"invalid config.json: {e}")
    # Environment overrides keep the image portable and let container tests
    # disable the watcher without editing a mounted config file.
    if os.environ.get("GRADER_HOST"):
        cfg["host"] = os.environ["GRADER_HOST"]
    if os.environ.get("GRADER_PORT"):
        cfg["port"] = int(os.environ["GRADER_PORT"])
    if os.environ.get("GRADER_WORKER_THREADS"):
        cfg["worker_threads"] = int(os.environ["GRADER_WORKER_THREADS"])
    if os.environ.get("GRADER_JUDGE_ATTEMPTS"):
        cfg["judge_attempts"] = max(1, int(os.environ["GRADER_JUDGE_ATTEMPTS"]))
    if os.environ.get("GRADER_JUDGE_RETRY_BASE_SECONDS"):
        cfg["judge_retry_base_seconds"] = max(0.0, float(os.environ["GRADER_JUDGE_RETRY_BASE_SECONDS"]))
    if os.environ.get("GRADER_JUDGE_RETRY_MAX_SECONDS"):
        cfg["judge_retry_max_seconds"] = max(1.0, float(os.environ["GRADER_JUDGE_RETRY_MAX_SECONDS"]))
    if os.environ.get("GRADER_LDR_BASE"):
        cfg["ldr"]["base"] = os.environ["GRADER_LDR_BASE"]
    if os.environ.get("GRADER_LDR_ENABLED"):
        cfg["ldr"]["enabled"] = os.environ["GRADER_LDR_ENABLED"].lower() not in ("0", "false", "no", "off")
    # Model/provider overrides are optional. The defaults keep the same models
    # on OpenRouter, but a model ID or gateway can be changed from .env.
    openrouter_base = os.environ.get("GRADER_OPENROUTER_BASE")
    if openrouter_base:
        for judge in cfg.get("judges", []):
            judge["base"] = openrouter_base
        cfg["adjudicator"]["base"] = openrouter_base
    model_overrides = {
        "primary": os.environ.get("GRADER_PRIMARY_MODEL"),
        "secondary": os.environ.get("GRADER_SECONDARY_MODEL"),
    }
    for judge in cfg.get("judges", []):
        if model_overrides.get(judge["id"]):
            judge["model"] = model_overrides[judge["id"]]
    if os.environ.get("GRADER_TIEBREAKER_MODEL"):
        cfg["adjudicator"]["model"] = os.environ["GRADER_TIEBREAKER_MODEL"]
    return cfg


def api_token() -> str:
    env = os.environ.get("GRADER_API_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    tok = secrets.token_urlsafe(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(tok + "\n")
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    return tok


def ldr_credentials() -> tuple[str, str]:
    user = os.environ.get("LDR_USER", "").strip()
    password = os.environ.get("LDR_PASS", "").strip()
    if user and password:
        return user, password
    raise RuntimeError("LDR credentials not found; set LDR_USER and LDR_PASS")


def query_map() -> dict[str, str]:
    if QUERIES_PATH is None or not QUERIES_PATH.exists():
        return {}
    return json.loads(QUERIES_PATH.read_text())


def _rubric_file(rubric_id: str) -> Path | None:
    name = "generic.md" if rubric_id == "generic_research_v1" else f"{rubric_id}.md"
    for d in (RUBRIC_OVERLAY_DIR, RUBRIC_DIR, CODE_RUBRIC_DIR):
        if d and (d / name).exists():
            return d / name
    return None


def _global_for(rubric_id: str) -> Path:
    """Return the neutral global policy, allowing an overlay to replace it."""
    for d in (RUBRIC_OVERLAY_DIR, RUBRIC_DIR, CODE_RUBRIC_DIR):
        if d:
            p = d / "rubric_global_generic.md"
            if p.exists():
                return p
    return GENERIC_GLOBAL_PATH


def rubric_version(rubric_id: str) -> str:
    p = _rubric_file(rubric_id)
    if not p:
        return "missing"
    global_p = _global_for(rubric_id)
    g = global_p.read_text() if global_p.exists() else ""
    h = hashlib.sha256((g + "\n" + p.read_text()).encode()).hexdigest()[:8]
    return f"2026-11-21.1-{h}"
