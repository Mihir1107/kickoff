"""The standalone verifier's boundary (M18 step 5, plan §5, D12): the distribution `edisc-verify`
owns the offline code and imports only the standard library, itself and its pinned dependencies;
the product imports from it, never the reverse; its dependencies are pinned exactly, hashed, and
match uv.lock; its wheel is reproducible."""

from __future__ import annotations

import ast
import importlib
import pathlib
import re
import subprocess
import sys
import tomllib
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
PKG = ROOT / "packages" / "verify"
SRC = PKG / "src" / "edisc_verify"
# what a module of the distribution may import, by top-level name
ALLOWED = frozenset({"edisc_verify", "rfc8785", "cryptography"})
# loaded at runtime by those (cryptography's bindings)
RUNTIME_EXTRA = frozenset({"_cffi_backend", "_openssl"})
# the offline modules that moved here, and the old paths that alias them for one release
MOVED = {
    "edisc_custody": ("merkle", "chain", "package", "cli", "archive", "package_archives",
                      "package_source", "rsmf_check", "render_files", "report_files",
                      "render_package", "report_package"),
    "edisc_core": ("canonical", "lone_surrogates", "jsonpath", "idempotency"),
    "edisc_renderers.report": ("model", "recompute", "version"),
}  # fmt: skip


def _modules() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def _imports(path: pathlib.Path) -> set[str]:
    """Every module an import statement names, anywhere in the file (functions included)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
        elif isinstance(node, ast.ImportFrom) and node.level:
            found.add("edisc_verify")  # relative: inside the distribution
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
        ):
            found.add("<dynamic import>")
    return found


def test_every_module_imports_only_the_stdlib_itself_and_its_pinned_dependencies() -> None:
    offenders = {
        f"{p.relative_to(SRC)}: {name}"
        for p in _modules()
        for name in _imports(p)
        if name.split(".")[0] not in ALLOWED | set(sys.stdlib_module_names)
    }
    assert offenders == set(), sorted(offenders)


def test_importing_the_whole_distribution_loads_nothing_else() -> None:
    names = [
        ".".join(p.relative_to(SRC.parent).with_suffix("").parts).removesuffix(".__init__")
        for p in _modules()
    ]
    code = (
        f"import sys; sys.path.insert(0, {str(SRC.parent)!r}); before = set(sys.modules)\n"
        f"for n in {names!r}: __import__(n)\n"
        "new = set(sys.modules) - before\n"
        "print(sorted({m.split('.')[0] for m in new} - set(sys.stdlib_module_names)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    loaded = set(ast.literal_eval(out.stdout.strip())) - {"__main__"}
    assert loaded <= ALLOWED | RUNTIME_EXTRA, sorted(loaded - ALLOWED - RUNTIME_EXTRA)


def test_its_dependencies_are_pinned_exactly_and_hashed_from_uv_lock() -> None:
    project = tomllib.loads((PKG / "pyproject.toml").read_text())["project"]
    deps = project["dependencies"]
    assert {re.split(r"[=<>!~ ;\[]", d)[0] for d in deps} == {"cryptography", "rfc8785"}
    assert all(re.fullmatch(r"[a-z0-9-]+==[0-9][0-9.]*", d) for d in deps), deps
    assert not any(d.startswith("edisc") for d in deps)  # never the reverse direction
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"]}
    for d in deps:
        name, version = d.split("==")
        assert locked[name] == version, (name, version, locked[name])
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verifier_standalone.py"), "check-lock"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = (PKG / "requirements.lock").read_text()
    for name in ("cryptography", "rfc8785"):
        assert re.search(rf"^{name}==\S+ .*\n\s+--hash=sha256:[0-9a-f]{{64}}", text, re.M), name


def test_the_product_imports_from_the_verifier_and_the_old_paths_are_aliases() -> None:
    for parent, children in MOVED.items():
        for child in children:
            old = importlib.import_module(f"{parent}.{child}")
            new_parent = (
                "edisc_verify.report" if parent == "edisc_renderers.report" else "edisc_verify"
            )
            assert old is importlib.import_module(f"{new_parent}.{child}"), (parent, child)
    # nothing in the product (or the tests) still imports the old paths
    old = re.compile(
        r"\b(edisc_custody\.(" + "|".join(MOVED["edisc_custody"]) + r")"
        r"|edisc_core\.(" + "|".join(MOVED["edisc_core"]) + r")"
        r"|edisc_renderers\.report\.(model|recompute|version))\b"
    )
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for top in ("packages", "apps", "workers", "tests", "scripts")
        for p in sorted((ROOT / top).rglob("*.py"))
        if "this path is an alias of it for one release" not in (text := p.read_text())
        and p.resolve() != pathlib.Path(__file__).resolve()
        for n, line in enumerate(text.splitlines(), 1)
        if old.search(line)
    ]
    assert offenders == []


@pytest.mark.timeout(300)
def test_the_wheel_is_reproducible_and_stamped_with_the_commit_time(tmp_path: pathlib.Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import verifier_standalone as vs
    finally:
        sys.path.pop(0)
    wheel, digest = vs.build(tmp_path)  # builds twice, compares, checks every entry's time
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    assert "edisc_verify/jws.py" in names and "edisc_verify/report/recompute.py" in names
    assert not any(n.startswith(("edisc_core/", "edisc_custody/", "tests/")) for n in names)
    assert len(digest) == 64


def test_the_verifier_states_its_version_and_every_format_it_reads() -> None:
    import edisc_verify
    from edisc_verify import package, render_package, report_package

    project = tomllib.loads((PKG / "pyproject.toml").read_text())["project"]
    assert edisc_verify.__version__ == project["version"]
    assert edisc_verify.FORMATS == {
        "custody": package.SUPPORTED_FORMATS,
        "render": (render_package.RENDER_PACKAGE_FORMAT_1, render_package.RENDER_PACKAGE_FORMAT_2,
                   render_package.RENDER_PACKAGE_FORMAT),
        "report": (report_package.REPORT_PACKAGE_FORMAT,),
    }  # fmt: skip
    out = subprocess.run(
        [sys.executable, "-m", "edisc_verify.cli", "--version"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert out.startswith(f"edisc-verify {project['version']}; reads ")
    assert all(f in out for kind in edisc_verify.FORMATS.values() for f in kind)
