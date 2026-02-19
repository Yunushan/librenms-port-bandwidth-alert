#!/usr/bin/env python3
"""
LibreNMS Port Bandwidth Email Alert

- Reads port traffic from LibreNMS RRD files
- Sends an email if sustained Mbps over last hour exceeds threshold
- Intended to run hourly (cron/systemd timer)
"""
from __future__ import annotations

import os
import sys
import time
import json
import math
import glob
import shlex
import socket
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
import smtplib

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return int(v)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return float(v)


@dataclass
class Config:
    device_hostname: str
    port_id: int
    threshold_mbps: float
    mode: str
    window_seconds: int
    min_fraction_above: float
    min_points: int
    rrd_base_dir: str
    rrd_file: str | None

    email_to: list[str]
    email_from: str
    email_subject_prefix: str

    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_pass: str | None
    smtp_starttls: bool

    state_file: str | None
    cooldown_seconds: int

    debug: bool


def load_config() -> Config:
    # Load .env if python-dotenv is installed and .env exists.
    if load_dotenv is not None and os.path.exists(".env"):
        load_dotenv(".env", override=False)

    device_hostname = os.getenv("DEVICE_HOSTNAME", "").strip()
    port_id_str = os.getenv("PORT_ID", "").strip()

    if not device_hostname:
        raise SystemExit("DEVICE_HOSTNAME is required")
    if not port_id_str.isdigit():
        raise SystemExit("PORT_ID is required and must be an integer")

    email_to_raw = os.getenv("EMAIL_TO", "").strip()
    if not email_to_raw:
        raise SystemExit("EMAIL_TO is required")
    email_to = [e.strip() for e in email_to_raw.split(",") if e.strip()]

    return Config(
        device_hostname=device_hostname,
        port_id=int(port_id_str),
        threshold_mbps=env_float("THRESHOLD_MBPS", 50.0),
        mode=os.getenv("MODE", "max").strip().lower(),
        window_seconds=env_int("WINDOW_SECONDS", 3600),
        min_fraction_above=env_float("MIN_FRACTION_ABOVE", 1.0),
        min_points=env_int("MIN_POINTS", 4),
        rrd_base_dir=os.getenv("RRD_BASE_DIR", "/opt/librenms/rrd").strip(),
        rrd_file=(os.getenv("RRD_FILE", "").strip() or None),

        email_to=email_to,
        email_from=os.getenv("EMAIL_FROM", "librenms-alert@example.com").strip(),
        email_subject_prefix=os.getenv("EMAIL_SUBJECT_PREFIX", "[Port Bandwidth Alert]").strip(),

        smtp_host=(os.getenv("SMTP_HOST", "").strip() or None),
        smtp_port=env_int("SMTP_PORT", 587),
        smtp_user=(os.getenv("SMTP_USER", "").strip() or None),
        smtp_pass=(os.getenv("SMTP_PASS", "").strip() or None),
        smtp_starttls=env_bool("SMTP_STARTTLS", True),

        state_file=(os.getenv("STATE_FILE", "").strip() or None),
        cooldown_seconds=env_int("COOLDOWN_SECONDS", 0),

        debug=env_bool("DEBUG", False),
    )


def debug(cfg: Config, msg: str) -> None:
    if cfg.debug:
        print(f"[DEBUG] {msg}")


