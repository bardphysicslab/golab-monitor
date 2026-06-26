#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_CONFIG="/etc/default/golab-backup"
LOCAL_CONFIG="${PROJECT_ROOT}/deploy/golab-backup.env"

if [[ -n "${BACKUP_CONFIG:-}" ]]; then
  CONFIG_PATH="${BACKUP_CONFIG}"
elif [[ -f "${DEFAULT_CONFIG}" ]]; then
  CONFIG_PATH="${DEFAULT_CONFIG}"
else
  CONFIG_PATH="${LOCAL_CONFIG}"
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Backup config not found: ${CONFIG_PATH}" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "${CONFIG_PATH}"

required_vars=(
  RCLONE_REMOTE
  REMOTE_ROOT
  LOCAL_DATA_ROOT
  LOCAL_BACKUP_ROOT
  BACKUP_LOG
  BACKUP_STATUS
)

for var_name in "${required_vars[@]}"; do
  if [[ -z "${!var_name:-}" ]]; then
    echo "Required backup config value is missing: ${var_name}" >&2
    exit 2
  fi
done

mkdir -p "$(dirname "${BACKUP_LOG}")" "${LOCAL_BACKUP_ROOT}" "$(dirname "${BACKUP_STATUS}")"
touch "${BACKUP_LOG}" 2>/dev/null || {
  echo "Cannot write backup log: ${BACKUP_LOG}" >&2
  exit 2
}

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "${BACKUP_LOG}" >&2
}

check_log_writable() {
  local probe="log write probe: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  if printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${probe}" >>"${BACKUP_LOG}" 2>/dev/null; then
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "diagnostic: log write succeeded (${BACKUP_LOG})" | tee -a "${BACKUP_LOG}" >&2
    return 0
  fi
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "diagnostic: log write failed (${BACKUP_LOG})" >&2
  return 1
}

write_status() {
  local status="$1"
  local last_attempt="$2"
  local last_success="$3"
  local last_error="$4"
  local files_uploaded="$5"
  local destination="${RCLONE_REMOTE}:${REMOTE_ROOT%/}"
  local tmp_status="${BACKUP_STATUS}.$$"

  python3 -c '
import json
import sys

path, status, last_attempt, last_success, last_error, files_uploaded, destination, local_root = sys.argv[1:]
payload = {
    "status": status,
    "last_attempt": last_attempt or None,
    "last_success": last_success or None,
    "last_error": last_error or None,
    "files_uploaded": None if files_uploaded == "" else int(files_uploaded),
    "destination": destination,
    "local_data_root": local_root,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, separators=(",", ":"))
    handle.write("\n")
' "${tmp_status}" "${status}" "${last_attempt}" "${last_success}" "${last_error}" "${files_uploaded}" "${destination}" "${LOCAL_DATA_ROOT}" \
    && mv "${tmp_status}" "${BACKUP_STATUS}"
}

run_logged() {
  log "+ $*"
  "$@" >>"${BACKUP_LOG}" 2>&1
}

quote_command() {
  printf '%q ' "$@"
}

log_file_with_prefix() {
  local label="$1"
  local path="$2"
  if [[ ! -s "${path}" ]]; then
    log "rclone ${label}: <empty>"
    return 0
  fi
  while IFS= read -r line; do
    log "rclone ${label}: ${line}"
  done <"${path}"
}

run_rclone_copy() {
  local source="$1"
  local destination="$2"
  local report="$3"
  local stdout_path="${snapshot_dir}/rclone-stdout.txt"
  local stderr_path="${snapshot_dir}/rclone-stderr.txt"
  local -a cmd=(rclone copy "${source}" "${destination}" --combined "${report}")

  log "rclone command: $(quote_command "${cmd[@]}")"
  "${cmd[@]}" >"${stdout_path}" 2>"${stderr_path}"
  local rc=$?
  log "rclone exit code: ${rc}"
  log_file_with_prefix "stdout" "${stdout_path}"
  log_file_with_prefix "stderr" "${stderr_path}"
  return "${rc}"
}

write_status_logged() {
  if write_status "$@"; then
    log "diagnostic: status JSON write succeeded (${BACKUP_STATUS})"
    return 0
  fi
  log "diagnostic: status JSON write failed (${BACKUP_STATUS})"
  return 1
}

backup_file=""
backup_subdir=""
backup_reason="scheduled"

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --file)
      backup_file="${2:-}"
      backup_subdir="${3:-}"
      shift 3
      ;;
    --reason)
      backup_reason="${2:-scheduled}"
      shift 2
      ;;
    *)
      echo "Unknown backup argument: $1" >&2
      exit 2
      ;;
  esac
done

