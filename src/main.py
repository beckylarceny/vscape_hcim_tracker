"""Main entry point: run the collector service."""

from __future__ import annotations

from .collector.main import main

if __name__ == "__main__":
    raise SystemExit(main())
