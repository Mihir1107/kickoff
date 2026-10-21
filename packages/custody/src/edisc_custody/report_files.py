"""Moved to `edisc_verify.report_files` (M18 step 5, D12): this path is an alias of it for one release."""

import sys

from edisc_verify import report_files as _moved

sys.modules[__name__] = _moved
