"""Run a dedicated loopback PCO receiver for ngrok, without admin/text routes.

Requires private .env with the verified PCO scope and subscription signing secret.
The only writable public route is the signed Planning Center webhook.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from fastapi import FastAPI

from app.integrations.planning_center import PCOBase, PCOConfig, PlanningCenterError
from app.db.session import make_engine, make_session_factory
from app.web.planning_center import router


def create_receiver(config: PCOConfig, database_url: str):
    config.require_scope()
    if not config.app_id or not config.secret:
        raise PlanningCenterError("Configure private PCO credentials first")
    if not database_url.startswith("sqlite:///") or not database_url.endswith(".db"):
        raise PlanningCenterError("Receiver requires an explicit isolated SQLite demo .db")
    path = Path(database_url.removeprefix("sqlite:///"))
    if not path.is_file():
        raise PlanningCenterError("Run the scoped demo sync before starting its receiver")
    engine = make_engine(database_url)
    PCOBase.metadata.create_all(engine)
    app = FastAPI(title="Text Monkey Planning Center Receiver", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pco_config = config
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.include_router(router)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "planning-center-webhook", "scope": "synthetic-demo", "texting": False, "webhook_configured": bool(config.webhook_secret or config.webhook_secrets)}

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--port", type=int, default=58125)
    args = ap.parse_args()
    load_dotenv(args.env_file, override=True)
    app = create_receiver(PCOConfig.from_env(), args.database)
    import uvicorn
    # Never expose the full admin application through this dedicated tunnel.
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    try:
        main()
    except PlanningCenterError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
