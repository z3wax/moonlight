"""Bot only (no web server) - for hosts where the panel runs as a separate
Flask/WSGI app, e.g. PythonAnywhere: run this as an Always-on task.

    python worker.py
"""

import asyncio
import sys

from autoreply.config import Bootstrap
from autoreply.runner import run


def main() -> int:
    boot = Bootstrap.from_env()
    try:
        return asyncio.run(run(boot, web=False))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
