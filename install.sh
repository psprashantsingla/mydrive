#!/usr/bin/env bash
# MyDrive installer — Oracle Cloud Always Free VM (Ubuntu or Oracle Linux, x86_64 or arm64).
#
#   curl -fsSL https://raw.githubusercontent.com/psprashantsingla/mydrive/main/get.sh | sudo bash
#   (or, from a checkout:  sudo ./install.sh)
#
# Safe to re-run: it updates the app code and keeps your data, users and settings.
# Optional overrides:  DOMAIN=drive.example.com  DATA_DIR=/data  sudo -E ./install.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/mydrive
ETC_DIR=/etc/mydrive
DATA_DIR="${DATA_DIR:-/data}"
PORT=8080
CADDY_FALLBACK=2.10.2

c_bold=$'\e[1m'; c_grn=$'\e[32m'; c_yel=$'\e[33m'; c_red=$'\e[31m'; c_off=$'\e[0m'
step() { echo; echo "${c_bold}==> $*${c_off}"; }
info() { echo "    $*"; }
warn() { echo "${c_yel}    ! $*${c_off}"; }
die()  { echo "${c_red}ERROR: $*${c_off}" >&2; exit 1; }
ask()  { local a; read -r -p "    $1 " a </dev/tty; echo "$a"; }

[[ $EUID -eq 0 ]] || die "Run with sudo:  sudo ./install.sh"
[[ -f "$SRC_DIR/app/main.py" ]] || die "Run this from the extracted mydrive folder"

case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "Unsupported CPU: $(uname -m)" ;;
esac
if command -v apt-get >/dev/null; then PKG=apt; elif command -v dnf >/dev/null; then PKG=dnf; else die "Needs Ubuntu/Debian or Oracle Linux"; fi

