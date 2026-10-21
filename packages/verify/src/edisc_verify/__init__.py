"""The offline verifier (`edisc-verify`, M18 step 5, plan §5, D12): everything a third party needs
to check a custody, render or report package without our database, object storage or services.

It imports only the standard library, its own modules and its pinned dependencies (`rfc8785`,
`cryptography`); the product imports from it, never the reverse (`tests/unit/verify/
test_boundary.py`). Formats it reads: `FORMATS` (printed by ``edisc-verify --version``)."""

__version__ = "0.1.0"  # = packages/verify/pyproject.toml (a unit test compares them)

FORMATS = {
    "custody": ("edisc-custody-package/1", "edisc-custody-package/2", "edisc-custody-package/3"),
    "render": ("edisc-render-package/1", "edisc-render-package/2", "edisc-render-package/3"),
    "report": ("edisc-report-package/1",),
}
