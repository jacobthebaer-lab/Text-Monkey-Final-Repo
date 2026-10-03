#!/usr/bin/env bash
# The workflow builds the actual deployment images. Local checks may reuse them.
set -euo pipefail

backend_image="${CLOUD_PROOF_BACKEND_IMAGE:-text-monkey-cloud-proof-backend:local}"
voice_image="${CLOUD_PROOF_VOICE_IMAGE:-text-monkey-cloud-proof-voice:local}"
restrictions=(--rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true)
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
restart_fixture="${script_dir}/../cloud/voice/test/container-restart-proof.mjs"

docker run "${restrictions[@]}" \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /data:rw,uid=1000,gid=1000,mode=700 \
  --tmpfs /app/logs:rw,uid=1000,gid=1000,mode=700 \
  -e DATABASE_URL=sqlite:////data/cloud-proof.db \
  -e SMS_PROVIDER=google_voice -e GOOGLE_VOICE_ENABLED=false \
  -e LIVE_SMS=false -e AUTOMATION_ENABLED=false -e DEMO_MODE=false \
  -e MAC_BRIDGE_ENABLED=false -e COMPETITION_CONFIRMATION_REQUIRED=true \
  "$backend_image" python - <<'PY'
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

assert os.getuid() != 0, "Backend must run as a nonroot user"
process = subprocess.Popen(
    ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--no-access-log"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

def request(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8000" + path, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as response_error:
        response = response_error
    with response:
        return response.status, json.load(response)

try:
    deadline = time.monotonic() + 45
    while True:
        assert process.poll() is None, "Backend exited before becoming healthy"
        try:
            status, _ = request("/healthz")
            if status == 200:
                break
        except (urllib.error.URLError, TimeoutError):
            pass
        assert time.monotonic() < deadline, "Backend startup timed out"
        time.sleep(0.25)
    status, config = request("/api/config")
    assert status == 200 and config["messagingTransport"] == "google_voice"
    assert config["automationEnabled"] is False and config["aiReady"] is False
    assert config["providerPolicyHold"] is True
    assert config["macBridgeConfigured"] is False and config["liveSms"] is False
    for path, body in (("/api/cloud-texting", None), ("/api/cloud-texting/session", b"{}"),
                       ("/api/cloud-texting/pause", b'{"paused":false}')):
        status, _ = request(path, body)
        assert status == 503, "Unconfigured anonymous cloud controls must fail closed"
    print("PASS: actual backend HTTP health/configuration; anonymous cloud controls rejected; permanent provider policy hold")
finally:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
PY

docker run "${restrictions[@]}" --shm-size 256m \
  --tmpfs /tmp:rw,nosuid,size=256m \
  --tmpfs /data:rw,uid=1000,gid=1000,mode=700 \
  "$voice_image" node --input-type=module <<'JS'
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { once } from 'node:events';
import { VoiceBrowser } from './browser.mjs';
import { Connector, Store } from './core.mjs';
import { apiServer } from './server.mjs';

assert.notEqual(process.getuid(), 0, 'Connector must run as a nonroot user');
const browser = new VoiceBrowser({ directory: '/data', executablePath: '/usr/bin/chromium', allowedPhones: [] });
let server;
try {
  await browser.start();
  assert.equal(await browser.page.evaluate(() => 6 * 7), 42);
  assert.equal(browser.page.url(), 'about:blank');
  const store = new Store('/data');
  await store.load();
  const connector = new Connector({ store, browser, expectedEmail: 'cloud-proof@example.invalid',
    expectedPhone: '+12025550100', allowedPhones: [] });
  // Exercise the real HTTP adapter without main(), which starts Google polling.
  const token = randomBytes(32).toString('hex');
  server = apiServer(connector, token);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const url = `http://127.0.0.1:${server.address().port}/health`;
  assert.equal((await fetch(url)).status, 401);
  const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  assert.equal(response.status, 200);
  const health = await response.json();
  assert.equal(health.ready, false);
  assert.equal(health.identity_verified, false);
  assert.equal(health.delivery_verified, false);
  assert.equal(health.reason_code, 'session_not_verified');
  assert.equal(browser.page.url(), 'about:blank');
  console.log('PASS: actual Chromium launches; private connector HTTP health requires authentication and reports no verified session');
} finally {
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
  await browser.close();
}
JS

# Exercise the real production startup, including all enabled flags. These tests
# assert no browser, account, profile, ledger or polling activity is possible.
docker run "${restrictions[@]}" \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /data:rw,uid=1000,gid=1000,mode=700 \
  --mount "type=bind,src=${script_dir}/../cloud/voice/test/server-startup.test.mjs,dst=/app/test/server-startup.test.mjs,readonly" \
  "$voice_image" node --test test/server-startup.test.mjs
printf '%s\n' 'PASS: production connector permanently rejects automation even with enabled flags; no account or browser access'

# Docker generates a unique anonymous name. Never inspect, reuse or remove any
# deployment volume. The EXIT trap removes only the volume created by this run.
proof_volume="$(docker volume create --label com.text-monkey.proof=container-restart)"
cleanup_proof_volume() {
  docker volume rm "$proof_volume" >/dev/null
}
trap cleanup_proof_volume EXIT
restart_args=("${restrictions[@]}" --shm-size 256m
  --tmpfs /tmp:rw,nosuid,size=256m
  --mount "type=volume,src=${proof_volume},dst=/data"
  --mount "type=bind,src=${restart_fixture},dst=/app/test/container-restart-proof.mjs,readonly"
  -e CLOUD_PROOF_RESTART_FIXTURE=synthetic)
interrupted_status=0
docker run "${restart_args[@]}" "$voice_image" node test/container-restart-proof.mjs reserve || interrupted_status=$?
if [[ "$interrupted_status" != 75 ]]; then
  printf '%s\n' "ERROR: reservation proof exited ${interrupted_status}; expected its controlled interruption (75)." >&2
  exit 1
fi
docker run "${restart_args[@]}" "$voice_image" node test/container-restart-proof.mjs recover
cleanup_proof_volume
trap - EXIT

bash "$script_dir/cloud_voice_backend_recovery_proof.sh"

printf '%s\n' 'PROOF SCOPE: policy-held production startup and offline fixtures only. No Google/Gloo login, texts, carrier delivery, or continuous free-hosting claim.'
