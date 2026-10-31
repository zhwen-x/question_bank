"""打包版的入口：双击就开窗口。

命令行仍然可用——传了参数就当命令行跑：

    qbank.app/Contents/MacOS/qbank doctor
    qbank.exe bundle export -o 备份.qbz

不这么做的话，打包版就只剩一个窗口，而 `doctor`（出问题时第一个要跑的东西）
和 `bundle export`（唯一的备份手段）都够不着了。
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> None:
    # 打包后子进程会重新执行入口；不冻结的话 uvicorn/reload 一类会无限分叉
    multiprocessing.freeze_support()

    if len(sys.argv) > 1:
        from qbank.cli import app

        app()
        return

    from qbank import desktop

    try:
        desktop.run()
    except desktop.DesktopUnavailableError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
