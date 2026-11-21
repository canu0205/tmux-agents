# tmux-agents

Resume Codex and Claude conversations after a tmux restart. Stores session IDs in
tmux-resurrect snapshots and uses Continuum's autosave. No daemon.

Requires Python 3.9+, bash/zsh, tmux-resurrect, and tmux-continuum.

## Setup

Place this repo at `~/.tmux/plugins/tmux-agents`.

In `~/.tmux.conf`, before TPM initialization (keep Continuum last):

```tmux
run '~/.tmux/plugins/tmux-agents/tmux-agents.tmux'
```

Under `[tui]` in `~/.codex/config.toml`:

```toml
terminal_title = ["app-name", "thread-id"]
```

In `~/.zshrc` or `~/.bashrc`:

```sh
source ~/.tmux/plugins/tmux-agents/shell.sh
```

For Claude, merge into `~/.claude/settings.json`:

```json
{"hooks":{"SessionStart":[{"hooks":[{
  "type":"command",
  "command":"python3 \"$HOME/.tmux/plugins/tmux-agents/tmux_agents.py\" claude-hook",
  "timeout":5
}]}]}}
```

Reload tmux config and your shell, then relaunch agents. Launch Codex through the
shell wrapper and keep its configured title. Original conversation stores must remain.

## Use

Save: **prefix + Ctrl-s**. Restore: **prefix + Ctrl-r**.

```sh
~/.tmux/plugins/tmux-agents/tmux_agents.py inspect
~/.tmux/plugins/tmux-agents/tmux_agents.py restore --dry-run
```

Only new shell panes receive resume commands; existing panes are left alone.
