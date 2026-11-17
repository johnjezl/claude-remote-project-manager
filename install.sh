#!/usr/bin/env bash
# Install crpm as a systemd *user* service.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin ~/.config/crpm ~/.config/systemd/user
ln -sf "$here/crpm.py" ~/.local/bin/crpm
chmod +x "$here/crpm.py"
[ -f ~/.config/crpm/config.toml ] || cp "$here/config.example.toml" ~/.config/crpm/config.toml
cp "$here/systemd/crpm.service" "$here/systemd/crpm-web.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crpm.service
if [ ! -s ~/.config/crpm/web-tokens ]; then
  echo "Web dashboard login (save this link; it is shown only once, \`crpm web-token\` makes a new one):"
  "$here/crpm.py" web-token
fi
systemctl --user enable --now crpm-web.service
systemctl --user restart crpm-web.service   # pick up a new crpm.py on reinstall
if [ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]; then
  echo "NOTE: run 'sudo loginctl enable-linger $USER' so crpm starts at boot without a login."
fi
echo "Installed. Logs: journalctl --user -u crpm -u crpm-web -f    Status: crpm status"