# ------------------------------------------------------------------ packages
step "Installing system packages"
if [[ $PKG == apt ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates tar e2fsprogs sqlite3 >/dev/null
else
  dnf install -y -q curl ca-certificates tar e2fsprogs sqlite >/dev/null
fi
info "done"

# ------------------------------------------------------------------ swap (1 GB RAM box)
step "Checking swap"
if [[ -z "$(swapon --noheadings 2>/dev/null)" ]]; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  info "created 2 GB swap file"
else
  info "swap already enabled"
fi
sysctl -q -w vm.swappiness=10 >/dev/null || true
echo 'vm.swappiness=10' > /etc/sysctl.d/90-mydrive.conf

# ------------------------------------------------------------------ block volume
step "Setting up the storage volume at $DATA_DIR"
if mountpoint -q "$DATA_DIR"; then
  info "$DATA_DIR is already mounted ($(df -h --output=size "$DATA_DIR" | tail -1 | tr -d ' '))"
else
  ROOT_SRC="$(findmnt -no SOURCE /)"
  ROOT_DISK="$(lsblk -snpo NAME,TYPE "$ROOT_SRC" 2>/dev/null | awk '$2=="disk"{print $1}' | tail -1)"
  mapfile -t CANDS < <(lsblk -dbnpo NAME,SIZE,TYPE | awk -v r="$ROOT_DISK" '$3=="disk" && $1!=r && $2>=50*1024*1024*1024 {print $1}')
  if [[ ${#CANDS[@]} -eq 0 ]]; then
    echo
    lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
    die "No extra disk of 50 GB+ found. In the Oracle console open the instance → Attached block volumes.
       If the attachment type is iSCSI, click ⋮ → 'iSCSI commands & information', run the ATTACH commands
       shown there on this VM, then run this installer again. (Paravirtualized attachments need nothing extra.)"
  fi
  if [[ ${#CANDS[@]} -gt 1 ]]; then
    lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT
    DISK="$(ask "Several disks found. Which one is the 150 GB volume? (e.g. /dev/sdb):")"
  else
    DISK="${CANDS[0]}"
  fi
  [[ -b "$DISK" ]] || die "$DISK is not a block device"

  # If the disk was partitioned before, use its first partition that has a filesystem.
  DEV="$DISK"
  PART="$(lsblk -lnpo NAME,TYPE,FSTYPE "$DISK" | awk '$2=="part" && $3!="" {print $1; exit}')"
  [[ -n "$PART" ]] && DEV="$PART"
  CUR_MNT="$(lsblk -no MOUNTPOINT "$DEV" | head -1)"
  if [[ -n "$CUR_MNT" ]]; then
    warn "$DEV is already mounted at $CUR_MNT — using that as the data folder."
    DATA_DIR="$CUR_MNT"
  else
    FSTYPE="$(blkid -o value -s TYPE "$DEV" 2>/dev/null || true)"
    SIZE="$(lsblk -dno SIZE "$DISK" | tr -d ' ')"
    if [[ -n "$FSTYPE" ]]; then
      info "$DEV ($SIZE) already has a $FSTYPE filesystem — it will be mounted as-is (no formatting)."
    else
      echo
      lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT "$DISK"
      warn "$DISK ($SIZE) is empty and will be FORMATTED as ext4. Anything on it is lost."
      [[ "$(ask "Type 'yes' to format $DISK:")" == "yes" ]] || die "Cancelled — nothing was changed."
      mkfs.ext4 -q -L mydrive -m 1 "$DEV"
      FSTYPE=ext4
      info "formatted $DEV"
    fi
    UUID="$(blkid -o value -s UUID "$DEV")"
    mkdir -p "$DATA_DIR"
    if ! grep -q "UUID=$UUID" /etc/fstab; then
      echo "UUID=$UUID $DATA_DIR $FSTYPE defaults,noatime,_netdev,nofail 0 2" >> /etc/fstab
    fi
    systemctl daemon-reload
    mount "$DATA_DIR"
    info "mounted $DEV at $DATA_DIR ($(df -h --output=size "$DATA_DIR" | tail -1 | tr -d ' ')) and added it to /etc/fstab"
  fi
fi

# ------------------------------------------------------------------ app user + code
step "Installing the MyDrive app to $APP_DIR"
id mydrive >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin mydrive
mkdir -p "$APP_DIR" "$ETC_DIR" "$DATA_DIR"
rm -rf "$APP_DIR/app.new"
cp -r "$SRC_DIR/app" "$APP_DIR/app.new"
find "$APP_DIR/app.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$APP_DIR/app.old"; [[ -d "$APP_DIR/app" ]] && mv "$APP_DIR/app" "$APP_DIR/app.old"
mv "$APP_DIR/app.new" "$APP_DIR/app"
cp "$SRC_DIR/requirements.txt" "$APP_DIR/"

if ! command -v uv >/dev/null && [[ ! -x /usr/local/bin/uv ]]; then
  info "installing uv (Python manager)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
UV="$(command -v uv || echo /usr/local/bin/uv)"
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python" UV_CACHE_DIR="$APP_DIR/.uv-cache"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  info "creating Python 3.12 environment"
  "$UV" venv -q --python 3.12 "$APP_DIR/venv"
fi
"$UV" pip install -q --python "$APP_DIR/venv/bin/python" -r "$APP_DIR/requirements.txt"
rm -rf "$APP_DIR/.uv-cache"
chown -R root:root "$APP_DIR"
find "$DATA_DIR" ! -user mydrive -exec chown mydrive:mydrive {} + 2>/dev/null || true
chmod 750 "$DATA_DIR"
info "done"

# ------------------------------------------------------------------ config
step "Configuring"
if [[ -z "${DOMAIN:-}" ]]; then
  if [[ -f "$ETC_DIR/env" ]] && grep -q '^MYDRIVE_BASE_URL=' "$ETC_DIR/env"; then
    DOMAIN="$(grep '^MYDRIVE_BASE_URL=' "$ETC_DIR/env" | sed 's#.*://##')"
  else
    IP="$(curl -fsS -m 10 https://api.ipify.org || curl -fsS -m 10 https://ifconfig.me || true)"
    [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || IP="$(ask "Could not detect the public IP. Enter it:")"
    DOMAIN="${IP//./-}.sslip.io"
  fi
fi
cat > "$ETC_DIR/env" <<EOF
MYDRIVE_DATA_DIR=$DATA_DIR
MYDRIVE_BASE_URL=https://$DOMAIN
MYDRIVE_COOKIE_SECURE=1
MYDRIVE_TRASH_DAYS=30
EOF
chmod 644 "$ETC_DIR/env"
info "address: https://$DOMAIN"

cat > /usr/local/bin/mydrive-admin <<EOF
#!/usr/bin/env bash
# Manage MyDrive users from the server:  sudo mydrive-admin list-users | create-user NAME [--admin] | reset-password NAME
set -a; . $ETC_DIR/env; set +a
cd $APP_DIR && exec sudo -E -u mydrive $APP_DIR/venv/bin/python -m app.cli "\$@"
EOF
chmod 755 /usr/local/bin/mydrive-admin

cat > /etc/systemd/system/mydrive.service <<EOF
[Unit]
Description=MyDrive personal cloud storage
After=network-online.target
Wants=network-online.target
RequiresMountsFor=$DATA_DIR

[Service]
User=mydrive
Group=mydrive
WorkingDirectory=$APP_DIR
EnvironmentFile=$ETC_DIR/env
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log --timeout-keep-alive 30
Restart=always
RestartSec=3
MemoryMax=600M
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR
UMask=0027

[Install]
WantedBy=multi-user.target
EOF

# ------------------------------------------------------------------ Caddy (HTTPS)
step "Installing Caddy (automatic HTTPS)"
if [[ ! -x /usr/local/bin/caddy ]]; then
  if ! curl -fsSL -m 120 "https://caddyserver.com/api/download?os=linux&arch=$ARCH" -o /tmp/caddy.bin; then
    curl -fsSL -m 120 "https://github.com/caddyserver/caddy/releases/download/v$CADDY_FALLBACK/caddy_${CADDY_FALLBACK}_linux_$ARCH.tar.gz" \
      | tar xz -C /tmp caddy && mv /tmp/caddy /tmp/caddy.bin
  fi
  install -m 755 /tmp/caddy.bin /usr/local/bin/caddy && rm -f /tmp/caddy.bin
fi
info "$(/usr/local/bin/caddy version | cut -d' ' -f1)"
id caddy >/dev/null 2>&1 || useradd --system --home-dir /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	@text path / /static/* /api/files* /api/search* /api/trash* /api/shares* /api/keys* /api/admin/* /s/*
	encode @text gzip
	reverse_proxy 127.0.0.1:$PORT {
		flush_interval -1
	}
}
EOF
cat > /etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy web server
After=network-online.target
Wants=network-online.target

[Service]
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# ------------------------------------------------------------------ OS firewall
step "Opening ports 80 and 443 in the VM firewall"
if command -v firewall-cmd >/dev/null && systemctl is-active -q firewalld; then
  firewall-cmd -q --permanent --add-service=http --add-service=https
  firewall-cmd -q --reload
  info "firewalld updated"
elif command -v iptables >/dev/null; then
  for p in 443 80; do
    if ! iptables -C INPUT -p tcp -m state --state NEW --dport $p -j ACCEPT 2>/dev/null; then
      LINE="$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')"
      if [[ -n "$LINE" ]]; then iptables -I INPUT "$LINE" -p tcp -m state --state NEW --dport $p -j ACCEPT
      else iptables -A INPUT -p tcp -m state --state NEW --dport $p -j ACCEPT; fi
    fi
  done
  if command -v netfilter-persistent >/dev/null; then netfilter-persistent save >/dev/null 2>&1
  elif [[ -d /etc/iptables ]]; then iptables-save > /etc/iptables/rules.v4; fi
  info "iptables updated"
fi

# ------------------------------------------------------------------ start
step "Starting services"
systemctl daemon-reload
systemctl enable -q mydrive caddy
systemctl restart mydrive
for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 1; done
curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null || { journalctl -u mydrive -n 30 --no-pager; die "MyDrive did not start"; }
systemctl restart caddy
info "MyDrive and Caddy are running"

# ------------------------------------------------------------------ first admin
if [[ -z "$(sudo -u mydrive sqlite3 "$DATA_DIR/mydrive.db" 'SELECT 1 FROM users WHERE is_admin=1 LIMIT 1' 2>/dev/null)" ]]; then
  step "Create your admin account"
  while true; do
    U="$(ask "Admin username (lowercase, e.g. prashant):")"
    /usr/local/bin/mydrive-admin create-user "$U" --admin </dev/tty && break
  done
fi

# ------------------------------------------------------------------ done
echo
echo "${c_grn}${c_bold}✔ MyDrive is installed.${c_off}"
cat <<EOF

  Open:        ${c_bold}https://$DOMAIN${c_off}
  Data:        $DATA_DIR  ($(df -h --output=avail "$DATA_DIR" | tail -1 | tr -d ' ') free)
  Manage:      sudo mydrive-admin list-users | create-user NAME | reset-password NAME
  Logs:        journalctl -u mydrive -f      journalctl -u caddy -f

  ${c_yel}One step in the Oracle console is required if you have not done it yet:${c_off}
  Networking → Virtual cloud networks → your VCN → Security Lists → Default Security List
  → Add Ingress Rules:  Source CIDR 0.0.0.0/0, TCP, destination port 80   (and again for 443)

  The HTTPS certificate is issued automatically the first time the site is reachable
  (can take a minute after opening the ports).
EOF
