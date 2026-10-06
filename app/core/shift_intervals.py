"""One effective interval for whole slots and reviewed child slots."""
class IntervalEvent:
    def __init__(self, shift):
        self._event = shift.event
        self.parent_shift_id = shift.parent_shift_id
        self.starts_at, self.ends_at = shift.starts_at, shift.ends_at

    def __getattr__(self, name):
        return getattr(self._event, name)


def interval_event(shift):
    return shift.event if shift.parent_shift_id is None else IntervalEvent(shift)