timestamp="$(date '+%Y-%m-%d_%H-%M-%S')"
attempt_utc="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
snapshot_dir="${LOCAL_BACKUP_ROOT}/${timestamp}"
snapshot_data_dir="${snapshot_dir}/data"
combined_report="${snapshot_dir}/rclone-combined.txt"
remote_base="${RCLONE_REMOTE}:${REMOTE_ROOT%/}"
previous_success="$(
  python3 -c 'import json,sys; p=sys.argv[1]
try:
    print((json.load(open(p, encoding="utf-8")) or {}).get("last_success") or "")
except Exception:
    print("")' "${BACKUP_STATUS}"
)"

log "backup started: ${timestamp}; reason=${backup_reason}"
log "config: remote=${RCLONE_REMOTE}, remote_root=${REMOTE_ROOT}, local_data_root=${LOCAL_DATA_ROOT}, local_backup_root=${LOCAL_BACKUP_ROOT}"
check_log_writable || true
write_status_logged "running" "${attempt_utc}" "${previous_success}" "" "" || true

if [[ ! -d "${LOCAL_DATA_ROOT}" ]]; then
  error="local data root does not exist: ${LOCAL_DATA_ROOT}"
  log "backup failed: ${error}"
  write_status_logged "failed" "${attempt_utc}" "${previous_success}" "${error}" ""
  exit 1
fi

if ! mkdir "${snapshot_dir}"; then
  error="snapshot directory already exists or cannot be created: ${snapshot_dir}"
  log "backup failed: ${error}"
  write_status_logged "failed" "${attempt_utc}" "${previous_success}" "${error}" ""
  exit 1
fi

if ! command -v rclone >/dev/null 2>&1; then
  error="rclone is not installed or not on PATH"
  log "backup failed: ${error}"
  write_status_logged "failed" "${attempt_utc}" "${previous_success}" "${error}" ""
  exit 1
fi

if [[ -n "${backup_file}" ]]; then
  if [[ ! -f "${backup_file}" ]]; then
    error="backup file does not exist: ${backup_file}"
    log "backup failed: ${error}"
    write_status_logged "failed" "${attempt_utc}" "${previous_success}" "${error}" ""
    exit 1
  fi
  file_stage="${snapshot_dir}/file"
  mkdir -p "${file_stage}"
  cp -p "${backup_file}" "${file_stage}/"
  file_count=1
  target="${remote_base}/${backup_subdir%/}"
  log "upload started: completed file ${backup_file} -> ${target}"
  if run_rclone_copy "${file_stage}" "${target}" "${combined_report}"; then
    success_utc="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    files_uploaded=1
    if [[ -f "${combined_report}" ]]; then
      files_uploaded="$(awk '/^[+*] / { count += 1 } END { print count + 0 }' "${combined_report}")"
    fi
    log "backup completed successfully: ${success_utc}; files uploaded=${files_uploaded}; file=${backup_file}"
    write_status_logged "ok" "${attempt_utc}" "${success_utc}" "" "${files_uploaded}" || true
    exit 0
  fi
  error="rclone copy failed for completed file; local data remains authoritative and daily backup will retry"
  log "backup failed: ${error}"
  write_status_logged "failed" "${attempt_utc}" "${previous_success}" "${error}" "0" || true
  exit 1
fi

mkdir -p "${snapshot_data_dir}"
log "copying local data into immutable snapshot: ${LOCAL_DATA_ROOT} -> ${snapshot_data_dir}"
if ! run_logged cp -a "${LOCAL_DATA_ROOT}/." "${snapshot_data_dir}/"; then
  error="local snapshot copy failed"
  log "backup failed: ${error}"
  write_status_logged "failed" "${attempt_utc}" "${previous_success}" "${error}" ""
  exit 1
fi

file_count="$(find "${snapshot_data_dir}" -type f | wc -l | tr -d ' ')"
log "local snapshot completed: ${snapshot_data_dir} (${file_count} files)"

log "upload started: ${snapshot_data_dir} -> ${remote_base}"
if run_rclone_copy "${snapshot_data_dir}" "${remote_base}" "${combined_report}"; then
  success_utc="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  if [[ -f "${combined_report}" ]]; then
    files_uploaded="$(awk '/^[+*] / { count += 1 } END { print count + 0 }' "${combined_report}")"
  else
    files_uploaded="${file_count}"
  fi
  log "backup completed successfully: ${success_utc}; files uploaded=${files_uploaded}; files in snapshot=${file_count}"
  write_status_logged "ok" "${attempt_utc}" "${success_utc}" "" "${files_uploaded}" || true
  exit 0
fi

error="rclone copy failed; local data remains authoritative and next timer run will retry"
log "backup failed: ${error}"
  write_status_logged "failed" "${attempt_utc}" "${previous_success}" "${error}" "${file_count}" || true
exit 1
