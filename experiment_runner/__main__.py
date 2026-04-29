import argparse
from pathlib import Path

import uvicorn

from .app import create_app


def main():
    p = argparse.ArgumentParser(prog="experiment_runner")
    p.add_argument("--root", required=True, type=Path,
                   help="storage root, e.g. ~/data/notebooks/runner or /data/notebooks/runner")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()

    root = args.root.expanduser().resolve()
    (root / "experiments").mkdir(parents=True, exist_ok=True)

    app = create_app(root)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info", workers=1)


if __name__ == "__main__":
    main()
