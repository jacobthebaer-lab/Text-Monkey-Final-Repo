"""Our own Mac Messages reader/sender. No BlueBubbles or private API injection.

The reader opens Apple's database read-only and fetches message content only
for explicitly selected, one-to-one phone conversations and services. It skips
existing history on first start. Live sending requires --live-delivery.
"""

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.sms.mac_provider import demo_phones, message_services
from app.integrations.test_sessions import parse_sessions, STOP_WORDS, permitted
from app.core.message_style import outbound_style_problem, validate_outbound_style

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


def checkpoint_diagnostic(config, *, now=None):
    """Read configuration and connector journals only; no DB/network/native IO."""
    now = now or datetime.now(timezone.utc)
    configured_phones = config.get("phones", [])
    if not isinstance(configured_phones, list) or not all(isinstance(p, str) for p in configured_phones):
        raise ValueError("phones must be a list of exact international numbers")
    phones = demo_phones(",".join(configured_phones))
    sessions = parse_sessions(config.get("test_sessions"), phones)
    if not config.get("receiving_number") or set(sessions) != set(phones):
        raise ValueError("Explicit receiving line and test sessions are required")
    if demo_phones(config["receiving_number"]) != frozenset({config["receiving_number"]}):
        raise ValueError("receiving_number must be one exact international number")
    if config.get("input_mode", "marked") not in {"marked", "natural"}:
        raise ValueError("input_mode must be marked or natural")
    services = sorted(message_services(",".join(config.get("services", ["iMessage"]))))
    path = Path(config.get("state_path", ".mac-state/checkpoint.json")).expanduser()
    state = json.loads(path.read_text()) if path.exists() else {}
    active_path = path.with_suffix(".active")
    active = json.loads(active_path.read_text()) if active_path.exists() else []
    if not isinstance(state, dict) or not isinstance(active, list) or not all(isinstance(item, dict) and "id" in item for item in active):
        raise ValueError("Invalid connector journal")
    dispatches = state.get("dispatches", {})
    if not isinstance(dispatches, dict) or not all(isinstance(entry, dict) for entry in dispatches.values()):
        raise ValueError("Invalid dispatch journal")
    session_spec = {p: {"id": selected.id, "starts_at": selected.starts_at.isoformat(),
                       "expires_at": selected.expires_at.isoformat()}
                    for p, selected in sessions.items()}
    active_count = sum(selected.active(now) for selected in sessions.values())
    expired_count = sum(now >= selected.expires_at for selected in sessions.values())
    matches = not state or (state.get("phones") == sorted(phones)
        and state.get("test_sessions") == session_spec
        and state.get("receiving_number") == config.get("receiving_number")
        and state.get("services", ["iMessage"]) == services
        and state.get("input_mode", "marked") == config.get("input_mode", "marked"))
    outcomes = [dispatches.get(str(item["id"]), {}).get("outcome") for item in active]
    return {"check": "local_journal_only", "backend_connectivity": "not_checked",
            "messages_connection": "not_checked", "checkpoint_exists": path.exists(),
            "checkpoint_matches_config": matches,
            "session_state": "active" if active_count else "expired" if expired_count == len(sessions) else "not_started",
            "active_sessions": active_count, "expired_sessions": expired_count,
            "claimed_items": len(active), "unattempted_claims": outcomes.count(None),
            "receipts_pending_ack": sum(o in {"submitted", "uncertain", "attempting"} for o in outcomes),
            "blocked_claims": outcomes.count("blocked"),
            "claim_response_uncertain": bool(state.get("claim_response_uncertain") or
                (state.get("claim_response_pending") and not active_path.exists()))}


