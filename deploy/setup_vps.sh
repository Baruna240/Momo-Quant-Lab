#!/usr/bin/env bash
# Pasang Momo Quant Lab di VPS Ubuntu 24.04 (jalankan sebagai root, sekali saja):
#   curl -fsSL https://raw.githubusercontent.com/Baruna240/Momo-Quant-Lab/main/deploy/setup_vps.sh | bash
#
# Yang dilakukan:
# - update sistem, pasang Python, git, sqlite, firewall (hanya SSH yang terbuka), fail2ban
# - buat user biasa "momo" (bot TIDAK berjalan sebagai root), salin kunci SSH root ke user itu
# - ambil kode dari GitHub ke /home/momo/momo, buat virtualenv, pasang dependensi
# - pasang 4 layanan systemd (simulator meme, simulator altcoin, dashboard, supervisor agent) + pembaruan data altcoin harian
# Layanan BELUM dinyalakan: .env dan data dipindahkan dulu (lihat deploy/PANDUAN_VPS.md), lalu: momo-start
set -euo pipefail

REPO="https://github.com/Baruna240/Momo-Quant-Lab.git"
APP_USER="momo"
APP_DIR="/home/${APP_USER}/momo"

echo "== 1/6 update sistem & paket dasar"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y
apt-get install -y python3 python3-venv python3-pip git sqlite3 ufw fail2ban

echo "== 2/6 user ${APP_USER}"
if ! id "${APP_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "${APP_USER}"
fi
mkdir -p "/home/${APP_USER}/.ssh"
cp /root/.ssh/authorized_keys "/home/${APP_USER}/.ssh/authorized_keys"
chown -R "${APP_USER}:${APP_USER}" "/home/${APP_USER}/.ssh"
chmod 700 "/home/${APP_USER}/.ssh"
chmod 600 "/home/${APP_USER}/.ssh/authorized_keys"

echo "== 3/6 firewall: hanya SSH (dashboard diakses lewat terowongan SSH, tidak dibuka ke internet)"
ufw allow OpenSSH
ufw --force enable
# login SSH hanya dengan kunci, tanpa password
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh || systemctl reload sshd || true
systemctl enable --now fail2ban

echo "== 4/6 kode & dependensi"
if [ ! -d "${APP_DIR}/.git" ]; then
  sudo -u "${APP_USER}" git clone "${REPO}" "${APP_DIR}"
else
  sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only
fi
sudo -u "${APP_USER}" python3 -m venv "${APP_DIR}/.venv"
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --upgrade pip -q
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

echo "== 5/6 layanan systemd"
PY="${APP_DIR}/.venv/bin/python"
unit() {   # unit <nama> <deskripsi> <perintah>
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=$3
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
}
unit momo-sim       "Momo: simulator meme coin"        "${PY} sim.py"
unit momo-alt       "Momo: simulator altcoin & futures" "${PY} altcoin.py"
unit momo-dashboard "Momo: dashboard (127.0.0.1:8050)"  "${PY} dashboard.py --no-browser"
unit momo-agents    "Momo: supervisor 8 agent"          "${PY} -m agents.supervisor --loop 600"

cat > /etc/systemd/system/momo-altdata.service <<EOF
[Unit]
Description=Momo: perbarui candle altcoin

[Service]
Type=oneshot
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PY} alt_data.py
EOF
cat > /etc/systemd/system/momo-altdata.timer <<EOF
[Unit]
Description=Momo: perbarui candle altcoin tiap hari

[Timer]
OnCalendar=*-*-* 00:20:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /usr/local/bin/momo-start <<'EOF'
#!/usr/bin/env bash
# nyalakan semua layanan Momo (setelah .env & data dipindahkan)
set -e
test -f /home/momo/momo/.env || { echo ".env belum ada di /home/momo/momo/.env"; exit 1; }
systemctl daemon-reload
systemctl enable --now momo-sim momo-alt momo-dashboard momo-agents momo-altdata.timer
systemctl --no-pager --lines=0 status momo-sim momo-alt momo-dashboard momo-agents
EOF
cat > /usr/local/bin/momo-update <<'EOF'
#!/usr/bin/env bash
# ambil kode terbaru dari GitHub lalu restart layanan
set -e
sudo -u momo git -C /home/momo/momo pull --ff-only
sudo -u momo /home/momo/momo/.venv/bin/pip install -r /home/momo/momo/requirements.txt -q
systemctl restart momo-sim momo-alt momo-dashboard momo-agents
echo "diperbarui ke: $(sudo -u momo git -C /home/momo/momo log --oneline -1)"
EOF
chmod +x /usr/local/bin/momo-start /usr/local/bin/momo-update
systemctl daemon-reload

echo "== 6/6 selesai"
echo "Langkah berikutnya (dari laptop): pindahkan .env & database, lalu jalankan 'momo-start' di VPS."
echo "Lihat log: journalctl -u momo-sim -f"
