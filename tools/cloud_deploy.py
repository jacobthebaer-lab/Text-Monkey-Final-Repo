#!/usr/bin/env python3
"""Create/check private cloud setup without connecting accounts or starting services."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy/cloud/.env.example"
COMPOSE = ROOT / "deploy/cloud/compose.yaml"
SECRET_FIELDS = ("VOICE_API_TOKEN", "BACKEND_BRIDGE_KEY", "ADMIN_PASSWORD")
INPUT_FIELDS = frozenset({"VOICE_EXPECTED_EMAIL", "VOICE_EXPECTED_NUMBER", "ADMIN_EMAIL_ALLOWLIST",
    "SUPERADMIN_EMAIL_ALLOWLIST", "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "ADMIN_SITE_URL",
    "BACKEND_URL", "GLOO_API_KEY", "GLOO_ENDPOINT", "CHURCH_TIMEZONE", "CLOUD_BACKEND_PORT",
    "CLOUDFLARE_TUNNEL_CONFIG", "CLOUDFLARE_TUNNEL_CREDENTIALS"})
REQUIRED_CONFIG = ("ADMIN_EMAIL_ALLOWLIST", "SUPERADMIN_EMAIL_ALLOWLIST", "SUPABASE_URL",
                   "SUPABASE_PUBLISHABLE_KEY", "ADMIN_SITE_URL", "BACKEND_URL", "GLOO_API_KEY")
OFF_FLAGS = ("AUTOMATION_ENABLED", "LIVE_SMS", "GOOGLE_VOICE_ENABLED", "ALLOW_TEXT_SIGNUP",
             "PROFILE_SYNC_ENABLED", "PCO_STAFFING_WRITE_ENABLED", "PCO_STAFFING_POLL_ENABLED")
EMPTY_SCOPE = ("GOOGLE_VOICE_DEMO_PHONES", "GOOGLE_VOICE_TEST_SESSIONS")
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")
PHONE = re.compile(r"\+1[2-9][0-9]{9}\Z")


class SetupError(ValueError):
    """Messages contain field names/reasons only, never submitted values."""


def read_env(path):
    result = {}
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, UnicodeError):
        raise SetupError("Cannot read the selected environment file") from None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if not match:
            raise SetupError(f"Unsupported environment syntax on line {number}")
        key, value = match.groups()
        if key in result:
            raise SetupError(f"Duplicate environment field on line {number}")
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        elif any(character in value for character in "'\"$`\\"):
            raise SetupError(f"Use literal single-quoted values on line {number}")
        if "'" in value or any(ord(character) < 32 for character in value):
            raise SetupError(f"Unsupported environment value on line {number}")
        result[key] = value
    return result


def public_https(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password or
                parsed.query or parsed.fragment or parsed.path not in {"", "/"} or
                port not in {None, 443} or any(c.isspace() for c in value)):
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return bool(re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", host))
    except ValueError:
        return False


def publishable_key(value):
    if re.fullmatch(r"sb_publishable_[A-Za-z0-9_-]{20,}", value):
        return True
    try:
        parts = value.split(".")
        if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
            return False
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return isinstance(payload, dict) and payload.get("role") == "anon"
    except (ValueError, UnicodeError):
        return False


def validate(values):
    errors = []
    known = set(read_env(TEMPLATE))
    if set(values) - known:
        errors.append("Unknown environment fields are not accepted by this isolated setup")
    for key in OFF_FLAGS:
        if values.get(key) != "false":
            errors.append(f"{key} must be false during disconnected setup")
    if values.get("COMPETITION_CONFIRMATION_REQUIRED") != "true":
        errors.append("COMPETITION_CONFIRMATION_REQUIRED must be true")
    if values.get("GLOO_SIGNUP_REPLIES") != "true":
        errors.append("GLOO_SIGNUP_REPLIES must be true; cloud text requires Gloo")
    for key in EMPTY_SCOPE:
        if values.get(key, "").strip():
            errors.append(f"{key} must stay empty during disconnected setup")
    for key in SECRET_FIELDS:
        if len(values.get(key, "")) < 32:
            errors.append(f"{key} needs a separately generated secret of at least 32 characters")
    configured_secrets = [values[key] for key in SECRET_FIELDS if values.get(key)]
    if len(configured_secrets) != len(set(configured_secrets)):
        errors.append("VOICE_API_TOKEN, BACKEND_BRIDGE_KEY and ADMIN_PASSWORD must be different")
    for key in ("SUPABASE_URL", "ADMIN_SITE_URL", "BACKEND_URL"):
        if values.get(key) and not public_https(values[key]):
            errors.append(f"{key} must be a public HTTPS origin without credentials, path, query or fragment")
    if values.get("SUPABASE_PUBLISHABLE_KEY") and not publishable_key(values["SUPABASE_PUBLISHABLE_KEY"]):
        errors.append("SUPABASE_PUBLISHABLE_KEY must be publishable or legacy anon, never a secret/service-role key")
    admins = {v.strip().lower() for v in values.get("ADMIN_EMAIL_ALLOWLIST", "").split(",") if v.strip()}
    superadmins = {v.strip().lower() for v in values.get("SUPERADMIN_EMAIL_ALLOWLIST", "").split(",") if v.strip()}
    for key, emails in (("ADMIN_EMAIL_ALLOWLIST", admins), ("SUPERADMIN_EMAIL_ALLOWLIST", superadmins)):
        if any(not EMAIL.fullmatch(email) for email in emails):
            errors.append(f"{key} contains an invalid email address")
    if not superadmins <= admins:
        errors.append("SUPERADMIN_EMAIL_ALLOWLIST must be a subset of ADMIN_EMAIL_ALLOWLIST")
    if values.get("VOICE_EXPECTED_EMAIL") and not EMAIL.fullmatch(values["VOICE_EXPECTED_EMAIL"]):
        errors.append("VOICE_EXPECTED_EMAIL must contain one account email")
    if values.get("VOICE_EXPECTED_NUMBER") and not PHONE.fullmatch(values["VOICE_EXPECTED_NUMBER"]):
        errors.append("VOICE_EXPECTED_NUMBER must be the claimed +1 E.164 Google Voice number")
    if values.get("GLOO_ENDPOINT") not in {"guarded", "direct"}:
        errors.append("GLOO_ENDPOINT must be guarded or direct")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(values.get("CHURCH_TIMEZONE", ""))
    except (ValueError, KeyError):
        errors.append("CHURCH_TIMEZONE must be an installed IANA timezone")
    try:
        if not 1 <= int(values.get("GOOGLE_VOICE_MAX_QUEUE_AGE_SECONDS", "")) <= 3600:
            raise ValueError
    except ValueError:
        errors.append("GOOGLE_VOICE_MAX_QUEUE_AGE_SECONDS must be 1 through 3600")
    if "CLOUD_BACKEND_PORT" in values:
        try:
            if not 1024 <= int(values["CLOUD_BACKEND_PORT"]) <= 65535:
                raise ValueError
        except ValueError:
            errors.append("CLOUD_BACKEND_PORT must be an unprivileged port from 1024 through 65535")
    missing = [key for key in REQUIRED_CONFIG if not values.get(key, "").strip()]
    return errors, missing


def initialize(path, inputs=None):
    values = read_env(TEMPLATE)
    if inputs is not None:
        if not isinstance(inputs, dict) or set(inputs) - INPUT_FIELDS:
            raise SetupError("Inputs must be an object containing only documented setup fields")
        for key, value in inputs.items():
            if not isinstance(value, str) or "'" in value or any(ord(c) < 32 for c in value):
                raise SetupError("Setup inputs must be single-line literal strings without single quotes")
        values.update(inputs)
    values.update({key: secrets.token_urlsafe(48) for key in SECRET_FIELDS})
    errors, _ = validate(values)
    if errors:
        raise SetupError("; ".join(errors))
    contents = "# Private disconnected cloud setup. Never commit or share.\n"
    contents += "".join(f"{key}='{value}'\n" for key, value in values.items())
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise SetupError("Environment file already exists; it was not changed") from None
    except OSError:
        raise SetupError("Could not create the private environment file; check destination permissions") from None


def docker_checks(run=subprocess.run):
    result = {}
    commands = {
        "engine": ["docker", "info", "--format", "{{.ServerVersion}}"],
        "compose": ["docker", "compose", "version", "--short"],
        "compose_schema": ["docker", "compose", "-f", str(COMPOSE), "config",
                           "--no-env-resolution", "--no-interpolate", "--quiet"],
    }
    for name, command in commands.items():
        try:
            completed = run(command, capture_output=True, timeout=20, check=False)
            result[name] = "available" if completed.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            result[name] = "unavailable"
    return result


def check(path, *, skip_docker=False):
    errors = []
    try:
        mode = Path(path).lstat()
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
            errors.append("Environment file must be a regular private file with mode 0600")
        values = read_env(path)
        invalid, missing = validate(values)
        errors.extend(invalid)
    except (OSError, SetupError) as error:
        errors.append(str(error) if isinstance(error, SetupError) else "Cannot inspect the environment file")
        missing = list(REQUIRED_CONFIG)
    docker = {"status": "not_checked"} if skip_docker else docker_checks()
    return {"scaffold_valid": not errors, "configuration_complete": not errors and not missing,
            "runtime_verified": False, "delivery_enabled": False, "live_ready": False,
            "provider_policy_hold": True,
            "missing_config": missing, "errors": errors, "docker": docker,
            "next_steps": ["Complete missing configuration privately" if missing else
                           "Infrastructure configuration is complete; provider automation remains blocked",
                           "If Docker checks are unavailable, install/start Docker Engine with Compose v2 and recheck",
                           "Google Voice automation is blocked by provider policy, regardless of identity verification or flags",
                           "Gloo connectivity, authentication and real delivery are not checked",
                           "No account was connected and no service was started"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "check"))
    parser.add_argument("--env-file", type=Path, default=ROOT / "deploy/cloud/.env")
    parser.add_argument("--inputs-file", type=Path, help="Explicit private JSON inputs for init; never inherited from the shell")
    parser.add_argument("--skip-docker", action="store_true", help="Validate files only, without invoking Docker")
    parser.add_argument("--require-config", action="store_true", help="Return failure if infrastructure configuration is incomplete")
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            inputs = None
            if args.inputs_file:
                try:
                    inputs = json.loads(args.inputs_file.read_text())
                except (OSError, ValueError):
                    raise SetupError("Cannot read the selected private JSON inputs") from None
            initialize(args.env_file, inputs)
        elif args.inputs_file:
            raise SetupError("--inputs-file is accepted only for init")
        result = check(args.env_file, skip_docker=args.skip_docker or args.action == "init")
        print(json.dumps(result, indent=2))
        tools_ready = args.skip_docker or args.action == "init" or all(v == "available" for v in result["docker"].values())
        return 0 if result["scaffold_valid"] and tools_ready and (not args.require_config or result["configuration_complete"]) else 2
    except SetupError as error:
        print(json.dumps({"error": str(error)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
