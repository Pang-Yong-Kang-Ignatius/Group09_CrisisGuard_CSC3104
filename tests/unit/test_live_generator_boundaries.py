import math

import pytest

from evaluation.experiments.run_recovery_matrix import (
    missed_live_opportunities, wait_for_live_opportunity,
)


class Timer:
    def __init__(self, now=0, wakes=(), cancel=False):
        self.now = now
        self.wakes = iter(wakes)
        self.cancel = cancel
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, duration):
        self.waits.append(duration)
        self.now = next(self.wakes)
        self.stopped = self.cancel
        return self.stopped


def test_early_timer_wake_does_not_dispatch_before_due():
    timer = Timer(now=100, wakes=(100.09, 100.10, 100.11))
    assert wait_for_live_opportunity(timer, 100, .1, lambda: timer.now)
    assert timer.now - 100 >= .1
    assert len(timer.waits) >= 2


def test_late_scheduling_dispatches_without_another_wait():
    timer = Timer(now=101)
    assert wait_for_live_opportunity(timer, 100, .5, lambda: timer.now)
    assert not timer.waits


def test_stop_during_wait_does_not_invent_a_request():
    timer = Timer(wakes=(.12,), cancel=True)
    assert not wait_for_live_opportunity(timer, 0, .1, lambda: timer.now)
    # Due .1 preceded stop .12: this is a real unissued opportunity.
    assert missed_live_opportunities(1, .12, .12, 10) == (0, 1)


def test_already_stopped_never_dispatches_even_when_late():
    timer = Timer(now=1)
    timer.stopped = True
    assert not wait_for_live_opportunity(timer, 0, .1, lambda: timer.now)
    assert not timer.waits


@pytest.mark.parametrize('end,recorded,missing', [
    (0, 0, 0), (.1, 1, 0), (.099, 1, 0), (.101, 1, 1),
    (.31, 1, 3),  # several overdue requests remain skips, not just the last one
])
def test_half_open_stop_boundary_and_genuine_late_requests(end, recorded, missing):
    assert missed_live_opportunities(recorded, end, end, 10) == (0, missing)


@pytest.mark.parametrize('end,attempted,target', [
    (131.09400000000096, 4719, 4720),
    (113.125, 4072, 4073),
])
def test_retained_36_rps_trials_really_missed_last_opportunity(end, attempted, target):
    assert (attempted - 1) / 36 < attempted / 36 < end
    assert math.ceil(end * 36) == target
    assert missed_live_opportunities(attempted, end, end, 36) == (0, 1)


def test_already_counted_cap_skip_is_not_counted_again_as_lag():
    # Four target opportunities, two actual attempts and one known cap skip.
    assert missed_live_opportunities(2 + 1, .4, .4, 10) == (0, 1)
    assert missed_live_opportunities(2 + 1 + 1, .4, .4, 10) == (0, 0)


def test_exact_due_clock_waits_instead_of_issuing_at_tied_stop():
    timer = Timer(now=24.25, wakes=(24.25,), cancel=True)
    assert not wait_for_live_opportunity(timer, 0, 24.25, lambda: timer.now)
    assert timer.waits and timer.waits[0] > 0


def test_boundary_opportunity_can_issue_only_after_clock_advances():
    timer = Timer(now=24.25, wakes=(24.251,))
    assert wait_for_live_opportunity(timer, 0, 24.25, lambda: timer.now)
    assert 2037/84 < timer.now
    assert math.ceil(timer.now*84)==2038


def test_stop_clock_cannot_be_captured_during_dispatch():
    import threading
    from evaluation.experiments.run_recovery_matrix import close_live_window
    lock=threading.Lock()
    stop=threading.Event()
    attempted=threading.Event()
    sampled=threading.Event()
    window={'end':None}
    output={}
    clock_value=[24.25]
    def clock():
        sampled.set()
        return clock_value[0]
    def close():
        attempted.set()
        close_live_window(window,lock,stop,0,output,clock)
    with lock:
        worker=threading.Thread(target=close)
        worker.start()
        assert attempted.wait(1)
        assert not sampled.wait(.02)
        assert not stop.is_set()
        # Dispatch finishes before stop is allowed to sample the clock.
        clock_value[0]=24.251
    worker.join(1)
    assert not worker.is_alive() and stop.is_set()
    assert window['end']==output['live_generator_stop_seconds']==24.251
    close_live_window(window,lock,stop,0,output,lambda:99)
    assert window['end']==24.251  # cleanup must not move the window


def test_original_boundary_overissue_stays_invalid():
    from evaluation.experiments.analyze_recovery_matrix import generator_accuracy
    trial=dict(configuration={'live_read_rps':84},live_generator_stop_seconds=24.25,
               live_target_opportunities=2037,live_generator_lag_skips=0,
               live_reads=[{'offered_index':i+1} for i in range(2038)])
    assert not generator_accuracy(trial)['ledger_reconciled']
