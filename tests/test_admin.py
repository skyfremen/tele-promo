import copy
import pytest
from promo.admin import parse_shorts, update_settings, summary
from promo.store import validate_state


def base_state():
    return {
        "version": 1,
        "config": {"dry_run": False, "keywords": ["original comedy"],
                   "shorts": [], "approved_groups": [],
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


def test_management_preserves_history_approvals_and_enforces_dry_run():
    prior = base_state()
    prior["config"]["approved_groups"] = [
        {"id": 123, "approved": True, "cooldown_hours": 48}]
    updated = update_settings(prior, "https://youtu.be/abcdefghijk",
                              "original comedy, youtube promotion")
    assert updated["config"]["dry_run"] is True
    assert len(updated["config"]["shorts"]) == 1
    assert len(updated["config"]["keywords"]) == 2
    assert updated["config"]["approved_groups"] == prior["config"]["approved_groups"]
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
