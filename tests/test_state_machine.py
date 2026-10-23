import pytest

from app.schemas import ProposalStatus, RuleStatus
from app.state_machine import ProposalStateMachine, RuleStateMachine


def test_rule_lifecycle():
    sm = RuleStateMachine()
    assert sm.transition(RuleStatus.ACTIVE, RuleStatus.TRIGGERED) is RuleStatus.TRIGGERED
    assert sm.transition(RuleStatus.TRIGGERED, RuleStatus.ACTIVE) is RuleStatus.ACTIVE
    assert sm.can(RuleStatus.ACTIVE, RuleStatus.PAUSED)
    assert sm.can(RuleStatus.PAUSED, RuleStatus.ACTIVE)


def test_rule_invalid_transition_raises():
    sm = RuleStateMachine()
    with pytest.raises(ValueError):
        sm.transition(RuleStatus.ACTIVE, RuleStatus.ACTIVE)
    with pytest.raises(ValueError):
        sm.transition(RuleStatus.PAUSED, RuleStatus.PAUSED)


def test_proposal_lifecycle():
    sm = ProposalStateMachine()
    assert sm.transition(ProposalStatus.PROPOSED, ProposalStatus.APPROVED) is ProposalStatus.APPROVED
    assert sm.transition(ProposalStatus.APPROVED, ProposalStatus.EXECUTED) is ProposalStatus.EXECUTED
    assert sm.transition(ProposalStatus.PROPOSED, ProposalStatus.REJECTED) is ProposalStatus.REJECTED
    assert sm.transition(ProposalStatus.PROPOSED, ProposalStatus.CANCELLED) is ProposalStatus.CANCELLED


def test_proposal_terminal_states_are_locked():
    sm = ProposalStateMachine()
    for terminal in (ProposalStatus.EXECUTED, ProposalStatus.REJECTED, ProposalStatus.CANCELLED):
        with pytest.raises(ValueError):
            sm.transition(terminal, ProposalStatus.PROPOSED)
        with pytest.raises(ValueError):
            sm.transition(terminal, ProposalStatus.APPROVED)