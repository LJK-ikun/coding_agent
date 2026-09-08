"""允许用 `python -m mewcode` 启动 CLI。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
