# source from bashrc or zshrc so a new client cannot inherit an old thread title.
codex() {
    local tmux_agents_exit
    if [ -n "${TMUX_PANE:-}" ]; then printf '\033]0;\007'; fi
    command codex "$@"
    tmux_agents_exit=$?
    if [ -n "${TMUX_PANE:-}" ]; then printf '\033]0;\007'; fi
    return "$tmux_agents_exit"
}
