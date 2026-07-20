from app.schemas.lifecycle import ApplicationStatus


class InvalidStateTransitionError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    ApplicationStatus.STOPPED: {ApplicationStatus.CHECKING, ApplicationStatus.DISABLED},
    ApplicationStatus.CHECKING: {
        ApplicationStatus.STARTING,
        ApplicationStatus.QUEUED,
        ApplicationStatus.FAILED,
        ApplicationStatus.STOPPED,
    },
    ApplicationStatus.QUEUED: {ApplicationStatus.CHECKING, ApplicationStatus.STOPPED},
    ApplicationStatus.STARTING: {
        ApplicationStatus.RUNNING,
        ApplicationStatus.UNHEALTHY,
        ApplicationStatus.FAILED,
        ApplicationStatus.STOPPED,
    },
    ApplicationStatus.RUNNING: {
        ApplicationStatus.STARTING,
        ApplicationStatus.STOPPING,
        ApplicationStatus.UNHEALTHY,
        ApplicationStatus.FAILED,
        ApplicationStatus.UNKNOWN,
    },
    ApplicationStatus.UNHEALTHY: {
        ApplicationStatus.RUNNING,
        ApplicationStatus.STARTING,
        ApplicationStatus.STOPPING,
        ApplicationStatus.FAILED,
    },
    ApplicationStatus.STOPPING: {ApplicationStatus.STOPPED, ApplicationStatus.FAILED},
    ApplicationStatus.FAILED: {
        ApplicationStatus.CHECKING,
        ApplicationStatus.STOPPING,
        ApplicationStatus.STOPPED,
    },
    ApplicationStatus.UNKNOWN: {
        ApplicationStatus.CHECKING,
        ApplicationStatus.RUNNING,
        ApplicationStatus.STOPPING,
        ApplicationStatus.STOPPED,
        ApplicationStatus.FAILED,
    },
    ApplicationStatus.DISABLED: {ApplicationStatus.STOPPED},
}


def require_transition(current: ApplicationStatus, target: ApplicationStatus) -> None:
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransitionError(f"Invalid state transition: {current.value} -> {target.value}")
