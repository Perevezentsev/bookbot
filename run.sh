#!/bin/bash
cd "$(dirname "$0")"
exec .venv/bin/python bot.py >> bot.log 2>&1
