"""Verify the hosted Text Monkey demo against an exact Pages upload.

Uses read-only HTTPS requests. No sign-in, mailbox access or texting calls.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEMO_URL = "https://text-monkey-demo.pages.dev/"
ASSETS = ("index.html", "app.js", "style.css", "domain.js", "setup.js", "setup-domain.js", "accessibility.js")


def checked_origin(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    allowed = host == "text-monkey-demo.pages.dev" or host.endswith(".text-monkey-demo.pages.dev")
    if (parsed.scheme != "https" or not allowed or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Use the Text Monkey Cloudflare Pages alias or deployment origin, without credentials or a path.")
    return url.rstrip("/") + "/"


def fetch(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(), response.geturl()


def verify(url, upload, fetcher=fetch):
    origin = checked_origin(url)
    upload = Path(upload)
    asset_names = sorted(set(ASSETS) | {p.relative_to(upload).as_posix() for p in upload.rglob("*.js")})
    expected = {name: (upload / name).read_bytes() for name in asset_names}
    expected_config = json.loads((upload / "api/config.json").read_text())
    if not expected_config.get("publicDemo") or any(expected_config.get(k) for k in ("connected", "aiReady", "liveSms")):
        raise ValueError("The upload is not the standalone public demo.")
    paths = (*expected, "api/config", "api/state")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = dict(zip(paths, pool.map(fetcher, (origin + path for path in paths))))
    for path, (status, headers, body, final_url) in responses.items():
        if urlsplit(final_url).netloc != urlsplit(origin).netloc:
            raise ValueError(f"{path}: redirected away from the requested Cloudflare origin.")
        if path == "api/state":
            if status != 404:
                raise ValueError(f"api/state: expected an unavailable backend (404), received {status}.")
            continue
        if status != 200:
            raise ValueError(f"{path}: HTTP {status}.")
        if path == "api/config":
            if json.loads(body) != expected_config:
                raise ValueError("api/config: hosted configuration differs from the upload.")
            if "no-store" not in headers.get("cache-control", ""):
                raise ValueError("api/config: missing no-store cache policy.")
        elif body != expected[path]:
            raise ValueError(f"{path}: live bytes differ from the tested upload. Publication is unverified.")
        if path == "index.html":
            if "connect-src 'self'" not in headers.get("content-security-policy", ""):
                raise ValueError("index.html: missing the expected content security policy.")
            if headers.get("x-content-type-options") != "nosniff":
                raise ValueError("index.html: missing nosniff protection.")
    return {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "production_url": origin,
        "asset_sha256": {name: hashlib.sha256(body).hexdigest() for name, body in expected.items()},
        "public_demo": True,
        "connected": False,
        "real_text_delivery": False,
        "backend_route_status": 404,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEMO_URL)
    parser.add_argument("--upload", required=True, type=Path, help="Exact directory uploaded to Pages")
    parser.add_argument("--receipt", type=Path, help="Optional JSON verification receipt")
    args = parser.parse_args()
    try:
        result = verify(args.url, args.upload)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Cloudflare verification failed: {error}\n")
    encoded = json.dumps(result, indent=2) + "\n"
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
