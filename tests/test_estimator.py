from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)  # 10:00 AM New York
T = NOW.timestamp()


def runs(est, pattern, minutes=10):
    out = []
    for i, ok in enumerate(pattern):
        out.append(est.Run(started_at=T - (len(pattern) - i) * 1800, duration_s=minutes * 60, ok=ok))
    return out


def test_basic_range(pkg):
    from eta_plugin import estimator as e
    stats = e.lane_stats(runs(e, [True] * 8 + [False, True]), T)
    assert stats.success_rate == 0.9 and stats.fail_streak == 0
    r = e.estimate(remaining_items=9, stats=stats, lanes=1, now=NOW, tz=ZoneInfo("America/New_York"))
    assert r.status == "ok"
    assert r.runs_needed == 10
    assert r.likely_at < r.worst_at
    assert r.headline.startswith("Likely between") and "today" in r.headline
    assert r.likely_at.tzinfo is not None


def test_parallel_lanes_cut_time(pkg):
    from eta_plugin import estimator as e
    one = e.estimate(remaining_items=10, avg_run_s=600, success_rate=1.0, lanes=1, now=NOW)
    two = e.estimate(remaining_items=10, avg_run_s=600, success_rate=1.0, lanes=2, now=NOW)
    assert (two.likely_at - NOW) * 2 == (one.likely_at - NOW)


def test_fail_streak_is_unbounded(pkg):
    from eta_plugin import estimator as e
    stats = e.lane_stats(runs(e, [True, True, False, False, False]), T)
    r = e.estimate(remaining_items=3, stats=stats, now=NOW)
    assert r.status == "unbounded" and r.likely_at is None
    assert "No honest ETA" in r.headline


def test_low_success_is_unbounded(pkg):
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=3, avg_run_s=60, success_rate=0.1, now=NOW)
    assert r.status == "unbounded"


def test_waiting_on_person(pkg):
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=2, avg_run_s=600, success_rate=1.0, now=NOW,
                   waiting_on_person="you approve the PR")
    assert r.status == "blocked_on_person"
    assert r.headline.startswith("Clock starts when you approve the PR")


def test_daily_cap_pushes_past_reset(pkg):
    from eta_plugin import estimator as e
    tz = ZoneInfo("America/New_York")
    r = e.estimate(remaining_items=10, avg_run_s=600, success_rate=1.0, runs_left_today=3,
                   daily_run_cap=5, reset_hour=0, now=NOW, tz=tz)
    # 3 today, then 5 tomorrow, then 2 the day after
    assert (r.likely_at.date() - NOW.astimezone(tz).date()).days == 2
    assert "cap" in r.biggest_risk


def test_zero_cap_does_not_loop_forever(pkg):
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=5, avg_run_s=60, success_rate=1.0, daily_run_cap=0, now=NOW)
    assert r.likely_at is not None  # bounded by the 366-day guard


def test_no_data(pkg):
    from eta_plugin import estimator as e
    assert e.estimate(remaining_items=3, now=NOW).status == "no_data"


def test_done(pkg):
    from eta_plugin import estimator as e
    assert e.estimate(remaining_items=0, now=NOW).status == "done"


def test_queue_ahead_counts(pkg):
    from eta_plugin import estimator as e
    a = e.estimate(remaining_items=1, avg_run_s=600, success_rate=1.0, now=NOW)
    b = e.estimate(remaining_items=1, avg_run_s=600, success_rate=1.0, queue_ahead=5, now=NOW)
    assert b.likely_at - a.likely_at == (b.likely_at - NOW) * 5 / 6
    assert "queued ahead" in b.biggest_risk


def test_window_falls_back_to_all_history(pkg):
    from eta_plugin import estimator as e
    old = [e.Run(started_at=T - 5 * 86400, duration_s=300, ok=True)]
    s = e.lane_stats(old, T)
    assert s.success_rate == 1.0 and s.runs_24h == 0 and s.median_duration_s == 300
