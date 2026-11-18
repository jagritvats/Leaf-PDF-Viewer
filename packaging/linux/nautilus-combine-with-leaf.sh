#!/bin/sh
# GNOME Files (Nautilus) script: select PDFs, right-click > Scripts > Combine with Leaf (one new document).
#   install -Dm755 nautilus-combine-with-leaf.sh "$HOME/.local/share/nautilus/scripts/Combine with Leaf"
# Nautilus passes the selected files as arguments; Leaf keeps their order.
exec leaf --merge "$@"
