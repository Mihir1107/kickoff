"""Moved to `edisc_verify.report.version` (M18 step 5, D12): this path is an alias of it for one release."""

import sys

from edisc_verify.report import version as _moved

sys.modules[__name__] = _moved
