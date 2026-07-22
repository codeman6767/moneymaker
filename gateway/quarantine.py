"""Execution quarantine.

The project is now a strictly read-only MLB/NBA betting **recommendation**
engine: it must never place, cancel or manage a bet. The earlier L0-L8 build
included an execution gateway; that code is preserved for reference but
quarantined. It is never imported on the read-only application's startup path
(see the ``sports_quant`` package), and the only code paths that could actually
contact an exchange -- the real Kalshi network transports -- call
:func:`ensure_execution_allowed` first, which always raises here.

Re-enabling execution is deliberately not a config toggle: it requires editing
this module, at which point every model, paper-trading and risk gate in
``CLAUDE.md`` still applies. Speed is never a reason to enable live orders.
"""

from __future__ import annotations

# The single, source-level switch. Read-only mode keeps this True; there is no
# environment variable or runtime flag that flips it.
EXECUTION_QUARANTINED = True


class ExecutionQuarantinedError(RuntimeError):
    """Raised when quarantined execution code attempts to contact an exchange."""


def ensure_execution_allowed() -> None:
    """Raise unless execution has been explicitly un-quarantined in source."""

    if EXECUTION_QUARANTINED:
        raise ExecutionQuarantinedError(
            "Order execution is quarantined: this project is a read-only "
            "recommendation engine and must never place, cancel or manage a bet."
        )
