"""Moved to `edisc_verify.package_archives` (M18 step 5, D12): this path is an alias of it for one release."""

import sys

from edisc_verify import package_archives as _moved

sys.modules[__name__] = _moved
