from polymarket_weather_arb.domain.polymarket_resolution import parse_resolution_state


def test_parse_resolution_state_unresolved():
    payload = {
        "closed": False,
        "umaResolutionStatus": "unresolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.4", "0.6"]',
    }
    state = parse_resolution_state(payload)
    assert not state.is_resolved
    assert state.resolved_outcome is None


def test_parse_resolution_state_resolved_yes():
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
    }
    state = parse_resolution_state(payload)
    assert state.is_resolved
    assert state.resolved_outcome == "yes"


def test_parse_resolution_state_accepts_decimal_winner_price():
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '[" Yes ", "No"]',
        "outcomePrices": '["0.999", "0.001"]',
    }
    state = parse_resolution_state(payload)
    assert state.is_resolved
    assert state.resolved_outcome == "yes"


def test_parse_resolution_state_resolved_no():
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0", "1"]',
    }
    state = parse_resolution_state(payload)
    assert state.is_resolved
    assert state.resolved_outcome == "no"


def test_parse_resolution_state_rejects_non_yes_no_winner_labels():
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Above", "Below"]',
        "outcomePrices": '["1", "0"]',
    }
    state = parse_resolution_state(payload)
    assert not state.is_resolved
    assert state.resolved_outcome is None


def test_parse_resolution_state_rejects_multiple_winner_prices():
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.99", "0.99"]',
    }
    state = parse_resolution_state(payload)
    assert not state.is_resolved
    assert state.resolved_outcome is None


def test_parse_resolution_state_disputed():
    payload = {
        "closed": True,
        "umaResolutionStatus": "disputed",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0", "1"]',
    }
    state = parse_resolution_state(payload)
    assert not state.is_resolved
    assert state.resolved_outcome is None


def test_parse_resolution_state_bad_json():
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": "invalid",
        "outcomePrices": "invalid",
    }
    state = parse_resolution_state(payload)
    assert not state.is_resolved
    assert state.resolved_outcome is None
