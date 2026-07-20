import pytest

from app.orchestration.state_machine import InvalidStateTransitionError, require_transition
from app.schemas.lifecycle import ApplicationStatus


def test_standard_start_and_stop_transitions_are_allowed() -> None:
    require_transition(ApplicationStatus.STOPPED, ApplicationStatus.CHECKING)
    require_transition(ApplicationStatus.CHECKING, ApplicationStatus.STARTING)
    require_transition(ApplicationStatus.STARTING, ApplicationStatus.RUNNING)
    require_transition(ApplicationStatus.RUNNING, ApplicationStatus.STOPPING)
    require_transition(ApplicationStatus.STOPPING, ApplicationStatus.STOPPED)


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(InvalidStateTransitionError, match="STOPPED -> RUNNING"):
        require_transition(ApplicationStatus.STOPPED, ApplicationStatus.RUNNING)
