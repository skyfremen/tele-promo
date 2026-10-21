"""Phone-friendly private configuration commands for Actions workflow dispatch.

Never prints group names, URLs, credentials, captions or approval evidence.
"""
import copy
import os
import re
import sys

import yaml

from .core import validate_config, video_id
from .store import GitHubStore


DEFAULT_CAPTION = "Watch my original YouTube Short!"


def parse_shorts(value):
    items = []
    seen = set()
    for raw in re.split(r"[,;\n]+", value):
        url = raw.strip()
        if not url:
            continue
        identity = video_id(url)
        if identity in seen:
            continue
        seen.add(identity)
        items.append({"url": "https://www.youtube.com/shorts/" + identity,
                      "caption": DEFAULT_CAPTION})
    if not 1 <= len(items) <= 30:
        raise ValueError("Provide between 1 and 30 unique Shorts URLs")
    return items


def update_settings(state, urls="", keywords="", min_group_members=""):
    if not urls.strip() and not keywords.strip() and not str(min_group_members).strip():
        raise ValueError("Provide Shorts links and/or search keywords")
    revised = copy.deepcopy(state)
    config = copy.deepcopy(revised["config"])
    if urls.strip():
        config["shorts"] = parse_shorts(urls)
    if keywords.strip():
        config["keywords"] = [item.strip() for item in keywords.split(",") if item.strip()]
    if str(min_group_members).strip():
        value = str(min_group_members).strip()
        if not value.isdecimal() or int(value) > 1000000:
            raise ValueError("Minimum group members must be an integer between 0 and 1000000")
        config.setdefault("limits", {})["min_group_members"] = int(value)
    # Changing settings never silently enables live Telegram mutations.
    config["dry_run"] = True
    revised["config"] = validate_config(config)
    return revised


def select_groups(state, raw, confirmed=False):
    """Replace the selection with known group IDs after external permission."""
    if not confirmed:
        raise ValueError("Confirm permission was obtained outside this automation")
    ids = []
    for item in raw.replace(";", ",").split(","):
        token = item.strip()
        if not token:
            continue
        if not token.isdecimal() or int(token) <= 0:
            raise ValueError("Group IDs must be positive integers")
        ids.append(int(token))
    if not ids or len(ids) != len(set(ids)) or len(ids) > 100:
        raise ValueError("Select one to 100 unique group IDs")
    if any(str(gid) not in state["groups"] for gid in ids):
        raise ValueError("All selected groups must first be discovered in private state")
    revised = copy.deepcopy(state)
    revised["config"]["selected_groups"] = ids
    # Changing group selection never silently starts live posting.
    revised["config"]["dry_run"] = True
    revised["config"] = validate_config(revised["config"])
    return revised


def clear_selection(state):
    revised = copy.deepcopy(state)
    revised["config"]["selected_groups"] = []
    revised["config"]["dry_run"] = True
    revised["config"] = validate_config(revised["config"])
    return revised


def set_live(state, enable, confirmed=False, switch_enabled=False):
    revised = copy.deepcopy(state)
    if enable:
        if not confirmed or not switch_enabled:
            raise ValueError("Live posting requires explicit confirmation and LIVE_ENABLED")
        if not revised["config"]["shorts"]:
            raise ValueError("Configure at least one Shorts URL before activation")
        selected = revised["config"].get("selected_groups", [])
        if not selected or any(str(gid) not in revised["groups"] for gid in selected):
            raise ValueError("Select discovered groups after obtaining permission separately")
    revised["config"]["dry_run"] = not enable
    revised["config"] = validate_config(revised["config"])
    return revised


def summary(state):
    if state is None:
        return "Private state: not initialized"
    config = validate_config(state["config"])
    return ("Private state: initialized; mode: " +
            ("dry-run" if config["dry_run"] else "live-capable") +
            "; shorts: " + str(len(config["shorts"])) +
            "; discovered groups: " + str(len(state["groups"])) +
            "; existing post records: " + str(len(state["posts"])) +
            "; selected groups: " + str(len(config.get("selected_groups", []))) +
            "; minimum new-group members: " + str(config["limits"].get("min_group_members", 100)))


def main():
    action = os.environ.get("MOBILE_ACTION", "status")
    if action not in ("status", "bootstrap", "configure", "select_groups", "clear_selection", "activate", "pause"):
        raise ValueError("Unsupported management operation")
    if not os.environ.get("STATE_TOKEN") or not os.environ.get("STATE_REPO"):
        raise ValueError("Configure STATE_TOKEN and STATE_REPO in GitHub Secrets")
    store = GitHubStore()
    if action == "bootstrap" and store.state is None:
        with open("config.yaml", encoding="utf-8") as source:
            config = validate_config(yaml.safe_load(source))
        config["dry_run"] = True
        store.save({"version": 1, "config": config, "groups": {}, "posts": {},
                    "blocked_until": 0})
    elif action == "configure":
        if store.state is None:
            raise ValueError("Bootstrap private state before configuring")
        updated = update_settings(store.state, os.environ.get("MOBILE_SHORTS", ""),
                                  os.environ.get("MOBILE_KEYWORDS", ""),
                                  os.environ.get("MOBILE_MIN_GROUP_MEMBERS", ""))
        store.save(updated)
    elif action in ("select_groups", "clear_selection"):
        if store.state is None:
            raise ValueError("Bootstrap private state first")
        if action == "select_groups":
            updated = select_groups(store.state,
                                    os.environ.get("MOBILE_GROUP_IDS", ""),
                                    confirmed=os.environ.get("MOBILE_CONFIRM_PERMISSION") == "true")
        else:
            updated = clear_selection(store.state)
        store.save(updated)
    elif action in ("activate", "pause"):
        if store.state is None:
            raise ValueError("Bootstrap private state first")
        updated = set_live(store.state, action == "activate",
                           confirmed=os.environ.get("MOBILE_CONFIRM_LIVE") == "true",
                           switch_enabled=os.environ.get("LIVE_ENABLED") == "true")
        store.save(updated)
    print(summary(store.state))
    print("Telegram session secret: " +
          ("configured" if os.environ.get("TG_SESSION") else "not configured"))
    print("No Telegram groups joined or messages sent by management actions.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Paths, values and URLs can reveal private operational data.
        print("Private management stopped safely (" + type(exc).__name__ +
              "). Verify private state access, URLs and settings.", file=sys.stderr)
        sys.exit(1)
