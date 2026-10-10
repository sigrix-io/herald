"""``python -m herald`` — the same command as the ``herald`` script."""

from herald.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
