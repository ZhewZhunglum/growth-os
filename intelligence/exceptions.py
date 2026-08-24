class IntelligenceError(Exception):
    pass


class IllegalStateTransition(IntelligenceError):
    pass


class StateVersionConflict(IntelligenceError):
    pass


class CommandReplayConflict(IntelligenceError):
    pass
