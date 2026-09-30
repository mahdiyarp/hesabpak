# -*- coding: utf-8 -*-
"""Read-only reconciliation CLI.

    python -m utils.reconcile            # human-readable Persian summary
    python -m utils.reconcile --json     # machine-readable
    python -m utils.reconcile --quiet    # exit status only, for cron

Exit codes are the contract:

* ``0`` -- nothing needs attention (info-level findings are fine);
* ``1`` -- warnings or critical findings, i.e. an operator should look;
* ``2`` -- the check could not run (bad configuration, missing database).

The command never writes. It opens the application read-only for the duration
and rolls the session back on the way out, so an interrupted run cannot leave a
half-applied change behind.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _bootstrap() -> str:
    """Point the app at the database to inspect, before it is imported."""
    if "--data-dir" in sys.argv:
        index = sys.argv.index("--data-dir")
        if index + 1 < len(sys.argv):
            os.environ["DATA_DIR"] = sys.argv[index + 1]
    return os.environ.get("DATA_DIR", "")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m utils.reconcile",
        description="بررسی یکپارچگی حسابداری (فقط خواندنی)",
    )
    parser.add_argument("--json", action="store_true", help="خروجی JSON")
    parser.add_argument("--quiet", action="store_true", help="فقط کد خروجی، بدون گزارش")
    parser.add_argument(
        "--data-dir", default=None, help="مسیر دیتابیس (معمولاً از متغیر DATA_DIR)"
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="حداکثر یافته‌های چاپ‌شده"
    )
    args = parser.parse_args(argv)

    _bootstrap()
    try:
        import app as app_module
    except Exception as exc:  # noqa: BLE001 - startup failure is a CLI error
        print(f"cannot start the application: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        with app_module.app.app_context():
            # Nothing below writes; make that structural rather than a promise.
            from extensions import db
            from utils import reconciliation

            try:
                report = reconciliation.run()
            finally:
                db.session.rollback()
    except Exception as exc:  # noqa: BLE001 - report, never traceback-dump keys
        print(f"reconciliation could not run: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(reconciliation.format_text(report, limit=args.limit))

    return EXIT_OK if report.ok else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
