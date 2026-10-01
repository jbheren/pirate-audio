#!/usr/bin/env bash
# Pirate Audio + Spotify Connect (go-librespot) + pochettes à l'écran
# Pour Raspberry Pi OS Lite 32 bits (Pi Zero W / ARMv6). À lancer SANS sudo :  ./install.sh
set -euo pipefail

GLR_VERSION="${GLR_VERSION:-v0.10.2}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ME="$(id -un)"
CONF_DIR="/home/$ME/.config/go-librespot"
APP_DIR="/opt/pirate-display"

[ "$ME" = "root" ] && { echo "Lance ce script avec ton utilisateur normal (il utilise sudo lui-même)."; exit 1; }

echo "==> Paquets système"
sudo apt-get update
sudo apt-get install -y curl avahi-daemon alsa-utils fonts-dejavu-core \
  python3-venv python3-dev build-essential \
  python3-pil python3-numpy python3-spidev python3-gpiozero python3-lgpio \
  python3-websocket python3-requests

echo "==> SPI + DAC (config.txt)"
sudo raspi-config nonint do_spi 0
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
sudo cp "$CFG" "$CFG.bak-pirate"
grep -q '^dtoverlay=hifiberry-dac' "$CFG" || echo 'dtoverlay=hifiberry-dac' | sudo tee -a "$CFG" >/dev/null
grep -q '^gpio=25=op,dh' "$CFG"           || echo 'gpio=25=op,dh'           | sudo tee -a "$CFG" >/dev/null
sudo sed -i 's/^dtparam=audio=on/#dtparam=audio=on/' "$CFG"

echo "==> go-librespot $GLR_VERSION"
case "$(uname -m)" in
  aarch64) ASSET=arm64 ;;
  *)       ASSET=armv6_rpi ;;   # Pi Zero W, Pi 1, et compatible ARMv7
esac
TMP="$(mktemp -d)"
curl -fsSL -o "$TMP/glr.tgz" \
  "https://github.com/devgianlu/go-librespot/releases/download/$GLR_VERSION/go-librespot_linux_$ASSET.tar.gz"
tar -xzf "$TMP/glr.tgz" -C "$TMP"
sudo install -m 755 "$TMP/go-librespot" /usr/local/bin/go-librespot
rm -rf "$TMP"
mkdir -p "$CONF_DIR"
[ -f "$CONF_DIR/config.yml" ] || cp "$HERE/config.yml" "$CONF_DIR/config.yml"

echo "==> Écran + boutons (venv $APP_DIR)"
sudo mkdir -p "$APP_DIR"
sudo chown "$ME": "$APP_DIR"
python3 -m venv --system-site-packages "$APP_DIR/venv"
# --no-deps : on garde numpy/spidev d'apt (pas de compilation de numpy sur le Zero)
"$APP_DIR/venv/bin/pip" install --no-deps st7789 gpiodevice gpiod
install -m 755 "$HERE/pirate_display.py" "$APP_DIR/pirate_display.py"
sudo usermod -aG audio,gpio,spi "$ME"

echo "==> Services systemd"
sudo tee /etc/systemd/system/go-librespot.service >/dev/null <<EOF
[Unit]
Description=go-librespot (Spotify Connect)
Wants=network-online.target avahi-daemon.service
After=network-online.target avahi-daemon.service sound.target

[Service]
User=$ME
ExecStart=/usr/local/bin/go-librespot --config_dir $CONF_DIR
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/pirate-display.service >/dev/null <<EOF
[Unit]
Description=Pirate Audio : pochettes + boutons pour go-librespot
After=go-librespot.service
Wants=go-librespot.service

[Service]
User=$ME
Environment=PYTHONUNBUFFERED=1
# lgpio crée ses fichiers de notification dans le dossier courant : il doit être inscriptible
WorkingDirectory=$APP_DIR
# Environment=PIRATE_Y_PIN=20   # décommente si le bouton Y ne réagit pas (anciennes cartes)
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/pirate_display.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable go-librespot.service pirate-display.service

echo
echo "Installation terminée. Redémarre :  sudo reboot"
echo "Puis dans l'appli Spotify (même Wi-Fi) choisis l'appareil « Pirate Audio »."
