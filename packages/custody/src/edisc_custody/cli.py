"""``edisc-verify``: verify a custody package (job), render package or report package offline (ADR 0008,
ADR 0015, ADR 0018 §14). A job package may be the downloaded zip: its entries are copied through the
hardened zip reader into a temporary directory first (the job verifier reads directories).

Exit codes: 0 verified, 1 verification failed, 2 unreadable/unsupported package.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from edisc_custody.archive import ArchiveError
from edisc_custody.package import PackageFormatError, verify_package
from edisc_custody.package_source import ZipSource, is_zip
from edisc_custody.render_package import (
    RenderPackageReport,
    is_render_package,
    verify_render_package,
)
from edisc_custody.report_package import (
    ReportPackageReport,
    is_report_package,
    verify_report_package,
)


@contextlib.contextmanager
def _as_directory(path: Path | None) -> Iterator[Path | None]:
    """A job package zip as a temporary directory (entries copied through the hardened reader, every
    name a plain relative path); a directory (or None) as itself."""
    if path is None or not is_zip(path):
        yield path
        return
    source = ZipSource(path)
    try:
        with tempfile.TemporaryDirectory(prefix="edisc-verify-") as tmp:
            for name in source.names():
                parts = PurePosixPath(name).parts
                if not parts or name.startswith("/") or ".." in parts or name.endswith("/"):
                    raise PackageFormatError(f"unsafe or unexpected entry name {name!r}")
                target = Path(tmp, *parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as fh:
                    for chunk in source.chunks(name):
                        fh.write(chunk)
            yield Path(tmp)
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edisc-verify",
        description="Verify an eDiscovery custody package without database access.",
    )
    parser.add_argument(
        "package",
        type=Path,
        help="directory produced by the custody export, or a render package (directory or zip)",
    )
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument(
        "--archive",
        type=Path,
        action="append",
        default=[],
        help="an archive (export zip) the package references by hash instead of embedding; repeatable."
        " Matched by its SHA-256, which is checked before any of its entries is read",
    )
    parser.add_argument(
        "--file",
        type=Path,
        action="append",
        default=[],
        help="render packages: an output file or native the package references by hash instead of"
        " embedding; repeatable, matched by SHA-256",
    )
    parser.add_argument(
        "--job-package",
        type=Path,
        help="render and report packages: the job's custody package (directory or zip), verified and"
        " matched; for a report, its chain-derived content is recomputed from it and compared",
    )
    parser.add_argument(
        "--tolerate-os-metadata",
        action="store_true",
        help="render packages, DIRECTORIES only: accept .DS_Store, ._* (AppleDouble), __MACOSX/,"
        " Thumbs.db and desktop.ini left by a file manager; each is listed. Never applies to a zip."
        " Verify the downloaded zip itself, not an extracted folder, whenever you can",
    )
    args = parser.parse_args(argv)
    try:
        with _as_directory(args.job_package) as job_dir:
            if is_render_package(args.package):
                return _render(
                    args.package, args.file, job_dir, as_json=args.json,
                    tolerate=args.tolerate_os_metadata,
                )  # fmt: skip
            if is_report_package(args.package):
                return _report(
                    args.package, job_dir, as_json=args.json, tolerate=args.tolerate_os_metadata
                )
        with _as_directory(args.package) as root:
            report = verify_package(root or args.package, args.archive)
    except (OSError, PackageFormatError, KeyError, ValueError, ArchiveError) as exc:
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


def _render(
    package: Path, files: list[Path], job: Path | None, *, as_json: bool, tolerate: bool
) -> int:
    try:
        report: RenderPackageReport = verify_render_package(
            package, files, job, tolerate_os_metadata=tolerate
        )
    except (OSError, PackageFormatError, KeyError, ValueError, ArchiveError) as exc:
        sys.stderr.write(f"edisc-verify: cannot read package: {exc}\n")
        return 2
    if as_json:
        sys.stdout.write(json.dumps(report.as_dict(), indent=2) + "\n")
        return 0 if report.ok else 1
    chain = report.chain
    sys.stdout.write(
        f"{'VERIFIED' if report.ok else 'FAILED'}  render package, manifest sha256 {report.manifest_sha256}\n"
    )
    if chain is not None:
        sys.stdout.write(
            f"  render stream {chain.stream_id}: {chain.events} events, {chain.batches_checked} file"
            f" batches, {chain.files_checked} files in Merkle roots, {chain.anchors_checked} WORM anchors\n"
            f"  head {chain.head_hash}\n"
        )
    sys.stdout.write(
        f"  {report.outputs_checked} output files and {report.natives_checked} natives re-hashed\n"
    )
    if tolerate and is_zip(package):
        sys.stdout.write("  --tolerate-os-metadata does not apply to a zip: verified strictly\n")
    for name in report.tolerated:
        sys.stdout.write(f"  TOLERATED {name} (OS metadata, not part of the package)\n")
    if report.job is not None:
        sys.stdout.write(
            f"  job package: {'VERIFIED' if report.job.ok else 'FAILED'}"
            f" ({report.job.items_checked} items)\n"
        )
    errors = report.errors + (chain.errors if chain else [])
    if report.job is not None:
        errors += [
            f"job: {e}"
            for e in report.job.errors + (report.job.chain.errors if report.job.chain else [])
        ]
    for err in errors:
        sys.stdout.write(f"  ERROR {err}\n")
    return 0 if report.ok else 1


def _report(package: Path, job: Path | None, *, as_json: bool, tolerate: bool) -> int:
    report: ReportPackageReport = verify_report_package(package, job, tolerate_os_metadata=tolerate)
    if as_json:
        sys.stdout.write(json.dumps(report.as_dict(), indent=2) + "\n")
        return 0 if report.ok else 1
    chain = report.chain
    sys.stdout.write(
        f"{'VERIFIED' if report.ok else 'FAILED'}  report package, manifest sha256"
        f" {report.manifest_sha256}\n"
    )
    if chain is not None:
        sys.stdout.write(
            f"  report stream {chain.stream_id}: {chain.events} events,"
            f" {chain.anchors_checked} WORM anchors\n  head {chain.head_hash}\n"
        )
    sys.stdout.write(f"  {report.outputs_checked} report files re-hashed\n")
    for name in report.tolerated:
        sys.stdout.write(f"  TOLERATED {name} (OS metadata, not part of the package)\n")
    if report.job is not None:
        sys.stdout.write(
            f"  job package: {'VERIFIED' if report.job.ok else 'FAILED'}"
            f" ({report.job.items_checked} items)\n"
        )
        if report.recomputed:
            sys.stdout.write(
                "  chain-derived content RECOMPUTED from the job chain: identical\n"
                "  checked against recorded digests only (not recomputable offline): "
                + "; ".join(report.not_recomputed)
                + "\n"
            )
    else:
        sys.stdout.write("  chain-derived content not recomputed (no --job-package given)\n")
    errors = report.errors + (chain.errors if chain else [])
    if report.job is not None:
        errors += [
            f"job: {e}"
            for e in report.job.errors + (report.job.chain.errors if report.job.chain else [])
        ]
    for err in errors:
        sys.stdout.write(f"  ERROR {err}\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
