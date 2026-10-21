"""Moved to `edisc_verify.merkle` (M18 step 5, D12): this path is an alias of it for one release."""

import sys

from edisc_verify import merkle as _moved

sys.modules[__name__] = _moved
