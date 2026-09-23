#!/usr/bin/env bash
# AI Router — start the server (run this after setup.sh)
set -e

if [ ! -d ".venv" ]; then
    echo "Run setup.sh first: bash setup.sh"
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "No .env found. Run setup.sh first: bash setup.sh"
    exit 1
fi

# Unbuffered so logs appear in real time when piped to a file or PM2
export PYTHONUNBUFFERED=1
exec .venv/bin/python -m router.main
