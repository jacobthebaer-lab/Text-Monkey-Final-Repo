"""Our own Mac Messages reader/sender. No BlueBubbles or private API injection.

The reader opens Apple's database read-only and fetches message content only
for explicitly selected, one-to-one iMessage phone conversations. It skips
existing history on first start. Live sending requires --live-delivery.
"""

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.sms.mac_provider import demo_phones

HERE = Path(__file__).resolve().parent


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as f:
        os.chmod(temp, 0o600)
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def decode_body(blob, helper):
    if not blob or len(blob) > 1_048_576:
        raise ValueError("Unsupported attributed message")
    result = subprocess.run([str(helper)], input=base64.b64encode(blob), capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("Attributed message could not be decoded; checkpoint was not advanced")
    return result.stdout.decode("utf-8")


class MessagesReader:
    def __init__(self, path, phones, helper):
        self.connection = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
        self.connection.execute("PRAGMA query_only=ON")
        self.phones = tuple(sorted(phones))
        self.helper = helper
        self.connection.row_factory = sqlite3.Row

    def watermark(self):
        return self.connection.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]

    def new_messages(self, after):
        placeholders = ",".join("?" for _ in self.phones)
        # No historical/personal content is fetched and then filtered in Python.
        rows = self.connection.execute(f"""
            SELECT DISTINCT m.ROWID AS row_id, m.guid, h.id AS phone,
                   m.text, m.attributedBody
            FROM message m
            JOIN handle h ON h.ROWID = m.handle_id
            JOIN chat_message_join cm ON cm.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cm.chat_id
            WHERE m.ROWID > ? AND m.is_from_me = 0 AND m.service = 'iMessage'
              AND c.service_name = 'iMessage' AND h.id IN ({placeholders})
              AND (SELECT COUNT(*) FROM chat_handle_join ch WHERE ch.chat_id = c.ROWID) = 1
            ORDER BY m.ROWID LIMIT 50
        """, (after, *self.phones)).fetchall()
        messages = []
        for row in rows:
            body = row["text"]
            if body is None and row["attributedBody"]:
                body = decode_body(row["attributedBody"], self.helper)
            # Attachments, reactions and unsupported events aren't guessed into text.
            if not body or not body.strip():
                messages.append({"row_id": row["row_id"], "skip": True})
                continue
            if len(body) > 1600:
                raise ValueError("Demo text exceeds 1,600 characters; checkpoint was not advanced")
            messages.append({"row_id": row["row_id"], "guid": row["guid"],
                             "phone": row["phone"], "body": body, "service": "iMessage"})
        return messages


def send_native(phone, body):
    result = subprocess.run(
        ["/usr/bin/osascript", str(HERE / "send_message.applescript"), phone, body],
        capture_output=True, timeout=30,
    )
    # A zero exit means Messages accepted the command, not carrier delivery.
    return "submitted" if result.returncode == 0 else "uncertain"


class MacWorker:
    def __init__(self, config, *, live=False, client=None, reader=None, sender=send_native):
        phones = config.get("phones", [])
        if not isinstance(phones, list) or not all(isinstance(p, str) for p in phones):
            raise ValueError("phones must be a list of exact international numbers")
        self.phones = demo_phones(",".join(phones))
        base = config.get("backend_url", "").rstrip("/")
        parsed = urlsplit(base)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path
                or parsed.scheme not in {"http", "https"} or not parsed.hostname):
            raise ValueError("backend_url must be an origin without credentials or path")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Remote backend connections require HTTPS")
        token = config.get("token", "")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Configure a Mac connector token of at least 32 characters")
        self.base = base
        self.client = client or httpx.Client(timeout=90, follow_redirects=False)
        self.headers = {"Authorization": "Bearer " + token, "ngrok-skip-browser-warning": "1"}
        self.live = live
        self.sender = sender
        self.state_path = Path(config.get("state_path", ".mac-state/checkpoint.json")).expanduser()
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if self.state and self.state.get("phones") != sorted(self.phones):
            raise ValueError("Demo numbers changed; use a fresh checkpoint to skip existing history")
        self.reader = reader or MessagesReader(
            config.get("messages_db", "~/Library/Messages/chat.db"), self.phones,
            Path(config.get("decoder", ".mac-state/decode-message")).expanduser().resolve(),
        )
        if not self.state:
            self.state = {"after": self.reader.watermark(), "phones": sorted(self.phones), "dispatches": {}}
            self.save()
        self.active_path = self.state_path.with_suffix(".active")

    def save(self):
        atomic_json(self.state_path, self.state)

    def post(self, path, data):
        response = self.client.post(self.base + path, json=data, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def once(self):
        for incoming in self.reader.new_messages(self.state["after"]):
            if not incoming.get("skip"):
                self.post("/mac/inbound", {k: v for k, v in incoming.items() if k != "row_id"})
            self.state["after"] = incoming["row_id"]
            self.save()  # only after server commit; a retry uses the same GUID
        if not self.live:
            return  # never claims or sends outbound messages in default mode
        # Recover a claim response persisted before a crash, without re-sending
        # any message that might have reached Messages already.
        batch = json.loads(self.active_path.read_text()) if self.active_path.exists() else None
        if batch is None:
            batch = self.post("/mac/outbound/pull", {})["messages"]
            atomic_json(self.active_path, batch)
        for item in batch:
            key = str(item["id"])
            entry = self.state["dispatches"].get(key)
            if entry and entry.get("token") != item["token"]:
                raise ValueError("Delivery claim changed unexpectedly")
            if entry:
                outcome = entry["outcome"]
                if outcome == "attempting":
                    outcome = "uncertain"
            else:
                if item["phone"] not in self.phones or not isinstance(item["body"], str) or not 0 < len(item["body"].strip()) <= 1600:
                    raise ValueError("Backend proposed an invalid or unapproved demo recipient")
                self.state["dispatches"][key] = {"token": item["token"], "outcome": "attempting"}
                self.save()  # durable before side effect
                try:
                    outcome = self.sender(item["phone"], item["body"])
                except Exception:
                    outcome = "uncertain"
                if outcome not in {"submitted", "uncertain"}:
                    outcome = "uncertain"
            self.state["dispatches"][key] = {"token": item["token"], "outcome": outcome}
            self.save()
            self.post(f"/mac/outbound/{item['id']}/ack", {"token": item["token"], "outcome": outcome})
        self.active_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Texty's first-party Mac Messages connector")
    parser.add_argument("--config", default=".mac-bridge.json")
    parser.add_argument("--live-delivery", action="store_true", help="Explicitly enable replies to configured demo numbers")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    # One Mac worker per checkpoint. Lock is held by the process and released
    # automatically on crash; checkpoint writes remain atomic.
    import fcntl
    lock_path = Path(config.get("state_path", ".mac-state/checkpoint.json")).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("w") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = MacWorker(config, live=args.live_delivery)
        print("Mac connector running; delivery " + ("enabled" if args.live_delivery else "disabled"))
        try:
            while True:
                try:
                    worker.once()
                except (httpx.HTTPError, ValueError):
                    # Don't print response bodies, message text or credentials.
                    print("Connector paused this cycle; check configuration/server status. No automatic resend.")
                if args.once:
                    break
                time.sleep(2)
        finally:
            worker.client.close()


if __name__ == "__main__":
    main()
