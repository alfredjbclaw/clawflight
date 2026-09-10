"""clawflight — family flight alerts from a forwarding address.

A pure-stdlib engine: parse airline confirmation email and calendar events,
attribute each flight to a configured person, watch it with keyless public
feeds, and hand bounded alert text to a channel adapter.

Nothing in this package performs network I/O at import time, and no module
carries credentials. Secrets are always referenced by environment-variable
name via :mod:`clawflight.config`.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
