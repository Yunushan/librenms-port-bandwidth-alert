# LibreNMS Port Bandwidth Email Alert

A small, MIT-licensed helper that checks **one specific LibreNMS port** and sends an email if its traffic is **sustained above a threshold** (default: **50 Mbps**) over the **last hour**.

This is handy when you want a simple “watch this uplink” style notification without changing LibreNMS alert rules.

## How it works

- Reads the port traffic from LibreNMS **RRD files** (fast + accurate).
- Computes bits/sec from the RRD samples for the last `WINDOW_SECONDS` (default: 3600).
- Triggers an alert if the traffic is above `THRESHOLD_MBPS` for at least `MIN_FRACTION_ABOVE` of samples (default: `1.0` = “continuously”).
- Sends an email to `EMAIL_TO`.
- Intended to be executed **hourly** (cron or systemd timer).

> Note: LibreNMS itself already has a powerful alerting engine. This project is for cases where you want a small standalone check,
> or you prefer “one-port, one-script” simplicity.

---

## Requirements

- Linux host with access to LibreNMS RRD files (usually the LibreNMS server)
- `rrdtool` installed
- Python 3.9+

On Debian/Ubuntu:
```bash
sudo apt-get update
sudo apt-get install -y rrdtool python3 python3-venv
```

On Rocky/RHEL:
```bash
sudo dnf install -y rrdtool python3
```

---

## Install

```bash
git clone <THIS_REPO_URL>
cd librenms-port-bandwidth-alert
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.env .env
```

Edit `.env` and set your variables (see below).

---

## Configuration (environment variables)

Minimum required:

- `DEVICE_HOSTNAME` — LibreNMS device hostname (RRD folder name), e.g. `router1.example`
- `PORT_ID` — LibreNMS port_id (integer)
- `EMAIL_TO` — recipient email (comma-separated allowed)

Optional but common:

- `RRD_BASE_DIR` — defaults to `/opt/librenms/rrd`
- `THRESHOLD_MBPS` — defaults to `50`
- `MODE` — `max` (default), `sum`, `in`, `out`
- `WINDOW_SECONDS` — defaults to `3600` (1 hour)
- `MIN_FRACTION_ABOVE` — defaults to `1.0` (1.0 = all samples above threshold)
- `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_STARTTLS`
  - If not set, the script uses local `sendmail` (if available)

### Finding `PORT_ID`

In the LibreNMS UI, click the port and check the URL, which typically contains a port id (e.g. `/port/12345`).
If your UI URLs are different, you can also query the LibreNMS DB or use LibreNMS API.

---

## Run manually

```bash
source .venv/bin/activate
set -a; source .env; set +a
python -m src.librenms_port_bandwidth_alert
```

---

## Run hourly (systemd timer)

1) Create an env file:
```bash
sudo cp config.example.env /etc/librenms-port-bandwidth-alert.env
sudo nano /etc/librenms-port-bandwidth-alert.env
```

2) Install/update systemd units automatically:
```bash
sudo bash deploy/systemd/install-systemd.sh
```
This command is idempotent, so you can run it again after every `git pull`.
To skip the test run during install, use:
```bash
sudo bash deploy/systemd/install-systemd.sh --skip-test
```

Logs:
```bash
journalctl -u librenms-port-bandwidth-alert.service -f
```

---

## Security notes

- If you’re aiming for maximum SNMP security, disable SNMPv1/v2c on devices and use SNMPv3 `authPriv`.
- This script does **not** store secrets in the repo. Put secrets only in your `.env` or system env / secret manager.

---

## License

MIT. See `LICENSE`.
