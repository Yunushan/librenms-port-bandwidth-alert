#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="librenms-port-bandwidth-alert"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"
TIMER_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.timer"
ENV_FILE="/etc/librenms-port-bandwidth-alert.env"
DEFAULT_STATE_FILE="/var/lib/${SERVICE_NAME}/state.json"
RUN_TEST=0

for arg in "$@"; do
  case "${arg}" in
    --run-test)
      RUN_TEST=1
      ;;
    --skip-test)
      RUN_TEST=0
      ;;
    *)
      echo "Usage: $0 [--run-test|--skip-test]"
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

ensure_state_file_writable() {
  local state_line
  local state_file
  local state_dir
  local original_relative

  if [[ ! -f "${ENV_FILE}" ]]; then
    return
  fi

  state_line="$(grep -E '^[[:space:]]*STATE_FILE=' "${ENV_FILE}" | tail -n1 || true)"
  if [[ -z "${state_line}" ]]; then
    state_file="${DEFAULT_STATE_FILE}"
    echo "STATE_FILE=${state_file}" >> "${ENV_FILE}"
    echo "Added STATE_FILE=${state_file} to ${ENV_FILE}"
  else
    state_file="${state_line#*=}"
    state_file="${state_file#"${state_file%%[![:space:]]*}"}"
    state_file="${state_file%"${state_file##*[![:space:]]}"}"
    state_file="${state_file%\"}"
    state_file="${state_file#\"}"
    state_file="${state_file%\'}"
    state_file="${state_file#\'}"

    if [[ -z "${state_file}" ]]; then
      echo "STATE_FILE is empty in ${ENV_FILE}; state persistence is disabled."
      return
    fi

    if [[ "${state_file#/}" == "${state_file}" ]]; then
      original_relative="${state_file}"
      state_file="${DEFAULT_STATE_FILE}"
      sed -i "s|^[[:space:]]*STATE_FILE=.*|STATE_FILE=${state_file}|" "${ENV_FILE}"
      echo "Replaced relative STATE_FILE=${original_relative} with ${state_file} in ${ENV_FILE}"
    fi
  fi

  if id -u librenms >/dev/null 2>&1 && getent group librenms >/dev/null 2>&1; then
    state_dir="$(dirname -- "${state_file}")"
    install -d -m 0750 -o librenms -g librenms "${state_dir}"
    touch "${state_file}"
    chown librenms:librenms "${state_file}"
    chmod 0640 "${state_file}"
  else
    echo "Warning: user/group librenms not found; could not set STATE_FILE ownership."
  fi
}

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

ensure_state_file_writable

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
  echo "Skipped service test run (default). Use --run-test to execute one immediate run."
fi

echo "Installed/updated ${SERVICE_NAME}.service and ${SERVICE_NAME}.timer"
echo "Active unit values:"
systemctl cat "${SERVICE_NAME}.service" | grep -E "WorkingDirectory|ExecStart" || true
