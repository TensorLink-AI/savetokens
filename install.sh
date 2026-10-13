#!/bin/sh
# savetokens: install it (if needed) and set up this machine
#   curl -fsSL https://raw.githubusercontent.com/TensorLink-AI/savetokens/main/install.sh | sh
#   ... | sh -s -- --no-ephemeris      (any `savetokens install` options after --)
set -eu
SOURCE="${SAVETOKENS_SOURCE:-git+https://github.com/TensorLink-AI/savetokens}"
YOUR_PATH="$PATH"
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
status=0
if ( : </dev/tty ) 2>/dev/null; then
  savetokens install "$@" </dev/tty || status=$?
else
  savetokens install "$@" --yes || status=$?
fi
if ! PATH="$YOUR_PATH" command -v savetokens >/dev/null 2>&1; then
  echo
  echo "savetokens is in $HOME/.local/bin, which isn't on your PATH yet: open a new terminal, or run"
  echo '  export PATH="$HOME/.local/bin:$PATH"'
fi
exit $status
