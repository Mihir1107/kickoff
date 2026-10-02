"""``edisc-verify``: verify an exported custody package offline (ADR 0008).

Exit codes: 0 verified, 1 verification failed, 2 unreadable/unsupported package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from edisc_custody.package import PackageFormatError, verify_package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edisc-verify",
        description="Verify an eDiscovery custody package without database access.",
    )
    parser.add_argument("package", type=Path, help="directory produced by the custody export")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument(
        "--archive",
        type=Path,
        action="append",
        default=[],
        help="an archive (export zip) the package references by hash instead of embedding; repeatable."
        " Matched by its SHA-256, which is checked before any of its entries is read",
    )
    args = parser.parse_args(argv)
    try:
        report = verify_package(args.package, args.archive)
    except (OSError, PackageFormatError, KeyError, ValueError) as exc:
        sys.stderr.write(f"edisc-verify: cannot read package: {exc}\n")
        return 2
    if args.json:
        sys.stdout.write(json.dumps(report.as_dict(), indent=2) + "\n")
    else:
        chain = report.chain
        status = "VERIFIED" if report.ok else "FAILED"
        sys.stdout.write(f"{status}  manifest sha256 {report.manifest_sha256}\n")
        if chain is not None:
            sys.stdout.write(
                f"  stream {chain.stream_id}: {chain.events} events, {chain.batches_checked} batches,"
                f" {chain.items_checked} items in Merkle roots, {chain.anchors_checked} WORM anchors\n"
                f"  head {chain.head_hash}\n"
            )
        sys.stdout.write(
            f"  {report.items_checked} items and {report.objects_checked} evidence objects re-hashed\n"
        )
        if report.archives_checked or report.entries_checked:
            sys.stdout.write(
                f"  {report.archives_checked} archives hash-checked, {report.entries_checked} archive"
                " entries extracted and verified\n"
            )
        for err in report.errors + (chain.errors if chain else []):
            sys.stdout.write(f"  ERROR {err}\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
