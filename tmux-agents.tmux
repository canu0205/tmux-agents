#!/bin/sh
# load after tmux-resurrect; continuum remains last in the plugin list.
plugin_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$plugin_dir/tmux_agents.py" install
