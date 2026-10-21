"""Run locally after `gh auth login`; creates repositories and installs dry-run automation."""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
PUBLIC = 'skyfremen/tele-promo'
PRIVATE = 'skyfremen/tele-promo-state'


def command(*args, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, cwd=ROOT)
    if result.returncode:
        # External output can contain tokens or authentication/account details.
        raise RuntimeError('Local deployment command failed: ' + args[0])
    return result.stdout.strip()


def repo(name, private):
    result = subprocess.run(['gh', 'repo', 'view', name, '--json', 'isPrivate'], text=True, capture_output=True)
    if result.returncode:
        command('gh', 'repo', 'create', name, '--private' if private else '--public', '--disable-issues', '--disable-wiki')
    else:
        if json.loads(result.stdout)['isPrivate'] != private:
            raise ValueError('Repository visibility does not match the required setting')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml', help='Local private settings file; do not put credentials in config')
    args = parser.parse_args()
    if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Run locally only')
    command('gh', 'auth', 'status')
    command('git', '--version')
    command(sys.executable, '-m', 'pip', 'install', '-e', '.[test]')
    import yaml
    from promo.core import validate_config
    public_template = yaml.safe_load((ROOT / 'config.yaml').read_text())
    repo(PUBLIC, False)
    repo(PRIVATE, True)
    # Initialize the private default branch with a non-sensitive sentinel.
    result = subprocess.run(['gh', 'api', 'repos/' + PRIVATE + '/commits', '--jq', 'length'], capture_output=True, text=True)
    if result.returncode:
        command('gh', 'api', '--method', 'PUT', 'repos/' + PRIVATE + '/contents/.initialized', '--input', '-', input=json.dumps({
            'message': 'Initialize private state storage',
            'content': base64.b64encode(b'private state storage\n').decode(),
        }))
    command(sys.executable, '-m', 'pytest', '-q')
    print('Repositories ready; local tests passed. Enter credentials locally only.')
    api_id = getpass.getpass('TG_API_ID: ').strip()
    api_hash = getpass.getpass('TG_API_HASH: ').strip()
    token = getpass.getpass('Fine-grained GitHub token, Contents read/write on tele-promo-state only: ').strip()
    if not api_id.isdecimal() or not api_hash or not token:
        raise ValueError('Required credentials missing')
    os.environ.update(TG_API_ID=api_id, TG_API_HASH=api_hash, STATE_REPO=PRIVATE, STATE_TOKEN=token)
    session_path = ROOT / '.tg-session'
    if not session_path.exists():
        # Authentication is intentionally interactive with hidden code/password input.
        subprocess.run([sys.executable, 'session.py'], cwd=ROOT, check=True)
    session = session_path.read_text().strip()
    if not session:
        raise ValueError('Empty session')
    # All secret bodies go through stdin, never shell arguments or output.
    for name, value in {'TG_API_ID': api_id, 'TG_API_HASH': api_hash, 'TG_SESSION': session,
                        'STATE_REPO': PRIVATE, 'STATE_TOKEN': token}.items():
        command('gh', 'secret', 'set', name, '--repo', PUBLIC, input=value)
    from promo.store import GitHubStore
    store = GitHubStore()
    if store.state is None:
        with (ROOT / args.config).open() as stream:
            config = validate_config(yaml.safe_load(stream))
        if not config['dry_run']:
            raise ValueError('Initial deployment must use dry-run')
        store.save({'version': 1, 'config': config, 'groups': {}, 'posts': {}, 'blocked_until': 0})
    elif not store.state['config']['dry_run']:
        raise ValueError('Existing configuration must be put in dry-run before deployment')
    command('gh', 'api', '--method', 'PUT', 'repos/' + PUBLIC + '/environments/telegram')
    command('gh', 'variable', 'set', 'AUTOMATION_ENABLED', '--repo', PUBLIC, '--body', 'true')
    command('gh', 'variable', 'set', 'LIVE_ENABLED', '--repo', PUBLIC, '--body', 'false')
    if not (ROOT / '.git').exists():
        command('git', 'init', '-b', 'main')
        command('git', 'remote', 'add', 'origin', 'https://github.com/' + PUBLIC + '.git')
    elif command('git', 'remote', 'get-url', 'origin') not in ('https://github.com/' + PUBLIC + '.git', 'git@github.com:' + PUBLIC + '.git'):
        raise ValueError('Unexpected git remote')
    command('git', 'add', '--', '.github', '.gitignore', 'config.yaml', 'pyproject.toml', 'promo', 'tests', 'session.py', 'deploy.py')
    if command('git', 'diff', '--cached', '--name-only'):
        command('git', '-c', 'user.name=skyfremen', '-c', 'user.email=skyfremen@users.noreply.github.com',
                'commit', '-m', 'Implement permission-gated Telegram promotion with private durable history')
    # No force push; concurrent or pre-existing remote work is protected.
    command('git', '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential', 'push', '-u', 'origin', 'HEAD:main')
    session_path.unlink()  # the session is now stored in GitHub Secrets
    commit = command('git', 'rev-parse', 'HEAD')
    for _ in range(12):
        runs = json.loads(command('gh', 'run', 'list', '--repo', PUBLIC, '--workflow', 'ci.yml', '--commit', commit, '--limit', '1', '--json', 'databaseId'))
        if runs:
            command('gh', 'run', 'watch', str(runs[0]['databaseId']), '--repo', PUBLIC, '--exit-status')
            print('GitHub Actions tests passed.')
            break
        time.sleep(5)
    else:
        print('GitHub Actions test result is pending; check the Tests workflow.')
    print('Pushed https://github.com/' + PUBLIC + '. Tests run automatically on push.')
    print('Dry-run enabled; LIVE_ENABLED=false. Configure links and review private group rules before enabling live mode.')
    print('Verify the Tests run in Actions. No live joining or posting has been requested.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Deployment stopped. Check local gh authentication, repository access, token permissions, and private configuration. Secrets were not printed.')
