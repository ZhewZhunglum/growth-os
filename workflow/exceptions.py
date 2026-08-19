from django.core.exceptions import ValidationError


class WorkflowConflict(ValidationError):
    """Base class for rejected workflow commands that must not write facts."""


class CommandReplayConflict(WorkflowConflict):
    """A command id was reused with a different canonical payload."""


class OptimisticConcurrencyConflict(WorkflowConflict):
    """The expected aggregate version is no longer current."""


class IllegalTaskTransition(WorkflowConflict):
    """The requested state transition is not in the frozen state machine."""


class CheckGateRejected(WorkflowConflict):
    """A transition was attempted without the required passing check run."""

