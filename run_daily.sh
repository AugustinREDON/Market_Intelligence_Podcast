#!/bin/zsh

set -e

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"

# Make Homebrew tools such as ffmpeg available to launchd
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$PROJECT_DIR"

/usr/bin/caffeinate -di "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/run_pipeline.py"