#!/usr/bin/env python3
"""
LibreNMS Port Bandwidth Email Alert

- Reads port traffic from LibreNMS RRD files
- Supports monitoring one specific port or all ports on a device
- Sends an email if sustained Mbps over last hour exceeds threshold
- Intended to run hourly (cron/systemd timer)
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
import smtplib
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
    port_id: int | None
    monitor_all_ports: bool
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
    librenms_api_url: str | None
    librenms_api_token: str | None
    port_name_include: list[str]

    state_file: str | None
    cooldown_seconds: int

    debug: bool


@dataclass
class PortEvaluation:
    port_id: int | None
    port_name: str | None
    rrd_file: str
    detail: dict
    stats: dict


def load_config() -> Config:
    # Load .env if python-dotenv is installed and .env exists.
    if load_dotenv is not None and os.path.exists(".env"):
        load_dotenv(".env", override=False)

    device_hostname = os.getenv("DEVICE_HOSTNAME", "").strip()
    port_id_str = os.getenv("PORT_ID", "").strip()
    monitor_all_ports = env_bool("MONITOR_ALL_PORTS", False)
    rrd_file = (os.getenv("RRD_FILE", "").strip() or None)
    librenms_api_url = (os.getenv("LIBRENMS_API_URL", "").strip() or None)
    librenms_api_token = (os.getenv("LIBRENMS_API_TOKEN", "").strip() or None)
    port_name_include = [x.strip() for x in os.getenv("PORT_NAME_INCLUDE", "").split(",") if x.strip()]

    if not device_hostname:
        raise SystemExit("DEVICE_HOSTNAME is required")
    if monitor_all_ports and rrd_file:
        raise SystemExit("MONITOR_ALL_PORTS=true cannot be used with RRD_FILE")
    if not monitor_all_ports and not rrd_file and not port_id_str.isdigit():
        raise SystemExit("PORT_ID is required and must be an integer (unless RRD_FILE is set)")
    if port_id_str and not port_id_str.isdigit():
        raise SystemExit("PORT_ID must be an integer")
    if port_name_include and (not librenms_api_url or not librenms_api_token):
        raise SystemExit("PORT_NAME_INCLUDE requires LIBRENMS_API_URL and LIBRENMS_API_TOKEN")

    email_to_raw = os.getenv("EMAIL_TO", "").strip()
    if not email_to_raw:
        raise SystemExit("EMAIL_TO is required")
    email_to = [e.strip() for e in email_to_raw.split(",") if e.strip()]

    return Config(
        device_hostname=device_hostname,
        port_id=(int(port_id_str) if port_id_str else None),
        monitor_all_ports=monitor_all_ports,
        threshold_mbps=env_float("THRESHOLD_MBPS", 50.0),
        mode=os.getenv("MODE", "max").strip().lower(),
        window_seconds=env_int("WINDOW_SECONDS", 3600),
        min_fraction_above=env_float("MIN_FRACTION_ABOVE", 1.0),
        min_points=env_int("MIN_POINTS", 4),
        rrd_base_dir=os.getenv("RRD_BASE_DIR", "/opt/librenms/rrd").strip(),
        rrd_file=rrd_file,

        email_to=email_to,
        email_from=os.getenv("EMAIL_FROM", "librenms-alert@example.com").strip(),
        email_subject_prefix=os.getenv("EMAIL_SUBJECT_PREFIX", "[Port Bandwidth Alert]").strip(),

        smtp_host=(os.getenv("SMTP_HOST", "").strip() or None),
        smtp_port=env_int("SMTP_PORT", 587),
        smtp_user=(os.getenv("SMTP_USER", "").strip() or None),
        smtp_pass=(os.getenv("SMTP_PASS", "").strip() or None),
        smtp_starttls=env_bool("SMTP_STARTTLS", True),
        librenms_api_url=librenms_api_url,
        librenms_api_token=librenms_api_token,
        port_name_include=port_name_include,

        state_file=(os.getenv("STATE_FILE", "").strip() or None),
        cooldown_seconds=env_int("COOLDOWN_SECONDS", 0),

        debug=env_bool("DEBUG", False),
    )


def debug(cfg: Config, msg: str) -> None:
    if cfg.debug:
        print(f"[DEBUG] {msg}")


def parse_port_id_from_rrd(rrd_file: str) -> int | None:
    base = os.path.basename(rrd_file)
    for pattern in (r"^port-id(\d+)\.rrd$", r"^port-(\d+)\.rrd$", r"^portid(\d+)\.rrd$"):
        m = re.match(pattern, base)
        if m:
            return int(m.group(1))
    return None


def find_rrd_file(cfg: Config) -> str:
    if cfg.rrd_file:
        if not os.path.exists(cfg.rrd_file):
            raise SystemExit(f"RRD_FILE does not exist: {cfg.rrd_file}")
        return cfg.rrd_file
    if cfg.port_id is None:
        raise SystemExit("PORT_ID is required unless RRD_FILE is set")

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


def find_all_port_rrd_files(cfg: Config) -> list[str]:
    device_dir = os.path.join(cfg.rrd_base_dir, cfg.device_hostname)
    if not os.path.isdir(device_dir):
        raise SystemExit(f"Device RRD directory not found: {device_dir}")

    patterns = [
        os.path.join(device_dir, "port-id*.rrd"),
        os.path.join(device_dir, "port-*.rrd"),
        os.path.join(device_dir, "portid*.rrd"),
    ]
    matches: list[str] = []
    for p in patterns:
        matches.extend(glob.glob(p))
    matches = sorted(set(matches))
    if not matches:
        raise SystemExit(f"No port RRD files found under {device_dir}")
    return matches


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


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem_minutes = minutes % 60
    if rem_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {rem_minutes}m"


def mode_description(mode: str) -> str:
    if mode == "in":
        return "incoming only"
    if mode == "out":
        return "outgoing only"
    if mode == "sum":
        return "incoming + outgoing"
    return "higher of incoming/outgoing"


def required_time_text(cfg: Config) -> str:
    required_seconds = int(round(cfg.window_seconds * cfg.min_fraction_above))
    required_percent = cfg.min_fraction_above * 100.0
    return f"{required_percent:.1f}% (~{format_duration(required_seconds)})"


def above_time_text(cfg: Config, fraction_above: float) -> str:
    above_seconds = int(round(cfg.window_seconds * fraction_above))
    return f"{fraction_above * 100.0:.1f}% (~{format_duration(above_seconds)})"


def format_port_label(port_id: int | None, port_name: str | None) -> str:
    if port_name:
        return port_name
    if port_id is not None:
        return str(port_id)
    return "unknown"


def port_name_matches_filter(port_name: str | None, includes: list[str]) -> bool:
    if not includes:
        return True
    if not port_name:
        return False
    name = port_name.lower()
    return any(term.lower() in name for term in includes)


def normalize_api_base(url: str) -> str:
    u = url.strip().rstrip("/")
    if u.endswith("/api/v0"):
        return u
    return f"{u}/api/v0"


def api_get_json(cfg: Config, path: str) -> dict | None:
    if not cfg.librenms_api_url or not cfg.librenms_api_token:
        return None
    base = normalize_api_base(cfg.librenms_api_url)
    url = f"{base}/{path.lstrip('/')}"
    req = Request(
        url,
        headers={
            "X-Auth-Token": cfg.librenms_api_token,
            "Accept": "application/json",
            "User-Agent": "librenms-port-bandwidth-alert",
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError) as e:
        debug(cfg, f"LibreNMS API request failed: {url} -> {e}")
        return None
    except OSError as e:
        debug(cfg, f"LibreNMS API request failed: {url} -> {e}")
        return None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        debug(cfg, f"LibreNMS API invalid JSON from {url}: {e}")
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def pick_port_payload(payload: dict, port_id: int) -> dict | None:
    candidates: list[dict] = []
    for key in ("port", "ports", "data"):
        v = payload.get(key)
        if isinstance(v, dict):
            candidates.append(v)
        elif isinstance(v, list):
            candidates.extend([x for x in v if isinstance(x, dict)])
    if not candidates and isinstance(payload, dict) and ("port_id" in payload or "ifName" in payload):
        candidates = [payload]

    pid = str(port_id)
    for item in candidates:
        item_pid = str(item.get("port_id", item.get("id", ""))).strip()
        if item_pid == pid:
            return item
    return candidates[0] if candidates else None


def port_name_from_payload(port_payload: dict) -> str | None:
    if_name = str(port_payload.get("ifName", "")).strip()
    if_alias = str(port_payload.get("ifAlias", "")).strip()
    if_descr = str(port_payload.get("ifDescr", "")).strip()
    if if_name and if_alias and if_alias != if_name:
        return f"{if_name} ({if_alias})"
    if if_name:
        return if_name
    if if_alias:
        return if_alias
    if if_descr:
        return if_descr
    return None


def resolve_alert_port_names(cfg: Config, alerts: list[PortEvaluation]) -> None:
    if not cfg.librenms_api_url or not cfg.librenms_api_token:
        return
    unique_ids = sorted({a.port_id for a in alerts if a.port_id is not None})
    if not unique_ids:
        return

    names: dict[int, str] = {}
    for port_id in unique_ids:
        payload = api_get_json(cfg, f"ports/{port_id}")
        if payload is None:
            continue
        port_payload = pick_port_payload(payload, port_id)
        if not port_payload:
            continue
        name = port_name_from_payload(port_payload)
        if name:
            names[port_id] = name

    for alert in alerts:
        if alert.port_id is not None:
            alert.port_name = names.get(alert.port_id)


def build_single_port_email(cfg: Config, result: PortEvaluation) -> tuple[str, str]:
    port_label = format_port_label(result.port_id, result.port_name)
    detail = result.detail
    stats = result.stats

    subject = (
        f"{cfg.email_subject_prefix} {cfg.device_hostname} "
        f"port {port_label} exceeded {cfg.threshold_mbps:.1f} Mbps"
    )
    body = "\n".join(
        [
            "LibreNMS Port Bandwidth Alert",
            "",
            f"Device: {cfg.device_hostname}",
            f"Port ID: {port_label}",
            "",
            "Rule:",
            f"- Threshold: >= {cfg.threshold_mbps:.2f} Mbps",
            f"- Window: last {format_duration(cfg.window_seconds)}",
            f"- Required time above threshold: {required_time_text(cfg)}",
            f"- Traffic mode: {cfg.mode} ({mode_description(cfg.mode)})",
            (
                f"- Port name filter: contains any of {', '.join(cfg.port_name_include)}"
                if cfg.port_name_include
                else "- Port name filter: disabled"
            ),
            "",
            "Observed for this port:",
            f"- Above threshold time: {above_time_text(cfg, detail.get('fraction_above', 0.0))}",
            f"- Peak bandwidth: {stats['max_mbps']:.2f} Mbps",
            f"- Average bandwidth: {stats['avg_mbps']:.2f} Mbps",
            f"- Avg in/out: {stats['avg_in_mbps']:.2f} / {stats['avg_out_mbps']:.2f} Mbps",
            f"- Samples used: {stats['points']}",
            "",
            "Action ideas:",
            "- Verify whether this traffic is expected (backup, replication, large downloads).",
            "- If unexpected, check top talkers / flows on the upstream device.",
            "- Consider adding an official LibreNMS alert rule for long-term management.",
            "",
            f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        ]
    )
    return subject, body


def build_all_ports_email(cfg: Config, alerted: list[PortEvaluation], checked: int, errors: list[str]) -> tuple[str, str]:
    subject = (
        f"{cfg.email_subject_prefix} {cfg.device_hostname} "
        f"{len(alerted)} port(s) exceeded {cfg.threshold_mbps:.1f} Mbps"
    )

    lines = [
        "LibreNMS Port Bandwidth Alert",
        "",
        f"Device: {cfg.device_hostname}",
        "Scope: all ports on this device",
        "",
        "Rule:",
        f"- Threshold: >= {cfg.threshold_mbps:.2f} Mbps",
        f"- Window: last {format_duration(cfg.window_seconds)}",
        f"- Required time above threshold: {required_time_text(cfg)}",
        f"- Traffic mode: {cfg.mode} ({mode_description(cfg.mode)})",
        (
            f"- Port name filter: contains any of {', '.join(cfg.port_name_include)}"
            if cfg.port_name_include
            else "- Port name filter: disabled"
        ),
        "",
        f"Ports checked: {checked}",
        f"Ports above threshold: {len(alerted)}",
        "",
        "Ports above threshold (sorted by peak bandwidth):",
    ]

    for hit in sorted(alerted, key=lambda x: x.stats.get("max_mbps", 0.0), reverse=True):
        port_label = format_port_label(hit.port_id, hit.port_name)
        lines.append(
            "- "
            + f"Port {port_label}: "
            + f"peak {hit.stats['max_mbps']:.2f} Mbps, "
            + f"avg {hit.stats['avg_mbps']:.2f} Mbps, "
            + f"above {above_time_text(cfg, hit.detail.get('fraction_above', 0.0))}, "
            + f"samples {hit.stats['points']}"
        )

    if errors:
        lines.append("")
        lines.append("Skipped RRD files (read/parse errors):")
        for err in errors[:20]:
            lines.append(f"  {err}")
        if len(errors) > 20:
            lines.append(f"  ... and {len(errors) - 20} more")

    lines.extend(
        [
            "",
            "Action ideas:",
            "- Verify whether this traffic is expected (backup, replication, large downloads).",
            "- If unexpected, check top talkers / flows on the upstream device.",
            "- Consider adding an official LibreNMS alert rule for long-term management.",
            "",
            f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        ]
    )
    return subject, "\n".join(lines)


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

    if cfg.monitor_all_ports:
        rrd_files = find_all_port_rrd_files(cfg)
    else:
        rrd_files = [find_rrd_file(cfg)]

    alerts: list[PortEvaluation] = []
    errors: list[str] = []
    checked = 0
    non_alert_detail: dict = {}
    non_alert_stats: dict = {}

    for rrd_file in rrd_files:
        port_id = parse_port_id_from_rrd(rrd_file)
        try:
            ds_names, data = run_rrdtool_fetch(cfg, rrd_file, start_ts, now)
            series, stats = compute_series_mbps(cfg, ds_names, data)
            ok, detail = should_alert(cfg, series)
        except SystemExit as e:
            if cfg.monitor_all_ports:
                msg = f"{rrd_file}: {str(e)}"
                errors.append(msg)
                debug(cfg, f"Skipping unreadable RRD: {msg}")
                continue
            raise

        checked += 1
        if ok:
            alerts.append(
                PortEvaluation(
                    port_id=(port_id if port_id is not None else cfg.port_id),
                    port_name=None,
                    rrd_file=rrd_file,
                    detail=detail,
                    stats=stats,
                )
            )
            continue

        if cfg.monitor_all_ports:
            debug(
                cfg,
                f"No alert for port_id={port_id if port_id is not None else 'unknown'}. "
                + f"Detail: {detail} Stats: {stats}",
            )
        else:
            non_alert_detail = detail
            non_alert_stats = stats

    if checked == 0:
        if cfg.monitor_all_ports and errors:
            raise SystemExit("No readable port RRD files found. First errors:\n" + "\n".join(errors[:20]))
        raise SystemExit("No readable port RRD files found.")

    if not alerts:
        if cfg.monitor_all_ports:
            debug(cfg, f"No alert. Checked {checked} ports. Skipped {len(errors)} files.")
        else:
            debug(cfg, f"No alert. Detail: {non_alert_detail} Stats: {non_alert_stats}")
        return 0

    resolve_alert_port_names(cfg, alerts)
    if cfg.port_name_include:
        before = len(alerts)
        alerts = [a for a in alerts if port_name_matches_filter(a.port_name, cfg.port_name_include)]
        debug(cfg, f"Port name filter kept {len(alerts)} of {before} alerting ports.")
        if not alerts:
            debug(cfg, "No alert after applying port-name filter.")
            return 0

    state = load_state(cfg.state_file) if cfg.state_file else {}
    if alerts and within_cooldown(cfg, now, state):
        debug(cfg, "Alert condition met, but within cooldown. Skipping email.")
        return 0

    if cfg.monitor_all_ports:
        subject, body = build_all_ports_email(cfg, alerts, checked, errors)
    else:
        subject, body = build_single_port_email(cfg, alerts[0])

    send_email(cfg, subject, body)

    if cfg.state_file:
        state["last_sent_epoch"] = now
        state["last_subject"] = subject
        state["last_alerted_count"] = len(alerts)
        state["last_alerted_ports"] = [a.port_id for a in alerts if a.port_id is not None]
        try:
            save_state(cfg.state_file, state)
        except OSError as e:
            # Do not fail the whole run after sending mail if state persistence is not writable.
            print(f"Warning: could not save STATE_FILE={cfg.state_file}: {e}", file=sys.stderr)

    if cfg.monitor_all_ports:
        print(f"Alert sent for {len(alerts)} port(s).")
    else:
        print("Alert sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