def find_rrd_file(cfg: Config) -> str:
    if cfg.rrd_file:
        if not os.path.exists(cfg.rrd_file):
            raise SystemExit(f"RRD_FILE does not exist: {cfg.rrd_file}")
        return cfg.rrd_file

    device_dir = os.path.join(cfg.rrd_base_dir, cfg.device_hostname)
    if not os.path.isdir(device_dir):
        raise SystemExit(f"Device RRD directory not found: {device_dir}")

    candidates = [
        os.path.join(device_dir, f"port-id{cfg.port_id}.rrd"),
        os.path.join(device_dir, f"port-{cfg.port_id}.rrd"),
        os.path.join(device_dir, f"portid{cfg.port_id}.rrd"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    # Fallback: search for any port RRD containing the port id.
    patterns = [
        os.path.join(device_dir, f"port*{cfg.port_id}*.rrd"),
        os.path.join(device_dir, f"*{cfg.port_id}*.rrd"),
    ]
    matches: list[str] = []
    for p in patterns:
        matches.extend(glob.glob(p))
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(
            "Multiple RRD candidates found. Set RRD_FILE explicitly.\n"
            + "\n".join(matches[:20])
        )
    raise SystemExit(
        f"Could not find port RRD for port_id={cfg.port_id} under {device_dir}. "
        "Set RRD_FILE explicitly."
    )


def run_rrdtool_fetch(cfg: Config, rrd_file: str, start_ts: int, end_ts: int) -> tuple[list[str], list[tuple[int, list[float]]]]:
    cmd = ["rrdtool", "fetch", rrd_file, "AVERAGE", "--start", str(start_ts), "--end", str(end_ts)]
    debug(cfg, "Running: " + " ".join(shlex.quote(x) for x in cmd))
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise SystemExit("rrdtool not found. Install it (e.g. apt/dnf install rrdtool).")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"rrdtool fetch failed:\n{e.output}")

    lines = [ln.rstrip() for ln in out.splitlines() if ln.strip() != ""]
    if not lines:
        raise SystemExit("rrdtool returned no data")

    # Header: DS names
    header = lines[0].split()
    ds_names = header

    data: list[tuple[int, list[float]]] = []
    for ln in lines[1:]:
        if ":" not in ln:
            continue
        ts_part, vals_part = ln.split(":", 1)
        ts = ts_part.strip()
        if not ts.isdigit():
            continue
        vals = vals_part.strip().split()
        if len(vals) < len(ds_names):
            continue
        parsed: list[float] = []
        for v in vals[: len(ds_names)]:
            if v.lower() == "nan":
                parsed.append(math.nan)
            else:
                try:
                    parsed.append(float(v))
                except ValueError:
                    parsed.append(math.nan)
        data.append((int(ts), parsed))

    return ds_names, data


def pick_in_out_indexes(ds_names: list[str]) -> tuple[int, int]:
    # Common LibreNMS port DS names: INOCTETS / OUTOCTETS (case varies).
    lowered = [d.lower() for d in ds_names]

    def find_idx(patterns: list[str]) -> int:
        for p in patterns:
            for i, name in enumerate(lowered):
                if p in name:
                    return i
        return -1

    in_idx = find_idx(["inoct", "in_oct", "inbytes", "rxoct", "rx_oct", "in"])
    out_idx = find_idx(["outoct", "out_oct", "outbytes", "txoct", "tx_oct", "out"])

    if in_idx == -1 or out_idx == -1:
        raise SystemExit(
            "Could not detect IN/OUT data sources in RRD. DS names were: "
            + ", ".join(ds_names)
            + "\nSet RRD_FILE to the correct port RRD and ensure it contains IN/OUT octets."
        )
    return in_idx, out_idx


def compute_series_mbps(cfg: Config, ds_names: list[str], data: list[tuple[int, list[float]]]) -> tuple[list[float], dict]:
    in_idx, out_idx = pick_in_out_indexes(ds_names)

    series: list[float] = []
    in_series: list[float] = []
    out_series: list[float] = []

    for _ts, vals in data:
        in_v = vals[in_idx]
        out_v = vals[out_idx]
        if math.isnan(in_v) or math.isnan(out_v):
            continue

        # rrdtool fetch on COUNTER yields per-second rate; for octets, this is bytes/sec.
        in_bps = in_v * 8.0
        out_bps = out_v * 8.0

        in_mbps = in_bps / 1_000_000.0
        out_mbps = out_bps / 1_000_000.0

        in_series.append(in_mbps)
        out_series.append(out_mbps)

        if cfg.mode == "in":
            series.append(in_mbps)
        elif cfg.mode == "out":
            series.append(out_mbps)
        elif cfg.mode == "sum":
            series.append(in_mbps + out_mbps)
        else:  # "max"
            series.append(max(in_mbps, out_mbps))

    stats = {
        "points": len(series),
        "avg_mbps": (sum(series) / len(series)) if series else math.nan,
        "max_mbps": (max(series)) if series else math.nan,
        "avg_in_mbps": (sum(in_series) / len(in_series)) if in_series else math.nan,
        "avg_out_mbps": (sum(out_series) / len(out_series)) if out_series else math.nan,
        "max_in_mbps": (max(in_series)) if in_series else math.nan,
        "max_out_mbps": (max(out_series)) if out_series else math.nan,
    }
    return series, stats


def should_alert(cfg: Config, series: list[float]) -> tuple[bool, dict]:
    if len(series) < cfg.min_points:
        return False, {"reason": f"Not enough valid samples: {len(series)} < {cfg.min_points}"}

    above = [v for v in series if v >= cfg.threshold_mbps]
    frac = len(above) / len(series) if series else 0.0
    return (frac >= cfg.min_fraction_above), {"fraction_above": frac, "samples": len(series), "above": len(above)}


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def within_cooldown(cfg: Config, now: int, state: dict) -> bool:
    if not cfg.state_file or cfg.cooldown_seconds <= 0:
        return False
    last = state.get("last_sent_epoch", 0)
    return (now - int(last)) < cfg.cooldown_seconds


def send_email(cfg: Config, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.email_from
    msg["To"] = ", ".join(cfg.email_to)
    msg["Subject"] = subject
    msg.set_content(body)

    if cfg.smtp_host:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as s:
            s.ehlo()
            if cfg.smtp_starttls:
                s.starttls()
                s.ehlo()
            if cfg.smtp_user and cfg.smtp_pass:
                s.login(cfg.smtp_user, cfg.smtp_pass)
            s.send_message(msg)
        return

    # Fallback: local sendmail
    for sendmail_path in ("/usr/sbin/sendmail", "/usr/lib/sendmail"):
        if os.path.exists(sendmail_path):
            p = subprocess.Popen([sendmail_path, "-t", "-oi"], stdin=subprocess.PIPE, text=True)
            p.communicate(msg.as_string())
            if p.returncode != 0:
                raise SystemExit(f"sendmail failed with code {p.returncode}")
            return

    raise SystemExit("No SMTP_HOST configured and sendmail not found. Configure SMTP_HOST or install a local MTA.")


def main() -> int:
    cfg = load_config()
    if cfg.mode not in {"max", "sum", "in", "out"}:
        raise SystemExit("MODE must be one of: max, sum, in, out")

    now = int(time.time())
    start_ts = now - cfg.window_seconds

    rrd_file = find_rrd_file(cfg)
    ds_names, data = run_rrdtool_fetch(cfg, rrd_file, start_ts, now)
    series, stats = compute_series_mbps(cfg, ds_names, data)
    ok, detail = should_alert(cfg, series)

    state = load_state(cfg.state_file) if cfg.state_file else {}
    if ok and within_cooldown(cfg, now, state):
        debug(cfg, "Alert condition met, but within cooldown. Skipping email.")
        return 0

    if not ok:
        debug(cfg, f"No alert. Detail: {detail} Stats: {stats}")
        return 0

    # Compose email
    subject = f"{cfg.email_subject_prefix} {cfg.device_hostname} port_id={cfg.port_id} >= {cfg.threshold_mbps:.1f} Mbps ({cfg.mode})"
    body = "\n".join(
        [
            "LibreNMS Port Bandwidth Alert",
            "",
            f"Device: {cfg.device_hostname}",
            f"Port ID: {cfg.port_id}",
            f"RRD: {rrd_file}",
            "",
            f"Window: last {cfg.window_seconds} seconds",
            f"Threshold: {cfg.threshold_mbps:.2f} Mbps",
            f"Mode: {cfg.mode} (max=either direction, sum=in+out)",
            f"Condition: fraction_above={detail.get('fraction_above', 0):.3f} (required >= {cfg.min_fraction_above:.3f})",
            "",
            "Stats (Mbps):",
            f"  avg: {stats['avg_mbps']:.2f}    max: {stats['max_mbps']:.2f}",
            f"  avg_in: {stats['avg_in_mbps']:.2f}  max_in: {stats['max_in_mbps']:.2f}",
            f"  avg_out: {stats['avg_out_mbps']:.2f} max_out: {stats['max_out_mbps']:.2f}",
            f"  points: {stats['points']}",
            "",
            "Action ideas:",
            "- Verify whether this traffic is expected (backup, replication, large downloads).",
            "- If unexpected, check top talkers / flows on the upstream device.",
            "- Consider adding an official LibreNMS alert rule for long-term management.",
            "",
            f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        ]
    )

    send_email(cfg, subject, body)

    if cfg.state_file:
        state["last_sent_epoch"] = now
        state["last_subject"] = subject
        save_state(cfg.state_file, state)

    print("Alert sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
