#!/bin/sh
# comms-kit installer. It writes only to:
#   ~/.config/comms-kit/config.json   your settings, created once, mode 600
#   ~/.local/share/comms-kit/         the message archive
#   ~/.local/bin/comms, gws-as        links to this checkout
# It never edits a shell file or an agent's settings, and never replaces a file it did not make.
set -eu

KIT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_DIR="$HOME/.config/comms-kit"
CONFIG="$CONFIG_DIR/config.json"
DATA_DIR="$HOME/.local/share/comms-kit"
BIN_DIR="$HOME/.local/bin"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "comms-kit needs python 3.10 or newer as python3. Install it, then run this again." >&2
    exit 1
fi

# Check every link first, so a refusal leaves the machine unchanged.
for name in comms gws-as; do
    target="$BIN_DIR/$name"
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$target")" != \
             "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$KIT/core/bin/$name")" ]; then
            echo "$target already exists and is not this kit: not replaced. Move it, then run this again." >&2
            exit 1
        fi
    fi
done

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$BIN_DIR"
chmod 700 "$CONFIG_DIR" "$DATA_DIR"

if [ -e "$CONFIG" ]; then
    echo "Config kept: $CONFIG"
else
    (umask 077; python3 - "$CONFIG" "$DATA_DIR" <<'PY'
import json
import sys

# Start empty, so a placeholder from the example file can never look like a real setting.
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"data_dir": sys.argv[2]}, fh, indent=2)
    fh.write("\n")
PY
    )
    echo "Config created: $CONFIG"
fi

for name in comms gws-as; do
    ln -sf "$KIT/core/bin/$name" "$BIN_DIR/$name"
done
echo "Commands linked: $BIN_DIR/comms, $BIN_DIR/gws-as"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "$BIN_DIR is not on your PATH. Add it in your shell's start file." ;;
esac

cat <<EOF

Next:
1. Pick a channel in README.md and read its risk line.
2. Copy the keys you need from $KIT/config.example.json to $CONFIG.
3. Run: comms doctor
EOF
