#!/usr/bin/env python3
"""restore exact agent conversations through tmux-resurrect, without a daemon."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
CODEX_TITLE = re.compile(r'codex \| (' + UUID + r'|[0-9a-f-]{29}\.\.\.)')
# retain snapshot and pane keys for compatibility.
PREFIX = 'agent-resurrect\t'
OPTION = '@agent-resurrect'
FIELDS = ('session_name', 'window_index', 'pane_index', 'pane_id', 'pane_pid',
          'pane_current_command', 'pane_current_path', 'pane_title', OPTION)
HOOKS = {'save': 'post-save-layout', 'before': 'pre-restore-all', 'restore': 'post-restore-all'}


def tmux(*args):
    return subprocess.check_output(['tmux', *args], text=True, timeout=10).rstrip('\n')


def option(name):
    return tmux('show-option', '-gqv', name)


def panes():
    output = tmux('list-panes', '-a', '-F', '\t'.join('#{' + x + '}' for x in FIELDS))
    return [dict(zip(FIELDS, parts)) for line in output.splitlines()
            if len(parts := line.split('\t')) == len(FIELDS)]


def processes():
    output = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,lstart=,comm='], text=True, timeout=10)
    result = {}
    for line in output.splitlines():
        parts = line.split(None, 7)
        if len(parts) == 8:
            result[parts[0]] = {'parent': parts[1], 'start': ' '.join(parts[2:7]),
                              'name': Path(parts[7]).name}
    return result


def descendant(pid, parent, table):
    seen = set()
    while pid in table and pid not in seen:
        if pid == parent:
            return True
        seen.add(pid)
        pid = table[pid]['parent']
    return False


def owner(pane, agent, table):
    candidates = [(pid, proc) for pid, proc in table.items()
                  if proc['name'] == agent and descendant(pid, pane['pane_pid'], table)]
    # select the leaf when launcher and child share a name.
    leaves = [(pid, proc) for pid, proc in candidates if not any(
        other != pid and descendant(other, pid, table) for other, _ in candidates)]
    return leaves[0] if len(leaves) == 1 else None


def codex_identity(title, home):
    match = CODEX_TITLE.fullmatch(title)
    if not match:
        return None
    key = match[1].removesuffix('...')
    databases = sorted(home.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not databases:
        return None
    try:
        with closing(sqlite3.connect(databases[0].as_uri() + '?mode=ro', uri=True, timeout=0.1)) as db:
            rows = db.execute('SELECT id,cwd FROM threads WHERE id >= ? AND id < ? LIMIT 2',
                              (key, key + 'g')).fetchall()
        if len(rows) == 1 and re.fullmatch(UUID, rows[0][0]):
            return tuple(rows[0])
    except sqlite3.Error:
        pass
    return None


def validate(record):
    if (record.get('agent') not in ('codex', 'claude')
            or not re.fullmatch(UUID, record.get('id', ''))
            or not os.path.isabs(record.get('cwd', ''))
            or not isinstance(record.get('session'), str)
            or not all(re.fullmatch(r'\d+', record.get(k, '')) for k in ('window', 'pane'))):
        raise ValueError('invalid agent snapshot record')
    if any('\x00' in value for value in record.values() if isinstance(value, str)):
        raise ValueError('NUL in agent snapshot record')


def encode_record(record):
    validate(record)
    return PREFIX + json.dumps({'version': 1, **record}, separators=(',', ':')) + '\n'


def read_records(text):
    records = []
    for line in text.splitlines():
        if line.startswith(PREFIX):
            try:
                record = json.loads(line[len(PREFIX):])
                if record.pop('version', None) != 1:
                    continue
                validate(record)
                records.append(record)
            except (ValueError, TypeError, AttributeError):
                print('tmux-agents: skipped invalid snapshot record', file=sys.stderr)
    return records


def snapshot_path():
    configured = option('@resurrect-dir')
    directory = (Path(os.path.expandvars(configured)).expanduser() if configured else
                 Path.home() / '.tmux/resurrect' if (Path.home() / '.tmux/resurrect').is_dir() else
                 Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'tmux/resurrect')
    return directory / 'last'


def register(agent, sid, target, cwd=None, hook=False):
    if cwd is not None and not os.path.isabs(cwd):
        raise ValueError('working directory must be absolute')
    if not re.fullmatch(UUID, sid):
        raise ValueError('a full conversation UUID is required')
    pane = next((p for p in panes() if p['pane_id'] == target), None)
    if pane is None:
        raise ValueError('pane not found; use its %id')
    table = processes()
    process = owner(pane, agent, table)
    if process is None:
        raise ValueError('expected exactly one running ' + agent + ' in the pane')
    pid, proc = process
    if hook and not descendant(str(os.getpid()), pid, table):
        raise ValueError('hook does not belong to this pane\'s agent process')
    value = {'agent': agent, 'id': sid, 'cwd': cwd or pane['pane_current_path'],
             'pid': pid, 'start': proc['start']}
    tmux('set-option', '-p', '-t', target, OPTION, json.dumps(value, separators=(',', ':')))


def capture():
    table = processes()
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
    records = []
    for pane in panes():
        agent = pane['pane_current_command']
        # native Claude uses a version as its executable name.
        if re.fullmatch(r'\d+\.\d+\.\d+(?:[-.].*)?', agent) and owner(pane, 'claude', table):
            agent = 'claude'
        if agent not in ('codex', 'claude'):
            continue
        identity = codex_identity(pane['pane_title'], home) if agent == 'codex' else None
        if not identity and not (agent == 'codex' and CODEX_TITLE.fullmatch(pane['pane_title'])):
            try:
                registered = json.loads(pane[OPTION])
                process = owner(pane, agent, table)
                if (process and registered['agent'] == agent and registered['pid'] == process[0]
                        and registered['start'] == process[1]['start']):
                    identity = registered['id'], registered['cwd']
            except (ValueError, KeyError, TypeError):
                pass
        if not identity:
            print('tmux-agents: no exact session for ' + pane['pane_id'] + ' (' + agent + ')', file=sys.stderr)
            continue
        record = {'agent': agent, 'id': identity[0], 'cwd': identity[1],
                  'session': pane['session_name'], 'window': pane['window_index'], 'pane': pane['pane_index']}
        validate(record)
        records.append(record)
    return records


def save(path):
    records = capture()
    managed = {(r['session'], r['window'], r['pane']) for r in records}
    lines = []
    # prevent duplicate launches by upstream process replay.
    for line in Path(path).read_text().splitlines():
        parts = line.split('\t')
        if len(parts) == 11 and parts[0] == 'pane' and (parts[1], parts[2], parts[5]) in managed:
            parts[10] = ':'
            line = '\t'.join(parts)
        if not line.startswith(PREFIX):
            lines.append(line + '\n')
    # upstream publishes this snapshot after the hook returns.
    Path(path).write_text(''.join(lines) + ''.join(encode_record(r) for r in records))


def before():
    context = {'snapshot': hashlib.sha256(snapshot_path().read_bytes()).hexdigest(),
               'panes': {p['pane_id']: p['pane_pid'] for p in panes()}}
    tmux('set-option', '-g', OPTION + '-before', json.dumps(context))


def restore(dry_run=False):
    snapshot = snapshot_path().read_bytes()
    records = read_records(snapshot.decode())
    current = {(p['session_name'], p['window_index'], p['pane_index']): p for p in panes()}
    context = json.loads(option(OPTION + '-before') or '{}')
    if not dry_run:
        tmux('set-option', '-gu', OPTION + '-before')
        if context.get('snapshot') != hashlib.sha256(snapshot).hexdigest():
            raise ValueError('restore must run through tmux-resurrect; use --dry-run to inspect')
    previous = context.get('panes', {})
    for record in records:
        key = record['session'], record['window'], record['pane']
        pane = current.get(key)
        label = ':'.join(key)
        if pane is None:
            print(label + ': missing pane')
            continue
        command = shlex.join([
            record['agent'], 'resume' if record['agent'] == 'codex' else '--resume', record['id'],
        ])
        if dry_run:
            print(label + ': cd ' + shlex.quote(record['cwd']) + ' && ' + command)
            continue
        if (pane['pane_current_command'] not in ('sh', 'bash', 'zsh', 'fish', 'dash', 'ksh')
                or previous.get(pane['pane_id']) == pane['pane_pid']):
            print(label + ': skipped existing or busy pane')
            continue
        marker = record['agent'] + ':' + record['id']
        if tmux('show-option', '-pqv', '-t', pane['pane_id'], OPTION + '-restored'):
            continue
        if not Path(record['cwd']).is_dir() or not shutil.which(record['agent']):
            print(label + ': missing directory or agent executable', file=sys.stderr)
            continue
        tmux('set-option', '-p', '-t', pane['pane_id'], OPTION + '-restored', marker)
        # literal keys preserve shell quoting.
        tmux('send-keys', '-t', pane['pane_id'], '-l', 'cd ' + shlex.quote(record['cwd']) + ' && ' + command)
        tmux('send-keys', '-t', pane['pane_id'], 'Enter')
        print(label + ': sent resume command for ' + record['agent'])


def install():
    executable = shutil.which('python3') or sys.executable
    for action, name in HOOKS.items():
        key = '@resurrect-hook-' + name
        command = shlex.join([executable, str(Path(__file__).resolve()), 'hook', action])
        old = option(key)
        if old == command:
            continue
        if 'tmux_agents.py hook ' in old:
            raise ValueError('another tmux-agents installation owns ' + key)
        tmux('set-option', '-g', OPTION + '-original-' + action, old)
        tmux('set-option', '-g', key, command)


def uninstall():
    for action, name in HOOKS.items():
        key = '@resurrect-hook-' + name
        if str(Path(__file__).resolve()) in option(key):
            tmux('set-option', '-g', key, option(OPTION + '-original-' + action))
            tmux('set-option', '-gu', OPTION + '-original-' + action)
    tmux('set-option', '-gu', OPTION + '-before')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('install')
    sub.add_parser('uninstall')
    sub.add_parser('before')
    sub.add_parser('inspect')
    sub.add_parser('claude-hook')
    p = sub.add_parser('save')
    p.add_argument('path')
    p = sub.add_parser('restore')
    p.add_argument('--dry-run', action='store_true')
    p = sub.add_parser('register')
    p.add_argument('agent', choices=['codex', 'claude'])
    p.add_argument('id')
    p.add_argument('--pane', default=os.environ.get('TMUX_PANE'))
    p.add_argument('--cwd')
    p = sub.add_parser('hook')
    p.add_argument('action', choices=HOOKS)
    p.add_argument('args', nargs='*')
    args = parser.parse_args()
    if args.command == 'install':
        install()
    elif args.command == 'uninstall':
        uninstall()
    elif args.command == 'save':
        save(args.path)
    elif args.command == 'before':
        before()
    elif args.command == 'restore':
        restore(args.dry_run)
    elif args.command == 'inspect':
        print(json.dumps(capture(), indent=2))
    elif args.command == 'register':
        register(args.agent, args.id, args.pane, args.cwd)
    elif args.command == 'claude-hook':
        if not os.environ.get('TMUX') or not os.environ.get('TMUX_PANE'):
            return
        payload = json.load(sys.stdin)
        register('claude', payload['session_id'], os.environ['TMUX_PANE'], payload['cwd'], hook=True)
    elif args.command == 'hook':
        original = option(OPTION + '-original-' + args.action)
        if original:
            subprocess.run(['/bin/bash', '-c', original + ' ' + shlex.join(args.args)], check=True)
        if args.action == 'save':
            save(args.args[0])
        elif args.action == 'before':
            before()
        else:
            restore()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print('tmux-agents: ' + str(error), file=sys.stderr)
        sys.exit(1)
