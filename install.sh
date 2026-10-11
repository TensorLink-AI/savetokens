#!/bin/sh
# savetokens: install it (if needed) and set up this machine
#   curl -fsSL https://raw.githubusercontent.com/TensorLink-AI/savetokens/main/install.sh | sh
#   ... | sh -s -- --no-ephemeris      (any `savetokens install` options after --)
set -eu
SOURCE="${SAVETOKENS_SOURCE:-git+https://github.com/TensorLink-AI/savetokens}"
PATH="$HOME/.local/bin:$PATH"; export PATH
if ! command -v savetokens >/dev/null 2>&1; then
  echo "Installing savetokens..."
  if command -v uv >/dev/null 2>&1; then uv tool install --quiet "$SOURCE"
  elif command -v pipx >/dev/null 2>&1; then pipx install --quiet "$SOURCE"
  else
    echo "(first uv, the Python installer: https://docs.astral.sh/uv)"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    uv tool install --quiet "$SOURCE"
  fi
fi
if ( : </dev/tty ) 2>/dev/null; then
  exec savetokens install "$@" </dev/tty
else
  exec savetokens install "$@" --yes
fi
