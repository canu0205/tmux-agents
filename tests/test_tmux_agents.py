import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tmux-agents-test-')
        self.root = pathlib.Path(self.tmp.name)
        self.socket = str(self.root / 'socket')
        self.env = dict(os.environ)
        self.env.pop('TMUX', None)
        self.env['CODEX_HOME'] = str(self.root / 'codex-home')
        self.config = self.root / 'tmux.conf'
        self.config.write_text("set -g default-shell /bin/sh\nset -g default-command /bin/sh\nset -g @resurrect-processes ':all:'\nset -g @resurrect-dir '" + str(self.root / 'snapshots') + "'\n")
        self.tmux('-f', str(self.config), 'new-session', '-d', '-s', 'same-dir', '-c', str(self.root))
        self.env['TMUX'] = self.tmux('display-message', '-p', '#{socket_path},#{pid},0')
        self.cli = [shutil.which('python3'), str(ROOT / 'tmux_agents.py')]

    def tearDown(self):
        subprocess.run(['tmux', '-S', self.socket, 'kill-server'], env=self.env, capture_output=True)
        self.tmp.cleanup()

    def tmux(self, *args):
        return subprocess.check_output(['tmux', '-S', self.socket, *args], env=self.env, text=True).strip()

    def run_cli(self, *args):
        result = subprocess.run(self.cli + list(args), env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_same_directory_save_restore_and_busy_repeat_guards(self):
        self.run_cli('install')
        folder = self.root / "directory with ' quotes"
        folder.mkdir()
        binary = self.root / 'claude'
        shutil.copy('/bin/sleep', binary)
        if sys.platform == 'darwin':
            subprocess.run(['codesign', '--force', '--sign', '-', str(binary)], check=True, capture_output=True)
        ids = ['019a39f6-f612-7ad1-8f50-bc1b2246a6cc', '019a39f7-5a28-7a33-b3a0-dceacabbf05c']
        for i, sid in enumerate(ids):
            if i:
                self.tmux('split-window', '-h', '-t', 'same-dir:0', '-c', str(folder))
            self.tmux('send-keys', '-t', 'same-dir:0.' + str(i), '-l', 'exec ' + shlex.quote(str(binary)) + ' 120')
            self.tmux('send-keys', '-t', 'same-dir:0.' + str(i), 'Enter')
        time.sleep(0.3)
        for i, sid in enumerate(ids):
            self.run_cli('register', 'claude', sid, '--pane', self.tmux('display-message', '-p', '-t', 'same-dir:0.' + str(i), '#{pane_id}'), '--cwd', str(folder))
        plugin = pathlib.Path.home() / '.tmux/plugins/tmux-resurrect/scripts'
        subprocess.run([str(plugin / 'save.sh'), 'quiet'], env=self.env, check=True, capture_output=True)
        snapshot = (self.root / 'snapshots/last').resolve()
        result = subprocess.run(self.cli + ['restore'], env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, 'restore must require a matching pre-restore hook')
        text = snapshot.read_text()
        for sid in ids:
            self.assertIn(sid, text)
        self.tmux('kill-server')
        time.sleep(0.1)
        self.tmux('-f', str(self.config), 'new-session', '-d', '-s', 'bootstrap', '-c', str(self.root))
        self.env['TMUX'] = self.tmux('display-message', '-p', '#{socket_path},#{pid},0')
        # fake agents log arguments and stay busy.
        binary.unlink()
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + shlex.quote(str(self.root / 'launched')) + '\nsleep 120\n')
        binary.chmod(0o755)
        self.env['PATH'] = str(self.root) + ':' + self.env['PATH']
        self.tmux('set-environment', '-g', 'PATH', self.env['PATH'])
        self.run_cli('install')
        subprocess.run([str(plugin / 'restore.sh'), 'quiet'], env=self.env, check=True, capture_output=True)
        time.sleep(0.5)
        launches = (self.root / 'launched').read_text().splitlines()
        self.assertCountEqual(launches, ['--resume ' + x for x in ids])
        result = subprocess.run(self.cli + ['restore'], env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'launched').read_text().splitlines(), launches)



if __name__ == "__main__":
    unittest.main()