def decode_body(blob, helper):
    if not blob or len(blob) > 1_048_576:
        raise ValueError("Unsupported attributed message")
    result = subprocess.run([str(helper)], input=base64.b64encode(blob), capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("Attributed message could not be decoded; checkpoint was not advanced")
    return result.stdout.decode("utf-8")


class MessagesReader:
    def __init__(self, path, phones, helper, receiving_number=None, services=("iMessage",)):
        self.services = tuple(sorted(message_services(",".join(services))))
        if "SMS" in self.services and not receiving_number:
            raise ValueError("SMS requires an exact selected receiving line")
        self.connection = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
        self.connection.execute("PRAGMA query_only=ON")
        self.phones = tuple(sorted(phones))
        self.helper = helper
        self.receiving_number = receiving_number
        self.connection.row_factory = sqlite3.Row

    def watermark(self):
        return self.connection.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]

    def new_messages(self, after):
        placeholders = ",".join("?" for _ in self.phones)
        service_placeholders = ",".join("?" for _ in self.services)
        receiving_filter = " AND m.destination_caller_id = ?" if self.receiving_number else ""
        # No historical/personal content is fetched and then filtered in Python.
        rows = self.connection.execute(f"""
            SELECT DISTINCT m.ROWID AS row_id, m.guid, h.id AS phone,
                   m.text, m.attributedBody, m.service
            FROM message m
            JOIN handle h ON h.ROWID = m.handle_id
            JOIN chat_message_join cm ON cm.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cm.chat_id
            WHERE m.ROWID > ? AND m.is_from_me = 0 AND m.service IN ({service_placeholders})
              AND c.service_name = m.service AND h.id IN ({placeholders})
              AND (SELECT COUNT(*) FROM chat_handle_join ch WHERE ch.chat_id = c.ROWID) = 1
              {receiving_filter}
            ORDER BY m.ROWID LIMIT 50
        """, (after, *self.services, *self.phones, *((self.receiving_number,) if self.receiving_number else ()))).fetchall()
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
                             "phone": row["phone"], "body": body, "service": row["service"]})
        return messages

    def outgoing_chat(self, phone):
        if phone not in self.phones or not self.receiving_number:
            raise ValueError("A selected receiving line is required for live chat delivery")
        service_placeholders = ",".join("?" for _ in self.services)
        rows = self.connection.execute(f"""
            SELECT c.guid FROM chat c
            JOIN chat_handle_join ch ON ch.chat_id = c.ROWID
            JOIN handle h ON h.ROWID = ch.handle_id
            WHERE h.id = ? AND c.service_name IN ({service_placeholders})
              AND c.last_addressed_handle = ?
              AND (SELECT COUNT(*) FROM chat_handle_join a WHERE a.chat_id = c.ROWID) = 1
        """, (phone, *self.services, self.receiving_number)).fetchall()
        if len(rows) != 1:
            raise ValueError("No unambiguous direct conversation on the selected sending line")
        return rows[0]["guid"]


def send_native(phone, body, chat_guid=None):
    validate_outbound_style(body)
    result = subprocess.run(
        ["/usr/bin/osascript", str(HERE / "send_message.applescript"), phone, body,
         *([chat_guid] if chat_guid else [])],
        capture_output=True, timeout=30,
    )
    # A zero exit means Messages accepted the command, not carrier delivery.
    return "submitted" if result.returncode == 0 else "uncertain"


