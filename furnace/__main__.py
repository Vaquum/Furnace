import argparse
from pathlib import Path

import uvicorn
from starlette.responses import Response

from .app import create_app


def main():
    p = argparse.ArgumentParser(prog="furnace")
    p.add_argument("--root", required=True, type=Path,
                   help="storage root, e.g. /data/experiments")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--scheduler", action="store_true",
                   help="run the per-minute Conduit prediction-cache top-up loop "
                        "instead of the web app")
    p.add_argument("--scheduler-interval", type=int, default=60,
                   help="seconds between scheduler ticks (default 60, the /opt/arrow cadence)")
    args = p.parse_args()

    root = args.root.expanduser().resolve()
    (root / "experiments").mkdir(parents=True, exist_ok=True)

    if args.scheduler:
        # Separate process from the web app: `python -m furnace --root … --scheduler`.
        # Configure visible logging here (the entrypoint owns it) so the loop's
        # per-tick timing and any per-cohort failure surface on stdout.
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        from .cache_scheduler import run_forever
        run_forever(interval_seconds=args.scheduler_interval, root=root)
        return

    app = create_app(root)

    # Register HEAD on `/` so the SPEC's TDW-internal smoke check
    # `curl -sI http://127.0.0.1:8502/` returns HTTP/1.1 200 OK.
    # FastAPI's @app.get does not auto-register HEAD; without this,
    # HEAD on the root jobs route would return 405.
    @app.head("/")
    def _head_root() -> Response:
        return Response(status_code=200)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", workers=1)


if __name__ == "__main__":
    main()
