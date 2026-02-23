#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="librenms-port-bandwidth-alert"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"
TIMER_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.timer"
ENV_FILE="/etc/librenms-port-bandwidth-alert.env"
RUN_TEST=1

for arg in "$@"; do
  case "${arg}" in
    --skip-test)
      RUN_TEST=0
      ;;
    *)
      echo "Usage: $0 [--skip-test]"
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
SERVICE_TEMPLATE="${SCRIPT_DIR}/${SERVICE_NAME}.service"
TIMER_TEMPLATE="${SCRIPT_DIR}/${SERVICE_NAME}.timer"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root, for example: sudo $0"
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found; this host does not appear to use systemd."
  exit 1
fi

if [[ ! -f "${SERVICE_TEMPLATE}" ]]; then
  echo "Missing service template: ${SERVICE_TEMPLATE}"
  exit 1
fi

if [[ ! -f "${TIMER_TEMPLATE}" ]]; then
  echo "Missing timer template: ${TIMER_TEMPLATE}"
  exit 1
fi

install -m 0644 "${SERVICE_TEMPLATE}" "${SERVICE_FILE}"
install -m 0644 "${TIMER_TEMPLATE}" "${TIMER_FILE}"

PROJECT_DIR_ESCAPED="$(printf '%s' "${PROJECT_DIR}" | sed 's/[&|]/\\&/g')"
sed -i \
  -e "s|^WorkingDirectory=.*|WorkingDirectory=${PROJECT_DIR_ESCAPED}|" \
  -e "s|^ExecStart=.*|ExecStart=/usr/bin/python3 -m src.librenms_port_bandwidth_alert|" \
  "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.timer"

if [[ "${RUN_TEST}" -eq 1 ]]; then
  if [[ -f "${ENV_FILE}" ]]; then
    if ! systemctl start "${SERVICE_NAME}.service"; then
      echo "Warning: service test run failed, but units were installed/updated."
      echo "This is usually a configuration issue in ${ENV_FILE}."
      systemctl status "${SERVICE_NAME}.service" --no-pager -l || true
    fi
  else
    echo "Note: ${ENV_FILE} does not exist yet, skipped test start."
  fi
else
  echo "Skipped service test run (--skip-test)."
fi

echo "Installed/updated ${SERVICE_NAME}.service and ${SERVICE_NAME}.timer"
echo "Active unit values:"
systemctl cat "${SERVICE_NAME}.service" | grep -E "WorkingDirectory|ExecStart" || true
