"""Position reconciliation between local Python state and bridge."""

import logging
import time
from dataclasses import dataclass, field

from core.types import Outcome, Position

logger = logging.getLogger(__name__)


@dataclass
class PositionMismatch:
    """A single position mismatch between local and bridge."""
    market_id: str
    outcome: Outcome
    local_size: float
    bridge_size: float
    mismatch_type: str  # "local_only", "bridge_only", "size_mismatch"


@dataclass
class ReconciliationResult:
    """Result of a position reconciliation."""
    matched: list[str] = field(default_factory=list)  # market_id:outcome keys that match
    local_only: list[PositionMismatch] = field(default_factory=list)
    bridge_only: list[PositionMismatch] = field(default_factory=list)
    size_mismatches: list[PositionMismatch] = field(default_factory=list)

    @property
    def has_mismatches(self) -> bool:
        return bool(self.local_only or self.bridge_only or self.size_mismatches)

    @property
    def mismatch_count(self) -> int:
        return len(self.local_only) + len(self.bridge_only) + len(self.size_mismatches)

    def summary(self) -> str:
        parts = [f"matched={len(self.matched)}"]
        if self.local_only:
            parts.append(f"local_only={len(self.local_only)}")
        if self.bridge_only:
            parts.append(f"bridge_only={len(self.bridge_only)}")
        if self.size_mismatches:
            parts.append(f"size_mismatches={len(self.size_mismatches)}")
        return ", ".join(parts)


class PositionReconciler:
    """Reconciles local Python positions with bridge-side positions.

    Called periodically in live mode to detect and correct drift between
    the Python position tracker and the bridge's actual state.
    """

    def __init__(self, reconcile_interval: float = 300.0):
        self.reconcile_interval = reconcile_interval
        self._last_reconcile: float = 0

    def should_reconcile(self) -> bool:
        """Whether it's time to run reconciliation."""
        return time.time() - self._last_reconcile >= self.reconcile_interval

    def reconcile(
        self,
        local_positions: list[Position],
        bridge_positions: list[Position],
    ) -> ReconciliationResult:
        """Compare local and bridge positions, return mismatches.

        Args:
            local_positions: Positions tracked in Python (Portfolio)
            bridge_positions: Positions reported by the bridge

        Returns:
            ReconciliationResult with matched, local_only, bridge_only, size_mismatches
        """
        self._last_reconcile = time.time()
        result = ReconciliationResult()

        # Build lookup dicts keyed by market_id:outcome
        local_map: dict[str, Position] = {}
        for pos in local_positions:
            key = f"{pos.market_id}:{pos.outcome.value}"
            local_map[key] = pos

        bridge_map: dict[str, Position] = {}
        for pos in bridge_positions:
            key = f"{pos.market_id}:{pos.outcome.value}"
            bridge_map[key] = pos

        all_keys = set(local_map.keys()) | set(bridge_map.keys())

        for key in all_keys:
            local_pos = local_map.get(key)
            bridge_pos = bridge_map.get(key)

            if local_pos and bridge_pos:
                # Both exist — check size
                if abs(local_pos.size - bridge_pos.size) > 0.01:
                    mismatch = PositionMismatch(
                        market_id=local_pos.market_id,
                        outcome=local_pos.outcome,
                        local_size=local_pos.size,
                        bridge_size=bridge_pos.size,
                        mismatch_type="size_mismatch",
                    )
                    result.size_mismatches.append(mismatch)
                    logger.warning(
                        f"Size mismatch {key}: local={local_pos.size:.2f} "
                        f"bridge={bridge_pos.size:.2f}"
                    )
                else:
                    result.matched.append(key)

            elif local_pos and not bridge_pos:
                mismatch = PositionMismatch(
                    market_id=local_pos.market_id,
                    outcome=local_pos.outcome,
                    local_size=local_pos.size,
                    bridge_size=0.0,
                    mismatch_type="local_only",
                )
                result.local_only.append(mismatch)
                logger.warning(f"Local-only position {key}: size={local_pos.size:.2f}")

            elif bridge_pos and not local_pos:
                mismatch = PositionMismatch(
                    market_id=bridge_pos.market_id,
                    outcome=bridge_pos.outcome,
                    local_size=0.0,
                    bridge_size=bridge_pos.size,
                    mismatch_type="bridge_only",
                )
                result.bridge_only.append(mismatch)
                logger.warning(f"Bridge-only position {key}: size={bridge_pos.size:.2f}")

        if result.has_mismatches:
            logger.warning(f"Reconciliation: {result.summary()}")
        else:
            logger.info(f"Reconciliation OK: {len(result.matched)} positions matched")

        return result
