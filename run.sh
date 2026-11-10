#!/usr/bin/env bash
# gclassfetch startup script for macOS and Linux

cd "$(dirname "$0")"

if command -v python3 &>/dev/null; then
    PYTHON_CMD=python3
elif command -v python &>/dev/null; then
    PYTHON_CMD=python
else
    echo "[ERROR] Python 3 is not installed or not in your PATH."
    exit 1
fi

echo "Starting gclassfetch..."
$PYTHON_CMD -u app.py "$@"
