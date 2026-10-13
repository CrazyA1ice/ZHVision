#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py
