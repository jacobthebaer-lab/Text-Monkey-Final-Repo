#!/usr/bin/env python3
"""Read-only demo checks; optionally start the credential-free loopback preview.

Never imports the backend, loads .env, reads Messages or opens a database.
Only presence checks and fixed status labels appear in the report.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    "index.html", "app.js", "domain.js", "setup.js", "setup-domain.js", "style.css",
    "accessibility.js", "admin-readiness.js", "planning-workflows.js",
    "onboarding-copy-nav.js", "onboarding-copy.js", "onboarding-copy.html",
    "onboarding-copy.css", "onboarding-copy-defaults.json",
)
GLOO_MODULES = (
    "fastapi", "sqlalchemy", "jinja2", "apscheduler", "openai", "dotenv",
    "httpx", "multipart", "uvicorn",
)


def inspect(mode, root=ROOT, environ=None):
    """Return only fixed labels; never serialize configuration values or paths."""
    environ = os.environ if environ is None else environ
    checks = {"python_3_11_or_newer": sys.version_info >= (3, 11)}
    if mode == "preview":
        checks["preview_launcher"] = (root / "tools/texty_local_demo.py").is_file()
        checks["public_assets"] = all(
            (root / "web/texty/public" / name).is_file() for name in PUBLIC_FILES
        )
        status = "ready" if all(checks.values()) else "blocked"
        evidence = "Browser-local fictional preview; Gloo and delivery are disconnected."
    else:
        checks["backend_dependencies"] = all(
            importlib.util.find_spec(name) is not None for name in GLOO_MODULES
        )
        checks["gloo_key_in_process_environment"] = bool(environ.get("GLOO_API_KEY", "").strip())
        if mode == "gloo":
            checks["signup_replay"] = (root / "tools/check_synthetic_gloo_signup.py").is_file()
            status = "configured_unverified" if all(checks.values()) else "blocked"
            evidence = "Configuration presence only; real Gloo calls and mock-delivery replay not run."
        else:
            checks["mac_connector_source"] = (root / "app/integrations/mac_messages.py").is_file()
            checks["private_worker_configuration_present"] = (root / ".mac-bridge.json").is_file()
            status = "requires_runtime_review"
            evidence = "No session, consent, selected line, scheduler or native delivery verification performed."
    return {
        "mode": mode,
        "status": status,
        "checks": checks,
        "optional_admin_status_replay_available": (root / "tools/demo_admin_status.py").is_file(),
        "evidence": evidence,
        "network_calls": 0,
        "database_writes": 0,
        "real_messages_sent": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preview", "gloo", "messages"), default="preview")
    parser.add_argument("--launch-preview", action="store_true", help="Start only the static loopback preview")
    parser.add_argument("--port", type=int, default=58123)
    args = parser.parse_args(argv)
    if args.launch_preview and args.mode != "preview":
        parser.error("--launch-preview requires --mode preview")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    report = inspect(args.mode)
    print(json.dumps(report, indent=2), flush=True)
    if report["status"] in ("blocked", "requires_runtime_review"):
        return 2
    if args.launch_preview:
        # No private configuration is inherited by the public-only child.
        child_env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        return subprocess.call(
            [sys.executable, str(ROOT / "tools/texty_local_demo.py"), "--host", "127.0.0.1", "--port", str(args.port)],
            cwd=ROOT, env=child_env,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
