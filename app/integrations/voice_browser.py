"""Ordinary visible DOM automation. No private API, login or security bypass.

The live DOM contract must be reviewed against a dedicated project account.
The supplied contract is synthetic ONLY, and cannot activate live browsing.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

from app.integrations.test_sessions import parse_sessions
from app.sms.mac_provider import demo_phones


class BrowserBlocked(RuntimeError):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class Bubble:
    id: str
    direction: str
    body: str
    created_at: datetime


class VoiceConfig:
    def __init__(self, data, *, fixture=False):
        self.data = data
        self.fixture = fixture
        self.account = data.get("account_label", "")
        self.line = data.get("receiving_number", "")
        if not self.account or demo_phones(self.line) != {self.line}:
            raise ValueError("Exact dedicated account label and receiving number required")
        if data.get("project_only_account") is not True:
            raise ValueError("Only a dedicated project-only account is supported")
        self.bindings = data.get("bindings", {})
        self.sessions = parse_sessions(data.get("test_sessions"), self.bindings)
        if not self.bindings or set(self.sessions) != set(self.bindings):
            raise ValueError("Every exact recipient binding requires an explicit test session")
        demo_phones(",".join(self.bindings))
        for phone, spec in self.bindings.items():
            if not isinstance(spec, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", spec.get("thread_id", "")):
                raise ValueError("Bind recipient to an exact direct thread ID")
            url = urlsplit(spec.get("url", ""))
            if fixture:
                if url.scheme != "file" or url.netloc:
                    raise ValueError("Fixture URLs must be local files")
            elif (url.scheme != "https" or url.netloc != "voice.google.com" or
                  not re.fullmatch(r"/u/[0-9]+/messages/" + re.escape(spec["thread_id"]), url.path) or
                  url.query or url.fragment):
                raise ValueError("Live URLs must bind one exact Google Voice message thread")
        self.dom = data.get("dom", {})
        required = {"account", "line", "thread", "recipient", "rows", "body", "composer", "send", "login", "challenge", "message_id_attr", "direction_attr", "timestamp_attr"}
        if not required <= self.dom.keys() or not all(isinstance(self.dom[k], str) and self.dom[k] for k in required):
            raise ValueError("A complete reviewed DOM contract is required")
        if not fixture and data.get("live_dom_reviewed") is not True:
            raise ValueError("Live DOM is unreviewed; use synthetic fixtures until human setup")

    def active(self, phone, now=None):
        return self.sessions[phone].active(now or datetime.now(timezone.utc))

    def identity(self):
        return {k: self.data[k] for k in ("account_label", "receiving_number", "bindings", "test_sessions", "dom", "project_only_account")} | {"fixture": self.fixture}


class VoiceBrowser:
    def __init__(self, page, config):
        self.page, self.config = page, config
        page.set_default_timeout(5000)

    def exact_text(self, selector, text):
        node = self.page.locator(selector)
        return node.count() == 1 and node.inner_text().strip() == text

    def health(self, phone):
        d, c = self.config.dom, self.config
        if self.page.locator(d["challenge"]).count():
            return "blocked"
        if self.page.locator(d["login"]).count() or urlsplit(self.page.url).hostname == "accounts.google.com":
            return "login_required"
        if self.page.url != c.bindings[phone]["url"]:
            return "blocked"
        # Check metadata before accessing any message body. Never scan other threads.
        if not self.exact_text(d["account"], c.account) or not self.exact_text(d["line"], c.line):
            return "blocked"
        if (not self.exact_text(d["thread"], c.bindings[phone]["thread_id"]) or
                not self.exact_text(d["recipient"], phone)):
            return "blocked"
        if self.page.locator(d["composer"]).count() != 1 or self.page.locator(d["send"]).count() != 1:
            return "dom_changed"
        return "ready"

    def require_ready(self, phone):
        status = self.health(phone)
        if status != "ready":
            raise BrowserBlocked(status)

    def sync_thread(self, phone):
        if self.page.url != self.config.bindings[phone]["url"]:
            self.page.goto(self.config.bindings[phone]["url"], wait_until="domcontentloaded")
        return self.read_current(phone)

    def read_current(self, phone):
        self.require_ready(phone)
        d = self.config.dom
        rows = self.page.locator(d["rows"])
        result, ids = [], set()
        for i in range(rows.count()):
            row = rows.nth(i)
            identity = row.get_attribute(d["message_id_attr"])
            direction = row.get_attribute(d["direction_attr"])
            if not identity or identity in ids or direction not in {"in", "out"}:
                raise BrowserBlocked("dom_changed")
            try:
                created_at = datetime.fromisoformat(row.get_attribute(d["timestamp_attr"]))
                if created_at.tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise BrowserBlocked("dom_changed")
            ids.add(identity)
            body = row.locator(d["body"])
            if body.count() != 1:
                raise BrowserBlocked("dom_changed")
            result.append(Bubble(identity, direction, body.inner_text(), created_at))
        # Revalidate after reading in case the UI changed identity mid-read.
        self.require_ready(phone)
        return result

    def prepare(self, phone, body):
        self.require_ready(phone)
        self.page.locator(self.config.dom["composer"]).fill(body)
        self.require_ready(phone)

    def click_once(self, phone):
        self.require_ready(phone)
        # Disable Playwright's locator retries for this external side effect:
        # use one resolved handle; any error becomes unknown, never click again.
        handle = self.page.locator(self.config.dom["send"]).element_handle()
        if handle is None:
            raise BrowserBlocked("dom_changed")
        handle.click(timeout=5000)
