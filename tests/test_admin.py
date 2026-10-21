import copy
import pytest
from promo.admin import parse_shorts, update_settings, summary, set_live
from promo.store import validate_state


def base_state():
    return {
        "version": 1,
        "config": {"dry_run": False, "keywords": ["original comedy"],
                   "shorts": [],
                   "limits": {"discovery_results": 5, "joins_per_run": 1,
                              "posts_per_run": 1, "posts_per_day": 2,
                              "cooldown_hours": 48}},
        "groups": {"123": {"status": "eligible"}},
        "posts": {"123:abcdefghijk": {"group_id": 123, "at": 1800000000,
                                      "status": "confirmed"}},
        "blocked_until": 0,
    }


def test_parse_shorts_normalizes_and_deduplicates():
    links = "https://youtu.be/abcdefghijk?si=track, https://www.youtube.com/shorts/abcdefghijk\nhttps://youtube.com/shorts/123456789ab"
    rows = parse_shorts(links)
    assert len(rows) == 2
    assert rows[0]["url"] == "https://www.youtube.com/shorts/abcdefghijk"
    assert rows[1]["caption"]


def test_parse_shorts_rejects_untrusted_link():
    with pytest.raises(ValueError):
        parse_shorts("https://youtube.com.attacker.invalid/shorts/abcdefghijk")
    with pytest.raises(ValueError):
        parse_shorts("")


def test_management_preserves_history_and_current_live_setting():
    prior = base_state()
    updated = update_settings(prior, "https://youtu.be/abcdefghijk",
                              "original comedy, youtube promotion")
    assert updated["config"]["dry_run"] is False
    assert len(updated["config"]["shorts"]) == 1
    assert len(updated["config"]["keywords"]) == 2
    assert updated["groups"] == prior["groups"]
    assert updated["posts"] == prior["posts"]
    assert prior["config"]["dry_run"] is False
    assert validate_state(updated) == updated
    assert "abcdefghijk" not in summary(updated)


def test_management_rejects_invalid_keywords_and_noop():
    with pytest.raises(ValueError):
        update_settings(base_state(), "", "")
    with pytest.raises(ValueError):
        update_settings(base_state(), keywords="a")

def test_activate_and_pause_are_explicit_without_checkbox():
    state = base_state()
    with pytest.raises(ValueError):
        set_live(state, True)  # A Shorts URL must be configured before activation.
    state["config"]["shorts"] = parse_shorts("https://youtu.be/abcdefghijk")
    ready = set_live(state, True)
    assert ready["config"]["dry_run"] is False
    assert state["config"]["dry_run"] is False
    stopped = set_live(ready, False)
    assert stopped["config"]["dry_run"] is True
    assert set_live(stopped, True)["config"]["dry_run"] is False
    assert state["config"]["dry_run"] is False


def test_live_activation_without_group_selection():
    state = base_state()
    state["config"]["shorts"] = parse_shorts("https://youtu.be/abcdefghijk")
    state["groups"] = {}
    assert set_live(state, True)["config"]["dry_run"] is False


def test_configure_keeps_paused_state_until_explicit_activation():
    state = base_state()
    state["config"]["dry_run"] = True
    updated = update_settings(state, keywords="comedy, youtube")
    assert updated["config"]["dry_run"] is True
    assert state["config"]["dry_run"] is True
