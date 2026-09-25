"""Start the service with one command."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from line_control.console.server import LineConsole
from line_control.line.supervisor import LineSupervisor


def build_parser() -> argparse.ArgumentParser:
    """Return the command line parser."""
    parser = argparse.ArgumentParser(prog="line-control")
    parser.add_argument("--data-dir", default="var", help="where the record stream is kept")
    parser.add_argument("--host", default="127.0.0.1", help="address to bind")
    parser.add_argument("--port", type=int, default=8080, help="port to bind")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Open the supervisor and serve its console."""
    args = build_parser().parse_args(argv)
    supervisor = LineSupervisor(Path(args.data_dir))
    console = LineConsole(supervisor, host=args.host, port=args.port)
    try:
        console.serve()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
