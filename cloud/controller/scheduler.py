
from dataclasses import dataclass
from collections import deque


@dataclass
class Ticket:
    hospital_id: str
    event_id: str
    priority: int
    bypass_count: int = 0


class ControllerScheduler:
    """One waiting ticket per hospital; larger priorities are more urgent.

    B=0 is FIFO, becoming round robin when served hospitals rejoin the tail.
    For B>0 a ticket can be overtaken at most B times. With p predecessors,
    it is served within p+B+1 successful selections, assuming finite queue
    length at registration, tail-only arrivals, continued selections and no
    external removal/reset of its ticket. This bounds selections, not time
    or completed replay. Callers must serialize scheduler mutations.
    """
    def __init__(self, bypass_limit=0):
        if bypass_limit < 0:
            raise ValueError("Bypass limit cannot be negative")

        self.queue = deque()
        self.bypass_limit = bypass_limit

    def add_hospital(self, hospital_id, event_id, priority):
        # A hospital can have only one waiting ticket
        for ticket in self.queue:
            if ticket.hospital_id == hospital_id:
                return

        new_ticket = Ticket(
            hospital_id=hospital_id,
            event_id=event_id,
            priority=priority
        )

        self.queue.append(new_ticket)

    def update_priority(self, hospital_id, priority):
        # Update priority without changing queue position
        # or resetting bypass count
        for ticket in self.queue:
            if ticket.hospital_id == hospital_id:
                ticket.priority = priority
                return True

        return False

    def next_hospital(self):
        if not self.queue:
            return None

        # Find the highest-priority ticket.
        # A tie keeps the earlier ticket.
        highest_index = 0

        for i in range(1, len(self.queue)):
            if self.queue[i].priority > self.queue[highest_index].priority:
                highest_index = i

        # Check whether the highest-priority ticket
        # can bypass every ticket before it.
        can_bypass = all(
            self.queue[i].bypass_count < self.bypass_limit
            for i in range(highest_index)
        )

        # If bypass is blocked, serve the queue head.
        selected_index = highest_index if can_bypass else 0

        # Increase bypass counts for overtaken tickets.
        for i in range(selected_index):
            self.queue[i].bypass_count += 1

        # Remove the selected ticket.
        selected_ticket = self.queue[selected_index]
        del self.queue[selected_index]

        return selected_ticket



if __name__ == "__main__":
    scheduler = ControllerScheduler(bypass_limit=1)

    scheduler.add_hospital("Hospital A", "A001", 1)
    scheduler.add_hospital("Hospital B", "B001", 5)

    first = scheduler.next_hospital()
    print("First:", first.hospital_id)

    print("Remaining queue:", list(scheduler.queue))

    second = scheduler.next_hospital()
    print("Second:", second.hospital_id)