class TestSessionMessagesReader(MessagesReader):
    """Fetch only marked test input or exact opt-out commands, inside SQL."""
    def __init__(self, path, phones, helper, receiving_number, services, test_sessions, now=None):
        self.test_sessions = parse_sessions(test_sessions, phones)
        if not receiving_number or set(self.test_sessions) != set(phones):
            raise ValueError("An exact receiving line and explicit test session for every phone are required")
        self.now = now or (lambda: datetime.now(timezone.utc))
        super().__init__(path, phones, helper, receiving_number, services)

    def new_messages(self, after):
        clauses, values = [], []
        now = self.now()
        for phone, selected in sorted(self.test_sessions.items()):
            command_placeholders = ",".join("?" for _ in STOP_WORDS)
            content = f"UPPER(TRIM(m.text)) IN ({command_placeholders})"
            args = [phone, *sorted(STOP_WORDS)]
            if selected.active(now):
                content += " OR substr(m.text, 1, length(?)) = ?"
                args.extend([selected.prefix, selected.prefix])
            clauses.append("(h.id = ? AND ("+content+"))")
            values.extend(args)
        service_placeholders = ",".join("?" for _ in self.services)
        rows = self.connection.execute(f"""
            SELECT DISTINCT m.ROWID AS row_id, m.guid, h.id AS phone, m.text, m.service
            FROM message m JOIN handle h ON h.ROWID=m.handle_id
            JOIN chat_message_join cm ON cm.message_id=m.ROWID
            JOIN chat c ON c.ROWID=cm.chat_id
            WHERE m.ROWID > ? AND m.is_from_me=0 AND m.service IN ({service_placeholders})
              AND c.service_name=m.service AND m.destination_caller_id=?
              AND c.last_addressed_handle=?
              AND (SELECT COUNT(*) FROM chat_handle_join ch WHERE ch.chat_id=c.ROWID)=1
              AND ({' OR '.join(clauses)})
            ORDER BY m.ROWID LIMIT 50
        """, (after, *self.services, self.receiving_number, self.receiving_number, *values)).fetchall()
        messages = []
        for row in rows:
            selected = self.test_sessions[row["phone"]]
            text = row["text"]
            body = text[len(selected.prefix):] if text.startswith(selected.prefix) else text.strip().upper()
            if not body.strip() or len(body) > 1600:
                raise ValueError("Marked test text must be nonempty and under 1,600 characters")
            messages.append({"row_id": row["row_id"], "guid": row["guid"], "phone": row["phone"],
                             "body": body, "service": row["service"], "session_id": selected.id})
        return messages


class NaturalTestSessionMessagesReader(TestSessionMessagesReader):
    """Normal replies on one explicitly selected route during its live session."""

    def new_messages(self, after):
        clauses, values = [], []
        now = self.now()
        epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
        for phone, selected in sorted(self.test_sessions.items()):
            stop_placeholders = ','.join('?' for _ in STOP_WORDS)
            content = f'UPPER(TRIM(m.text)) IN ({stop_placeholders})'
            args = [phone, *sorted(STOP_WORDS)]
            if selected.active(now):
                content += ' OR (m.date >= ? AND m.date < ?)'
                args.extend([int((selected.starts_at-epoch).total_seconds()*1_000_000_000),
                             int((selected.expires_at-epoch).total_seconds()*1_000_000_000)])
            clauses.append('(h.id = ? AND ('+content+'))')
            values.extend(args)
        service_placeholders = ','.join('?' for _ in self.services)
        rows = self.connection.execute(f'''
            SELECT DISTINCT m.ROWID AS row_id, m.guid, h.id AS phone,
                   m.text, m.attributedBody, m.service
            FROM message m JOIN handle h ON h.ROWID=m.handle_id
            JOIN chat_message_join cm ON cm.message_id=m.ROWID
            JOIN chat c ON c.ROWID=cm.chat_id
            WHERE m.ROWID > ? AND m.is_from_me=0 AND m.service IN ({service_placeholders})
              AND c.service_name=m.service AND m.destination_caller_id=?
              AND c.last_addressed_handle=?
              AND (SELECT COUNT(*) FROM chat_handle_join ch WHERE ch.chat_id=c.ROWID)=1
              AND ({' OR '.join(clauses)})
            ORDER BY m.ROWID LIMIT 50
        ''', (after, *self.services, self.receiving_number, self.receiving_number, *values)).fetchall()
        messages = []
        for row in rows:
            selected = self.test_sessions[row['phone']]
            body = row['text']
            if body is None and row['attributedBody']:
                body = decode_body(row['attributedBody'], self.helper)
            if not body or not body.strip():
                messages.append({'row_id': row['row_id'], 'skip': True})
                continue
            if body.startswith(selected.prefix):
                body = body[len(selected.prefix):]
            if not body.strip() or len(body) > 1600:
                raise ValueError('Test reply must be nonempty and under 1,600 characters')
            messages.append({'row_id': row['row_id'], 'guid': row['guid'],
                'phone': row['phone'], 'body': body, 'service': row['service'],
                'session_id': selected.id})
        return messages


