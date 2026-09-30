import argparse

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="ui", description="워크플로우 화면 (로컬 전용)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(f"워크플로우 화면: http://{args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


main()
