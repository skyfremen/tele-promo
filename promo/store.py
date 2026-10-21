"""Private GitHub Contents storage with optimistic concurrency; never use caches."""
import base64
import json
import os
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def validate_state(state):
    if (not isinstance(state, dict) or state.get('version') != 1
            or not isinstance(state.get('groups'), dict)
            or not isinstance(state.get('posts'), dict)
            or not isinstance(state.get('blocked_until'), (int, float))):
        raise ValueError('Invalid private state; refusing to reset history')
    for entry in state['posts'].values():
        if not isinstance(entry, dict) or entry.get('status') not in ('pending', 'confirmed', 'uncertain') or not isinstance(entry.get('at'), (int, float)) or type(entry.get('group_id')) is not int:
            raise ValueError('Invalid posting ledger')
    return state


class GitHubStore:
    def __init__(self):
        repo = os.environ['STATE_REPO']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError('Invalid state repository')
        self.base = 'https://api.github.com/repos/' + repo
        self.token = os.environ['STATE_TOKEN']
        self.sha = None
        metadata = self.request('GET', self.base)
        if metadata.get('private') is not True:
            raise ValueError('State repository MUST be private')
        self.branch = metadata['default_branch']
        # Verify that the repository is initialized; a missing branch is not an
        # empty ledger and must never trigger an implicit reset.
        self.request('GET', self.base + '/git/ref/heads/' + self.branch)
        try:
            obj = self.request('GET', self.base + '/contents/state.json?ref=' + self.branch)
        except HTTPError as exc:
            if exc.code != 404:
                raise
            self.state = None
        else:
            self.sha = obj['sha']
            self.state = validate_state(json.loads(base64.b64decode(obj['content'])))

    def request(self, method, url, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = Request(url, data=body, method=method, headers={
            'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
            'User-Agent': 'tele-promo',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    def save(self, state):
        validate_state(state)
        # Recheck visibility before every write, including after a rename/change.
        if self.request('GET', self.base).get('private') is not True:
            raise ValueError('State repository is no longer private')
        payload = {'message': 'Update private automation state', 'branch': self.branch,
                   'content': base64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()}
        if self.sha:
            payload['sha'] = self.sha
        result = self.request('PUT', self.base + '/contents/state.json', payload)
        self.sha = result['content']['sha']
        self.state = state
