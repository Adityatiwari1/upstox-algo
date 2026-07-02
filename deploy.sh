#!/usr/bin/env bash
# deploy.sh — pull latest code and restart services without closing the session.
# Run from any pane: bash deploy.sh
# tmux sessions: "algo" (TUI) and "api" (web server).

set -e

echo "==> git pull"
git pull

echo "==> restarting api (uvicorn)..."
# Send Ctrl+C then relaunch in the api window
tmux send-keys -t api C-c ""
sleep 1
tmux send-keys -t api "uptox-web" Enter
echo "    api restarted"

echo "==> restarting tui (trading terminal)..."
# 'q' is the Textual quit binding; open trade is safe — persisted to open_trade.json
tmux send-keys -t algo "q" ""
sleep 2
tmux send-keys -t algo "uptox" Enter
echo "    tui restarted"

echo "==> done. attach to tui: tmux attach -t algo"
