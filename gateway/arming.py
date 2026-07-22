"""Arming controller: the hard gate between demo and live orders.

Demo orders are always permitted (they hit the paper environment). Live orders
require an explicit, manual arming step with a confirmation token -- code being
fast never arms live trading. The controller auto-disarms on danger signals
(consecutive failures, market pause, reconciliation mismatch), reverting to the
safe state until a human re-arms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# The literal a human must pass to arm live trading. Being explicit and awkward
# is the point.
LIVE_ARM_TOKEN = "ARM-LIVE-CONFIRM"


class ArmError(Exception):
    """Raised when a live order is attempted without valid arming."""


@dataclass
class ArmingController:
    environment: str = "demo"
    _armed: bool = False
    disarm_reason: Optional[str] = None
    history: List[str] = field(default_factory=list)

    @property
    def is_armed(self) -> bool:
        return self._armed

    def arm(self, confirmation: str) -> None:
        if self.environment != "live":
            # Demo never needs arming and cannot be "armed live".
            raise ArmError("cannot arm: environment is not 'live'")
        if confirmation != LIVE_ARM_TOKEN:
            raise ArmError("invalid arming confirmation token")
        self._armed = True
        self.disarm_reason = None
        self.history.append("armed")

    def disarm(self, reason: str) -> None:
        if self._armed:
            self.history.append(f"disarmed: {reason}")
        self._armed = False
        self.disarm_reason = reason

    def ensure_order_allowed(self) -> None:
        """Gate every order. Demo passes; live requires being armed."""

        if self.environment == "demo":
            return
        if not self._armed:
            raise ArmError(
                "live order refused: not armed"
                + (f" (last disarm: {self.disarm_reason})" if self.disarm_reason else "")
            )
