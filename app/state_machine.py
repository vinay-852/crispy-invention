from enum import Enum

from .schemas import ProposalStatus, RuleStatus


class StateMachine:
    """Generic enum state machine driven by an explicit transition table."""

    def __init__(self, transitions: dict[Enum, set[Enum]]):
        self._transitions = {a: set(b) for a, b in transitions.items()}

    def can(self, current: Enum, target: Enum) -> bool:
        return target in self._transitions.get(current, set())

    def transition(self, current: Enum, target: Enum) -> Enum:
        if not self.can(current, target):
            raise ValueError(f"Invalid transition: {current.value} -> {target.value}")
        return target


class RuleStateMachine(StateMachine):
    """Rule lifecycle: active <-> paused, active -> triggered (research in flight)."""

    def __init__(self):
        super().__init__({
            RuleStatus.ACTIVE: {RuleStatus.PAUSED, RuleStatus.TRIGGERED},
            RuleStatus.PAUSED: {RuleStatus.ACTIVE, RuleStatus.TRIGGERED},
            RuleStatus.TRIGGERED: {RuleStatus.ACTIVE, RuleStatus.PAUSED},
        })


class ProposalStateMachine(StateMachine):
    """Proposal lifecycle: proposed -> approved -> executed (or rejected/cancelled)."""

    def __init__(self):
        super().__init__({
            ProposalStatus.PROPOSED: {ProposalStatus.APPROVED, ProposalStatus.REJECTED, ProposalStatus.CANCELLED},
            ProposalStatus.APPROVED: {ProposalStatus.EXECUTED, ProposalStatus.CANCELLED},
            ProposalStatus.REJECTED: set(),
            ProposalStatus.CANCELLED: set(),
            ProposalStatus.EXECUTED: set(),
        })