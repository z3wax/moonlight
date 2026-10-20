"""Run the bot + admin panel:  python main.py   (panel: http://localhost:7860)"""

import asyncio
import sys

from autoreply.config import Bootstrap
from autoreply.runner import run


def main() -> int:
    boot = Bootstrap.from_env()
    try:
        return asyncio.run(run(boot))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