class MacWorker:
    def __init__(self, config, *, live=False, client=None, reader=None, sender=send_native):
        phones = config.get("phones", [])
        if not isinstance(phones, list) or not all(isinstance(p, str) for p in phones):
            raise ValueError("phones must be a list of exact international numbers")
        self.phones = demo_phones(",".join(phones))
        services = config.get("services", ["iMessage"])
        if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
            raise ValueError("services must be a list containing iMessage or SMS")
        self.services = sorted(message_services(",".join(services)))
        receiving_number = config.get("receiving_number")
        if receiving_number:
            if demo_phones(receiving_number) != frozenset({receiving_number}):
                raise ValueError("receiving_number must be one exact international number")
        if "SMS" in self.services and not receiving_number:
            raise ValueError("SMS requires an exact selected receiving line")
        self.test_sessions = parse_sessions(config.get("test_sessions"), self.phones)
        self.input_mode = config.get('input_mode', 'marked')
        if self.input_mode not in {'marked', 'natural'}:
            raise ValueError('input_mode must be marked or natural')
        if not receiving_number or set(self.test_sessions) != set(self.phones):
            raise ValueError("An exact receiving line and explicit test session for every phone are required")
        session_checkpoint = {p: {"id": s.id, "starts_at": s.starts_at.isoformat(), "expires_at": s.expires_at.isoformat()}
                              for p, s in self.test_sessions.items()}
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
        self.confirmation_required = config.get("competition_confirmation_required", False)
        if not isinstance(self.confirmation_required, bool):
            raise ValueError("competition_confirmation_required must be boolean")
        self.sender = sender
        self.state_path = Path(config.get("state_path", ".mac-state/checkpoint.json")).expanduser()
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if self.state and self.state.get('input_mode', 'marked') != self.input_mode:
            raise ValueError('Input mode changed; use a fresh checkpoint to skip existing history')
        if self.state and self.state.get("phones") != sorted(self.phones):
            raise ValueError("Demo numbers changed; use a fresh checkpoint to skip existing history")
        if self.state and self.state.get("services", ["iMessage"]) != self.services:
            raise ValueError("Message services changed; use a fresh checkpoint to skip existing history")
        if self.state and self.state.get("test_sessions") != session_checkpoint:
            raise ValueError("Test sessions changed; use a fresh checkpoint to skip existing history")
        reader_type = NaturalTestSessionMessagesReader if self.input_mode == 'natural' else TestSessionMessagesReader
        self.reader = reader or reader_type(
            config.get("messages_db", "~/Library/Messages/chat.db"), self.phones,
            Path(config.get("decoder", ".mac-state/decode-message")).expanduser().resolve(),
            receiving_number, self.services, config.get("test_sessions"),
        )
        if receiving_number and sender is send_native:
            self.sender = lambda phone, body: send_native(phone, body, self.reader.outgoing_chat(phone))
        if self.state and self.state.get("receiving_number") != receiving_number:
            raise ValueError("Receiving line changed; use a fresh checkpoint")
        if not self.state:
            self.state = {"after": self.reader.watermark(), "phones": sorted(self.phones),
                          "receiving_number": receiving_number, "services": self.services,
                          "input_mode": self.input_mode,
                          "test_sessions": session_checkpoint, "dispatches": {}}
            self.save()
        self.active_path = self.state_path.with_suffix(".active")

    def save(self):
        atomic_json(self.state_path, self.state)

    def post(self, path, data):
        response = self.client.post(self.base + path, json=data, headers=self.headers)
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as error:
            raise httpx.RemoteProtocolError("Backend response is not valid JSON; checkpoint preserved") from error
        if not isinstance(result, dict):
            raise httpx.RemoteProtocolError("Backend response has an unexpected shape; checkpoint preserved")
        return result

    def preflight(self, item, *, exact=False):
        try:
            return self.post(f"/mac/outbound/{item['id']}/verify", {
                "token":item["token"], **({"content_hash":item["content_hash"]} if exact else {})})
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 409:
                raise
            # The server rejected this claim before any native attempt. Keep
            # its durable ID blocked and allow newly reviewed IDs to be pulled.
            # No submitted/uncertain acknowledgment or resend is invented.
            self.state["dispatches"][str(item["id"])] = {"token":item["token"], "outcome":"blocked"}
            self.save()
            return None

    def once(self):
        for incoming in self.reader.new_messages(self.state["after"]):
            if not incoming.get("skip"):
                if not permitted(self.test_sessions.get(incoming.get("phone")), incoming.get("session_id", ""),
                                 incoming.get("body", ""), datetime.now(timezone.utc)):
                    raise ValueError("Incoming text has no active, matching test-session proof")
                result = self.post("/mac/inbound", {k: v for k, v in incoming.items() if k != "row_id"})
                if result.get("progress_key"):
                    self.state["progress_enabled"] = True
            self.state["after"] = incoming["row_id"]
            self.save()  # only after server commit; a retry uses the same GUID
            if self.live and self.state.get("progress_enabled"):
                self.dispatch_outbound()  # submit the quick reply before draining another input
                self.post("/mac/progress/tick", {})
        if not self.live:
            return  # never claims or sends outbound messages in default mode
        self.dispatch_outbound()
        if self.state.get("progress_enabled"):
            self.post("/mac/progress/tick", {})

    def dispatch_outbound(self):
        # Recover a claim response persisted before a crash, without re-sending
        # any message that might have reached Messages already.
        batch = json.loads(self.active_path.read_text()) if self.active_path.exists() else None
        if batch is None:
            if self.state.get("claim_response_pending"):
                self.state["claim_response_uncertain"] = True
            self.state["claim_response_pending"] = True
            self.save()
            try:
                response = self.post("/mac/outbound/pull", {})
                if "messages" not in response:
                    raise httpx.RemoteProtocolError("Backend claim list is missing; checkpoint preserved")
                batch = response["messages"]
                if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
                    raise httpx.RemoteProtocolError("Backend claim list is invalid; checkpoint preserved")
            except httpx.HTTPError:
                # The server may have committed a claim before its response was lost.
                # Never invent a token, replay its native attempt, or silently clear this warning.
                self.state["claim_response_uncertain"] = True
                self.save()
                raise
            atomic_json(self.active_path, batch)
            self.state.pop("claim_response_pending", None)
            self.save()
        if self.state.pop("claim_response_pending", None):
            self.save()  # a durable .active response resolves a crash before this flag was cleared
        for item in batch:
            key = str(item["id"])
            entry = self.state["dispatches"].get(key)
            if entry and entry.get("token") != item["token"]:
                raise ValueError("Delivery claim changed unexpectedly")
            if entry:
                outcome = entry["outcome"]
                if outcome == "blocked":
                    continue
                if outcome == "attempting":
                    outcome = "uncertain"
            else:
                if not permitted(self.test_sessions.get(item.get("phone")), item.get("session_id", ""),
                                 "", datetime.now(timezone.utc)):
                    raise ValueError("Outbound text has no active, matching test-session proof")
                if item["phone"] not in self.phones or not isinstance(item["body"], str) or not 0 < len(item["body"].strip()) <= 1600:
                    raise ValueError("Backend proposed an invalid or unapproved demo recipient")
                if problem := outbound_style_problem(item["body"]):
                    self.state["dispatches"][key] = {"token": item["token"], "outcome": "blocked", "reason": problem}
                    self.save()
                    continue
                if self.confirmation_required or item.get("confirmation_required"):
                    if item.get("confirmation_required") is not True or not isinstance(item.get("content_hash"), str):
                        raise ValueError("Native delivery requires exact human confirmation")
                    expires = datetime.fromisoformat(item.get("approval_expires_at", ""))
                    if expires.tzinfo is None or datetime.now(timezone.utc) >= expires:
                        raise ValueError("Human confirmation expired before native delivery")
                    proof = self.preflight(item, exact=True)
                    if proof is None:
                        continue
                    if (proof.get("verified") is not True or proof.get("phone") != item["phone"] or proof.get("body") != item["body"] or proof.get("content_hash") != item["content_hash"]):
                        raise ValueError("Human-approved recipient or body changed before native delivery")
                elif item.get("offer_preflight_required") or item.get("conversation_preflight_required"):
                    proof = self.preflight(item)
                    if proof is None:
                        continue
                    if (proof.get("verified") is not True or proof.get("phone") != item["phone"] or
                            not isinstance(proof.get("body"), str) or not 0 < len(proof["body"].strip()) <= 1600):
                        raise ValueError("Outbound conversation preflight failed")
                    item["body"] = proof["body"]
                if problem := outbound_style_problem(item["body"]):
                    self.state["dispatches"][key] = {"token": item["token"], "outcome": "blocked", "reason": problem}
                    self.save()
                    continue
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
    parser = argparse.ArgumentParser(description="Text Monkey's first-party Mac Messages connector")
    parser.add_argument("--config", default=".mac-bridge.json")
    parser.add_argument("--live-delivery", action="store_true", help="Explicitly enable replies to configured demo numbers")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--diagnose", action="store_true", help="Report local sessions/journals without opening Messages or contacting the backend")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if args.diagnose:
        try:
            report = checkpoint_diagnostic(config)
        except (ValueError, TypeError, KeyError, OSError):
            print("Connector diagnostic could not read valid configuration/journals. No connections were attempted.")
            return 2
        print(json.dumps(report, sort_keys=True))
        return 0 if report["checkpoint_matches_config"] else 2
    # One Mac worker per checkpoint. Lock is held by the process and released
    # automatically on crash; checkpoint writes remain atomic.
    import fcntl
    lock_path = Path(config.get("state_path", ".mac-state/checkpoint.json")).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("w") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = MacWorker(config, live=args.live_delivery)
        print("Mac connector running; native delivery " + ("requested (active test sessions and preflight required)" if args.live_delivery else "disabled"))
        failure_delay = 2
        offline = False
        exit_code = 0
        try:
            while True:
                try:
                    worker.once()
                    if offline:
                        print("Backend connection restored; saved checkpoint resumed without repeating native attempts.")
                    offline = False
                    failure_delay = 2
                except httpx.HTTPStatusError as error:
                    if error.response.status_code not in {429, 500, 502, 503, 504}:
                        print("Backend rejected a message; connector stopped with checkpoint preserved. Repair and resume manually.")
                        exit_code = 2
                        break
                    offline = True
                    failure_delay = min(60, failure_delay * 2)
                    print("Backend temporarily unavailable; checkpoint preserved.")
                except ValueError:
                    print("Invalid message, expired session or configuration; connector stopped with checkpoint preserved.")
                    exit_code = 2
                    break
                except httpx.HTTPError:
                    # Don't print response bodies, message text or credentials.
                    offline = True
                    print("Backend offline or response unreadable; retrying from saved receipts. Native attempts will not be repeated.")
                    failure_delay = min(60, failure_delay * 2)
                if args.once:
                    if offline:
                        exit_code = 1
                    break
                time.sleep(failure_delay)
        finally:
            worker.client.close()
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
