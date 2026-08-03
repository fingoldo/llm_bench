"""Sequential Halving — multi-round elimination cascade.

Public surface re-exports the pieces consumers compose: the schedule,
the driver state machine, the round result dataclass, the pruner, the
validator-pair helpers, and the alive/dead filter.
"""

from llm_bench.halving.alive_filter import (
    DEAD_ERROR_CLASSES,
    TRANSIENT_ERROR_CLASSES,
    is_alive_candidate,
    load_dead_model_ids,
)
from llm_bench.halving.assignment import assign_task_units_for_round
from llm_bench.halving.driver import Halving, RoundResult
from llm_bench.halving.pairing import (
    allowed_validator_pairs,
    select_validator_for_producer,
)
from llm_bench.halving.pruner import mad_bootstrap_prune
from llm_bench.halving.schedule import HalvingSchedule

__all__ = [
    "HalvingSchedule",
    "Halving",
    "RoundResult",
    "mad_bootstrap_prune",
    "assign_task_units_for_round",
    "allowed_validator_pairs",
    "select_validator_for_producer",
    "is_alive_candidate",
    "load_dead_model_ids",
    "DEAD_ERROR_CLASSES",
    "TRANSIENT_ERROR_CLASSES",
]
