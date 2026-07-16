#!/usr/bin/env python3
"""Standalone live viewer for the KR260 raw DVS UDP stream."""

import argparse
import sys

from actnow_client import DEFAULT_SOCKET_BUF, render


def main():
    ap = argparse.ArgumentParser(
        description="Render raw DVS events from an already-running ActNow server")
    ap.add_argument("--port", type=int, default=3336)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--scale", type=int, default=5)
    ap.add_argument("--decay", type=float, default=0.88)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--socket-buf", type=int, default=DEFAULT_SOCKET_BUF)
    ap.add_argument("--max-drain-packets", type=int, default=4096)
    ap.add_argument("--max-frame-words", type=int, default=100000)
    args = ap.parse_args()
    args.title = "ActNow raw DVS stream"
    args.flip_up_down = True
    args.colourblind = True

    try:
        render(args)
    except KeyboardInterrupt:
        pass
    print("\nstopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
