"""Container health-check entrypoint."""

from __future__ import annotations

import urllib.request


def main() -> None:
    with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=2) as response:
        if response.status != 200:
            raise SystemExit(1)
        response.read()


if __name__ == "__main__":
    main()
