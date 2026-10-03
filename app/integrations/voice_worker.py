"""Separate Google Voice worker. Default is read-only browser observation.

Backend protocol reuses /mac to retain existing SendGate, Gloo and history.
No API credentials, browser cookies or message bodies appear in health logs.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx

from app.integrations.test_sessions import STOP_WORDS
from app.integrations.voice_browser import BrowserBlocked, VoiceBrowser, VoiceConfig
from app.integrations.voice_journal import Journal


class Backend:
    def __init__(self, base, token, client=None):
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path or parsed.query or
                parsed.fragment or parsed.username or parsed.password or
                (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("Backend must be HTTPS origin or local HTTP origin")
        if len(token) < 32:
            raise ValueError("Backend bridge token must contain at least 32 characters")
        self.base, self.token = base, token
        self.client = client or httpx.Client(timeout=90, follow_redirects=False)

    def post(self, path, payload):
        response = self.client.post(self.base + path, json=payload, headers={"Authorization": "Bearer " + self.token,
                                                                          "X-TextMonkey-Transport": "google_voice"})
        response.raise_for_status()
        return response.json()

    def check_configuration(self, config):
        response = self.client.get(self.base + "/mac/transport", headers={"Authorization": "Bearer " + self.token,
                                                                        "X-TextMonkey-Transport": "google_voice"})
        response.raise_for_status()
        data = response.json()
        if (data.get("provider") != "google_voice" or data.get("human_confirmation_required") is not True or
                data.get("demo_mode") is not False or data.get("test_sessions") != config.data["test_sessions"]):
            raise ValueError("Backend transport, real clock, exact reviews or sessions do not match worker")

    def incoming(self, payload):
        return self.post("/mac/inbound", payload)

    def pull(self):
        return self.post("/mac/outbound/pull", {})["messages"]

    def verify(self, item):
        return self.post(f"/mac/outbound/{item['id']}/verify", {"token": item["token"], "content_hash": item["content_hash"]})

    def ack(self, item, outcome):
        return self.post(f"/mac/outbound/{item['id']}/ack", {"token": item["token"], "outcome": outcome})


class VoiceWorker:
    def __init__(self, config, browser, journal, backend=None, *, ingress=False, outbound=False, now=None):
        if outbound and not ingress:
            raise ValueError("Outbound requires ingress so STOP can be processed first")
        self.config, self.browser, self.journal, self.backend = config, browser, journal, backend
        self.ingress, self.outbound = ingress, outbound
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.health = {"status": "ready", "last_sync": None, "outbound_enabled": outbound}

    def payload(self, phone, bubble):
        selected = self.config.sessions[phone]
        if bubble.created_at < self.journal.cutoff(phone):
            return None, None
        text = bubble.body
        if text.strip().upper() in STOP_WORDS:
            body, keyword = text.strip(), "stop"
        elif selected.active(self.now()) and selected.starts_at <= bubble.created_at < selected.expires_at:
            # Identity was verified by the browser BEFORE body reads. In a
            # dedicated project-only direct thread, natural replies are test
            # input; provenance is added internally, never required from tester.
            body = text[len(selected.prefix):] if text.startswith(selected.prefix) else text
            keyword = "start" if body.strip().upper() in {"START", "UNSTOP"} else ("stop" if body.strip().upper() in STOP_WORDS else None)
        else:
            return None, None
        if not 0 < len(body.strip()) <= 1600:
            return None, None
        return {"guid": Journal.guid(self.config.identity(), phone, bubble.id), "phone": phone, "body": body,
                "service": "SMS", "session_id": selected.id}, keyword

    def sync(self):
        for phone in self.config.bindings:
            bubbles = self.browser.sync_thread(phone)
            # On first use skip all visible history; read-only baseline may not be
            # reused to enable ingress, see mode binding in main().
            if self.journal.baseline(phone, bubbles):
                continue
            self.record_bubbles(phone, bubbles)
        self.health["last_sync"] = self.now().isoformat()

    def record_bubbles(self, phone, bubbles):
        for bubble in bubbles:
            payload, keyword = self.payload(phone, bubble) if bubble.direction == "in" else (None, None)
            self.journal.ingest(phone, bubble, payload if self.ingress else None, keyword)

    def flush_inbox(self):
        for row in self.journal.pending_inbox():
            payload = json.loads(row["payload"])
            selected = self.config.sessions[payload["phone"]]
            # Expired input cannot suddenly enter the model after an outage.
            if not selected.active(self.now()) and payload["body"].strip().upper() not in STOP_WORDS:
                self.journal.delivered(row)
                continue
            self.backend.incoming(payload)  # server idempotency GUID survives response loss
            self.journal.delivered(row, start=payload["body"].strip().upper() in {"START", "UNSTOP"})

    def valid_item(self, item):
        phone = item.get("phone")
        if phone not in self.config.bindings or item.get("session_id") != self.config.sessions[phone].id:
            raise BrowserBlocked("blocked")
        if not self.config.active(phone, self.now()) or self.journal.suppressed(phone):
            raise BrowserBlocked("blocked")
        if (item.get("confirmation_required") is not True or not isinstance(item.get("content_hash"), str) or
                not isinstance(item.get("body"), str) or not 0 < len(item["body"].strip()) <= 1600):
            raise BrowserBlocked("blocked")
        try:
            expires = datetime.fromisoformat(item["approval_expires_at"])
            if expires.tzinfo is None or self.now() >= expires:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise BrowserBlocked("blocked")

    def reconcile(self, row):
        item = json.loads(row["item"])
        bubbles = self.browser.sync_thread(item["phone"])
        baseline = set(json.loads(row["baseline"] or "[]"))
        matches = [b for b in bubbles if b.direction == "out" and b.id not in baseline and b.body == item["body"]]
        if len(matches) == 1:
            self.journal.set_state(row["id"], "observed_sent", observed=matches[0].id)
            return "submitted"
        return "uncertain"

    def dispatch(self, row):
        item = json.loads(row["item"])
        if self.journal.unresolved(item["phone"]):
            raise BrowserBlocked("blocked")
        self.valid_item(item)
        before = self.browser.sync_thread(item["phone"])
        self.browser.prepare(item["phone"], item["body"])
        before = self.browser.read_current(item["phone"])
        self.record_bubbles(item["phone"], before)
        self.flush_inbox()
        # Preflight EVERY message with exact proof, immediately before one click.
        proof = self.backend.verify(item)
        if (proof.get("verified") is not True or proof.get("phone") != item["phone"] or
                proof.get("body") != item["body"] or proof.get("content_hash") != item["content_hash"]):
            raise BrowserBlocked("blocked")
        self.valid_item(item)
        self.browser.require_ready(item["phone"])
        self.journal.set_state(row["id"], "sending", baseline=[b.id for b in before])
        # Any failure from here (including hard process death) must NEVER retry.
        try:
            self.browser.click_once(item["phone"])
            for _ in range(3):
                bubbles = self.browser.read_current(item["phone"])
                matches = [b for b in bubbles if b.direction == "out" and b.id not in {x.id for x in before} and b.body == item["body"]]
                if len(matches) == 1:
                    self.journal.set_state(row["id"], "observed_sent", observed=matches[0].id)
                    return "submitted"
                if len(matches) > 1:
                    break
                time.sleep(0.25)
        except Exception:
            pass
        self.journal.set_state(row["id"], "unknown")
        return "uncertain"

    def once(self):
        try:
            self.sync()
            if not self.ingress:
                return
            self.flush_inbox()  # STOP commits before pulling outbound
            if not self.outbound:
                return
            # Observe uncertain operations first; never release new output to
            # the same recipient while any prior click is unresolved.
            for row in self.journal.operations():
                if row["state"] == "unknown":
                    self.reconcile(row)
            # A lost pull response
            # remains dispatching server-side; never infer or re-claim it.
            for item in self.backend.pull():
                self.valid_item(item)
                self.journal.queue_send(item)
            for row in self.journal.operations():
                if row["state"] == "failed":
                    continue
                if row["state"] == "queued":
                    try:
                        outcome = self.dispatch(row)
                    except BrowserBlocked:
                        self.journal.set_state(row["id"], "failed")
                        raise
                elif row["state"] == "unknown":
                    outcome = self.reconcile(row)
                else:
                    outcome = "submitted"
                if row["ack"] is None:
                    self.backend.ack(json.loads(row["item"]), outcome)
                    self.journal.acked(row["id"], outcome)
                # After an uncertain ACK, later browser evidence is local only;
                # backend reconciliation requires review (terminal ACK contract).
            self.health["status"] = "ready"
        except BrowserBlocked as error:
            self.health["status"] = error.status
            raise
        except Exception:
            self.health["status"] = "blocked"
            raise


def lock_file(stack, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = stack.enter_context(path.open("a"))
    os.chmod(path, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def main():
    parser = argparse.ArgumentParser(description="Text Monkey dedicated-account browser worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--enable-ingress", action="store_true")
    parser.add_argument("--live-delivery", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    data = json.loads(Path(args.config).read_text())
    config = VoiceConfig(data, fixture=args.fixture)
    profile = Path(data["profile_path"]).expanduser().resolve()
    state = Path(data["state_path"]).expanduser().resolve()
    if not args.fixture and not data.get("dedicated_profile_confirmed") is True:
        raise ValueError("Human must confirm a new dedicated project profile; no personal Chrome profiles")
    if args.live_delivery and data.get("live_delivery_approved") is not True:
        raise ValueError("Live delivery approval is missing")
    identity = config.identity() | {"ingress": args.enable_ingress, "profile": str(profile)}
    # Lock profile and state independently so neither can be shared by workers.
    with ExitStack() as stack:
        lock_file(stack, profile / ".textmonkey-worker.lock")
        lock_file(stack, state.with_suffix(".lock"))
        journal = Journal(state, identity)
        stack.callback(journal.db.close)
        backend = None
        if args.enable_ingress:
            token = os.environ.get("VOICE_BRIDGE_TOKEN", "")
            backend = Backend(data["backend_url"].rstrip("/"), token)
            stack.callback(backend.client.close)
            backend.check_configuration(config)
        from playwright.sync_api import sync_playwright
        playwright = stack.enter_context(sync_playwright())
        context = playwright.chromium.launch_persistent_context(str(profile), headless=False,
                    channel=data.get("browser_channel", "chromium"), accept_downloads=False)
        stack.callback(context.close)
        page = context.new_page()
        if args.fixture:
            page.route("http://**/*", lambda route: route.abort())
            page.route("https://**/*", lambda route: route.abort())
        browser = VoiceBrowser(page, config)
        worker = VoiceWorker(config, browser, journal, backend, ingress=args.enable_ingress, outbound=args.live_delivery)
        health_path = state.with_suffix(".health.json")
        while True:
            try:
                worker.once()
            except Exception:
                # No automatic resume from auth, DOM drift or policy rejection.
                # Fail process and leave all journal evidence for human review.
                health_path.write_text(json.dumps(worker.health))
                print("Browser worker paused: " + worker.health["status"] + "; human review required")
                return 2
            health_path.write_text(json.dumps(worker.health))
            if args.once:
                return 0
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
