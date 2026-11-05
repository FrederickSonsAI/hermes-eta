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
    assert r.runs_needed == 9  # typical case: most runs succeed, so one run per item
    assert (r.likely_at - NOW).total_seconds() == 9 * 600
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


def test_zero_cap_is_unbounded(pkg):
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=5, avg_run_s=60, success_rate=1.0, daily_run_cap=0, now=NOW)
    assert r.status == "unbounded"


def test_tiny_cap_over_a_year_is_unbounded(pkg):
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=1000, avg_run_s=60, success_rate=1.0, daily_run_cap=1, now=NOW)
    assert r.status == "unbounded" and "year" in r.headline


def test_far_dates_show_year(pkg):
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=30, avg_run_s=60, success_rate=1.0, daily_run_cap=2, now=NOW)
    assert "2026" in r.headline


def test_dst_change_is_exact(pkg):
    from datetime import timedelta
    from eta_plugin import estimator as e
    tz = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)  # 00:30 EDT; clocks fall back at 2 AM
    r = e.estimate(remaining_items=18, avg_run_s=600, success_rate=1.0, now=start, tz=tz)
    assert r.likely_at - start == timedelta(hours=3)
    assert r.likely_at.strftime("%H:%M %Z") == "02:30 EST"


def test_allowance_without_daily_cap_resumes_after_reset(pkg):
    from eta_plugin import estimator as e
    tz = ZoneInfo("America/New_York")
    r = e.estimate(remaining_items=4, avg_run_s=600, success_rate=1.0, runs_left_today=2,
                   reset_hour=0, now=NOW, tz=tz)
    assert r.likely_at.astimezone(tz).date() > NOW.astimezone(tz).date()


def test_same_time_range_reads_cleanly(pkg):
    from eta_plugin import estimator as e
    now = NOW.astimezone(ZoneInfo("America/New_York"))
    assert e.describe_range(now, now, now).startswith("Likely around")


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


def test_cap_never_beats_no_cap(pkg):
    from eta_plugin import estimator as e
    free = e.estimate(remaining_items=300, avg_run_s=1800, success_rate=1.0, now=NOW)
    capped = e.estimate(remaining_items=300, avg_run_s=1800, success_rate=1.0, daily_run_cap=100, now=NOW)
    assert capped.likely_at >= free.likely_at


def test_running_run_is_credited(pkg):
    from datetime import timedelta
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=1, avg_run_s=600, success_rate=1.0, running_elapsed_s=[300], now=NOW)
    assert r.likely_at - NOW == timedelta(seconds=300)
    three = e.estimate(remaining_items=3, avg_run_s=600, success_rate=1.0, running_elapsed_s=[300],
                       now=NOW)
    assert three.likely_at - NOW == timedelta(seconds=1500)


def test_elapsed_only_gives_labelled_rough_range(pkg):
    from datetime import timedelta
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=1, running_elapsed_s=[1200], now=NOW)
    assert r.status == "ok" and r.confidence == "rough" and r.headline.startswith("Rough guess")
    # likely: as long again; worst: the slow end (3x elapsed more) plus one retry, since nothing
    # says how often this work fails: two slow runs of 80 min, less the 20 already run
    assert r.likely_at - NOW == timedelta(minutes=20) and r.worst_at - NOW == timedelta(minutes=140)
    assert e.estimate(remaining_items=1, running_elapsed_s=[10], now=NOW).status == "no_data"


def test_few_runs_widen_worst_case_and_say_so(pkg):
    from eta_plugin import estimator as e
    few = e.estimate(remaining_items=4, stats=e.lane_stats(runs(e, [True, True]), T), now=NOW)
    lots = e.estimate(remaining_items=4, stats=e.lane_stats(runs(e, [True] * 12), T), now=NOW)
    assert few.worst_at > lots.worst_at and few.confidence == "low"
    assert "Based on 2 runs, so the worst case is wide." in few.headline
    assert lots.headline.endswith("Based on 12 runs.")


def test_one_failed_run_is_not_unbounded(pkg):
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=1, stats=e.lane_stats(runs(e, [False]), T), now=NOW)
    assert r.status == "ok" and 0.15 < r.key_numbers["success_rate_used"] < 0.8
    assert r.key_numbers["success_rate"] == 0.0


