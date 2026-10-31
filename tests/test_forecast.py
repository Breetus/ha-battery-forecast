from datetime import datetime, timedelta, timezone

from custom_components.energy_planner import forecast as fc

from .helpers import BNE


def _amber(start, minutes, channel, cents, kind="ForecastInterval", predicted=None):
    item = {
        "type": kind,
        "channelType": channel,
        "startTime": (start + timedelta(seconds=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "endTime": (start + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "perKwh": cents,
    }
    if predicted is not None:
        item["advancedPrice"] = {"low": 0, "predicted": predicted, "high": 0}
    return item


def test_parse_amber_raw_api():
    t = datetime(2026, 10, 30, 10, 0, tzinfo=BNE)
    imports, exports = fc.parse_amber(
        [
            _amber(t, 30, "general", 20.0, predicted=18.0),
            _amber(t, 30, "feedIn", -7.0),
            _amber(t, 5, "general", 25.0, kind="CurrentInterval"),
        ]
    )
    assert imports[0].start == t
    assert imports[0].end == t + timedelta(minutes=5)  # 5-min actual preferred
    assert abs(imports[0].value - 0.25) < 1e-9
    assert abs(exports[0].value - 0.07) < 1e-9  # earning is positive


def test_parse_amber_uses_advanced_price_for_forecasts():
    t = datetime(2026, 10, 30, 10, 0, tzinfo=BNE)
    imports, _ = fc.parse_amber([_amber(t, 30, "general", 20.0, predicted=18.0)])
    assert abs(imports[0].value - 0.18) < 1e-9


def test_parse_ha_amber_attributes():
    imports, _ = fc.parse_amber(
        [{"start_time": "2026-10-30T00:00:01+00:00", "end_time": "2026-10-30T00:30:00+00:00", "per_kwh": 0.21}]
    )
    assert abs(imports[0].value - 0.21) < 1e-9


def test_parse_solcast_shapes():
    api = fc.parse_solar([{"period_end": "2026-10-30T02:00:00Z", "period": "PT30M", "pv_estimate": 4.2}])
    ha = fc.parse_solar([{"period_start": "2026-10-30T01:30:00+00:00", "pv_estimate": 4.2}])
    assert api[0].start == ha[0].start
    assert api[0].end == ha[0].end


def test_build_slots_first_slot_is_partial_and_uses_5min_price():
    t = datetime(2026, 10, 30, 10, 0, tzinfo=BNE)
    raw = [_amber(t + timedelta(minutes=30 * i), 30, "general", 20 + i) for i in range(6)]
    raw += [_amber(t + timedelta(minutes=30 * i), 30, "feedIn", -5) for i in range(6)]
    raw.append(_amber(t + timedelta(minutes=10), 5, "general", 50, kind="CurrentInterval"))
    imports, exports = fc.parse_amber(raw)
    now = t + timedelta(minutes=12)
    slots = fc.build_slots(now, imports, exports, [], [1.0] * 48, BNE)
    # The live 5-minute price gets its own slot (10:12-10:15), undiluted.
    assert abs(slots[0].hours - 3 / 60) < 1e-6
    assert abs(slots[0].import_price - 0.50) < 1e-6
    assert slots[1].start == t + timedelta(minutes=15)
    assert abs(slots[1].import_price - 0.20) < 1e-6
    assert slots[2].start == t + timedelta(minutes=30)
    assert slots[-1].start + timedelta(hours=slots[-1].hours) == t + timedelta(hours=3)


def test_deadlines_skip_one_already_passed():
    now = datetime(2026, 10, 30, 17, 0, tzinfo=BNE)
    dls = fc.deadlines(now, BNE, 16, 0)
    assert dls[0] == datetime(2026, 10, 31, 16, 0, tzinfo=BNE)


def test_profile_from_samples():
    t = datetime(2026, 10, 30, 0, 0, tzinfo=BNE)
    samples = [(t + timedelta(minutes=30 * i), 2.0 if i == 3 else 1.0) for i in range(48)]
    profile = fc.profile_from_samples(samples, BNE)
    assert profile[3] == 2.0 and profile[4] == 1.0


def test_parse_amber_express_series():
    t = datetime(2026, 10, 30, 10, 0, tzinfo=BNE)
    items = [{"time": (t + timedelta(minutes=5 * i)).isoformat(), "value": 0.10 + i / 100} for i in range(3)]
    items.append({"time": (t + timedelta(minutes=30)).isoformat(), "value": 0.30})
    out = fc.parse_series(items)
    assert [i.end - i.start for i in out] == [timedelta(minutes=5)] * 2 + [timedelta(minutes=20), timedelta(minutes=30)]
    assert out[-1].value == 0.30


def test_profile_from_changes_is_time_weighted():
    t = datetime(2026, 10, 30, 0, 0, tzinfo=BNE)
    # 0:00-0:10 at 3 kW, 0:10-0:30 at 0 kW; 0:30-1:00 unavailable; then 2 kW until 1:30.
    changes = [(t, 3.0), (t + timedelta(minutes=10), 0.0), (t + timedelta(minutes=30), None), (t + timedelta(hours=1), 2.0)]
    profile = fc.profile_from_changes(changes, t + timedelta(minutes=90), BNE)
    assert abs(profile[0] - 1.0) < 1e-6
    assert profile[2] == 2.0
    # Bin 1 had no data, so it gets the overall mean.
    assert abs(profile[1] - (3 * 10 + 2 * 30) / 60) < 1e-3


def test_parse_solar_cautious_estimate():
    item = {"period_start": "2026-10-30T01:30:00+00:00", "pv_estimate": 4.2, "pv_estimate10": 2.0}
    assert fc.parse_solar([item], key="pv_estimate10")[0].value == 2.0
    # Falls back to the median when the percentile is missing.
    assert fc.parse_solar([{"period_start": "2026-10-30T01:30:00+00:00", "pv_estimate": 4.2}], key="pv_estimate10")[0].value == 4.2


def test_flagged_spike_plans_on_the_dear_edge():
    t = datetime(2026, 10, 30, 17, 0, tzinfo=BNE)
    general = _amber(t, 30, "general", 30.0, predicted=30.0)
    general["advancedPrice"]["high"] = 200.0
    general["spikeStatus"] = "potential"
    feed_in = _amber(t, 30, "feedIn", -20.0, predicted=-20.0)
    feed_in["advancedPrice"]["low"] = -180.0
    feed_in["spikeStatus"] = "potential"
    imports, exports = fc.parse_amber([general, feed_in])
    assert abs(imports[0].value - 2.00) < 1e-9
    assert abs(exports[0].value - 1.80) < 1e-9


def test_live_price_beats_forecast_for_the_same_interval():
    # PowerSync's merged list repeats the current 5 minutes as a forecast, and
    # the forecast comes first. The live price must win either way round.
    t = datetime(2026, 10, 30, 21, 50, tzinfo=BNE)
    forecast = [_amber(t, 5, "general", 17.9, predicted=17.9), _amber(t, 5, "feedIn", -7.63, predicted=-7.63)]
    current = [_amber(t, 5, "general", 19.39, kind="CurrentInterval"), _amber(t, 5, "feedIn", -8.99, kind="CurrentInterval")]
    for items in (forecast + current, current + forecast):
        imports, exports = fc.parse_amber(items)
        assert abs(imports[0].value - 0.1939) < 1e-9
        assert abs(exports[0].value - 0.0899) < 1e-9


def _solar_slots(low, high):
    from custom_components.energy_planner.planner import Slot

    t = datetime(2026, 10, 31, 11, 45, tzinfo=BNE)
    return [Slot(t + timedelta(minutes=30 * i), 0.5, 0.1, 0.05, lo, 1.0, hi) for i, (lo, hi) in enumerate(zip(low, high))]


def test_live_solar_starts_both_forecasts_and_fades():
    slots = _solar_slots([2.0] * 30, [4.0] * 30)
    fc.apply_live_solar(slots, 5.0)
    assert slots[0].solar_kw == 5.0 and slots[0].solar_high_kw == 5.0
    # The dull day is lifted near term and fades back to its forecast.
    assert 2.0 < slots[8].solar_kw < slots[1].solar_kw <= 5.0
    assert 4.0 < slots[1].solar_high_kw < 5.0
    # Tomorrow (from 00:15 at slot 25) is left alone.
    assert slots[25].solar_kw == 2.0 and slots[25].solar_high_kw == 4.0


def test_live_solar_only_raises_when_the_panels_may_be_throttled():
    slots = _solar_slots([2.0] * 4, [4.0] * 4)
    fc.apply_live_solar(slots, 1.0, only_up=True)
    assert [s.solar_kw for s in slots] == [2.0] * 4
    assert [s.solar_high_kw for s in slots] == [4.0] * 4
