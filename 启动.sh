#!/bin/sh
# CSswitch macOS / Linux launcher.
set -eu

ROOT=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
: "${CSSWITCH_HOME:=$HOME/.csswitch-win}"
export CSSWITCH_HOME
mkdir -p "$CSSWITCH_HOME"

if [ -n "${CSSWITCH_PYTHON:-}" ]; then
    PY=$CSSWITCH_PYTHON
elif command -v python3 >/dev/null 2>&1; then
    PY=$(command -v python3)
else
    printf '%s\n' '[X] Python 3 not found. Install Python 3.9 or newer first.' >&2
    exit 1
fi

if ! "$PY" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
    printf '%s\n' '[X] Python 3.9 or newer is required.' >&2
    exit 1
fi

if ! "$PY" -c 'import cryptography' >/dev/null 2>&1; then
    VENV_PY=$ROOT/.venv/bin/python
    if [ ! -x "$VENV_PY" ]; then
        printf '%s\n' '[i] Creating local Python environment ...'
        if ! "$PY" -m venv "$ROOT/.venv"; then
            printf '%s\n' '[X] Could not create .venv. On Ubuntu/Debian install python3-venv.' >&2
            exit 1
        fi
    fi
    PY=$VENV_PY
    printf '%s\n' '[i] Installing Python dependency cryptography ...'
    "$PY" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
fi

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY FTP_PROXY
unset http_proxy https_proxy all_proxy ftp_proxy
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export PYTHONDONTWRITEBYTECODE=1

if [ "$#" -eq 0 ]; then
    set -- panel
fi
exec "$PY" "$ROOT/csx.py" "$@"