# The likely case is the typical one: retries for failures belong in the worst case only.

def test_one_item_with_prior_is_one_run(pkg):
    from eta_plugin import estimator as e
    stats = e.lane_stats(runs(e, [True, True], minutes=4), T)  # 2 runs: prior pulls rate to 0.9
    r = e.estimate(remaining_items=1, stats=stats, now=NOW)
    assert r.key_numbers["success_rate_used"] < 1
    assert r.runs_needed == 1
    assert (r.likely_at - NOW).total_seconds() == 4 * 60
    assert r.worst_at > r.likely_at


def test_one_item_with_80pct_prior_rate(pkg):
    from eta_plugin import estimator as e
    stats = e.lane_stats(runs(e, [False, True, True], minutes=3), T)
    # 2 of 3 ok, shrunk with 2 prior runs at 0.8: (2 + 1.6) / 5 = 0.72
    r = e.estimate(remaining_items=1, stats=stats, now=NOW)
    assert round(r.key_numbers["success_rate_used"], 2) == 0.72
    assert r.runs_needed == 1 and (r.likely_at - NOW).total_seconds() == 180
    r = e.estimate(remaining_items=1, stats=stats, success_rate=0.8, avg_run_s=300, now=NOW)
    assert r.runs_needed == 1 and (r.likely_at - NOW).total_seconds() == 300
    assert r.worst_at > r.likely_at


def test_one_item_with_half_observed_success(pkg):
    from eta_plugin import estimator as e
    stats = e.lane_stats(runs(e, [True, False] * 5, minutes=5), T)
    assert stats.success_rate == 0.5 and stats.samples == 10 and stats.fail_streak == 1
    r = e.estimate(remaining_items=1, stats=stats, now=NOW)
    assert r.status == "ok" and r.runs_needed == 1
    assert (r.likely_at - NOW).total_seconds() == 300      # the typical item: one run
    assert (r.worst_at - NOW).total_seconds() >= 3 * 300   # retries: rate 0.35 -> 3 runs


def test_low_rate_uses_expected_runs_unrounded(pkg):
    from eta_plugin import estimator as e
    r = e.estimate(remaining_items=1, avg_run_s=600, success_rate=0.4, now=NOW)
    assert (r.likely_at - NOW).total_seconds() == 2.5 * 600   # 1 / 0.4, not ceil -> 3
    assert r.key_numbers["runs_expected"] == 2.5 and r.runs_needed == 3
    assert r.worst_at > r.likely_at
    r = e.estimate(remaining_items=1, avg_run_s=600, success_rate=0.4, lanes=4, now=NOW)
    assert (r.likely_at - NOW).total_seconds() == 600   # a spare lane takes the fraction


def test_rounds_helper(pkg):
    from eta_plugin import estimator as e
    assert e._rounds(0, 2) == 0 and e._rounds(3, 1) == 3 and e._rounds(3, 2) == 2
    assert e._rounds(2.5, 1) == 2.5 and e._rounds(6.5, 4) == 2 and e._rounds(4.5, 4) == 1.5
    assert e.likely_runs(3, 0.8) == 3 and e.likely_runs(3, 0.25) == 12 and e.likely_runs(0, 0.2) == 0


def test_no_honest_eta_still_fires_when_work_keeps_failing(pkg):
    from eta_plugin import estimator as e
    stats = e.lane_stats(runs(e, [True, False, False, False]), T)
    r = e.estimate(remaining_items=1, stats=stats, now=NOW)
    assert r.status == "unbounded" and "No honest ETA" in r.headline and r.likely_at is None
    stats = e.lane_stats(runs(e, [False] * 9 + [True, False]), T)  # 1 of 11 ok, no 3-run streak
    assert stats.fail_streak < e.STUCK_STREAK and stats.success_rate < e.MIN_SUCCESS_RATE
    r = e.estimate(remaining_items=1, stats=stats, now=NOW)
    assert r.status == "unbounded" and "No honest ETA" in r.headline
    r = e.estimate(remaining_items=1, avg_run_s=60, success_rate=0.1, now=NOW)
    assert r.status == "unbounded" and "No honest ETA" in r.headline
