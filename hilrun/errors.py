class PlanError(Exception):
    """The plan, bench, report, or hub reply is malformed. Not a DUT verdict."""


class StepFailure(Exception):
    def __init__(self, step_id, message, received, checked=False):
        super().__init__(message)
        self.step_id = step_id
        self.received = received
        self.checked = checked
