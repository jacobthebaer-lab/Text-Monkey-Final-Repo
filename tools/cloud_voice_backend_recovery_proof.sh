#!/usr/bin/env bash
set -euo pipefail
backend_image="${CLOUD_PROOF_BACKEND_IMAGE:-text-monkey-cloud-proof-backend:local}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
proof_volume="$(docker volume create --label com.text-monkey.proof=backend-restart)"
cleanup_proof_volume() { docker volume rm "$proof_volume" >/dev/null; }
trap cleanup_proof_volume EXIT
for phase in reserve recover; do
  docker run --rm -i --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --tmpfs /app/logs:rw,uid=1000,gid=1000,mode=700 \
    --mount "type=volume,src=${proof_volume},dst=/data" \
    -e CLOUD_PROOF_RESTART_FIXTURE=synthetic \
    -e DATABASE_URL=sqlite:// -e SMS_PROVIDER=mock -e MAC_BRIDGE_ENABLED=false \
    -e LIVE_SMS=false -e AUTOMATION_ENABLED=false -e PYTHON_DOTENV_DISABLED=1 \
    "$backend_image" python - "$phase" < "$script_dir/cloud_voice_backend_recovery_proof.py"
done
