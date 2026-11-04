"""Offline Windows device-identity PoC. Never authorizes activation or recovery.

Capture on both sides of a Windows reinstall with the same random study key.
Only keyed digests leave the collector; keep the key outside the reports.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path

FIELDS = ("smbios_uuid", "baseboard_serial", "bios_serial", "machine_guid", "disk0_serial")
HARDWARE = ("smbios_uuid", "baseboard_serial", "bios_serial")
PLACEHOLDERS = {"", "none", "unknown", "not applicable", "not available", "to be filled by o.e.m.",
                "to be filled by oem", "default string", "system serial number", "00000000-0000-0000-0000-000000000000"}

# Read-only CIM/registry calls. The disk signal is deliberately optional: SSD replacement is expected.
POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
$errors = @{}
function Read-Value([string]$name, [scriptblock]$query) {
 try { & $query } catch { $errors[$name] = $_.Exception.GetType().Name; $null }
}
$cs = Read-Value 'smbios_uuid' { Get-CimInstance Win32_ComputerSystemProduct }
$board = Read-Value 'baseboard_serial' { Get-CimInstance Win32_BaseBoard }
$bios = Read-Value 'bios_serial' { Get-CimInstance Win32_BIOS }
$disk = Read-Value 'disk0_serial' { Get-CimInstance Win32_DiskDrive | Where-Object Index -eq 0 | Select-Object -First 1 }
$guid = Read-Value 'machine_guid' { (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid }
$tpm = Read-Value 'tpm' { Get-Tpm }
@{
 smbios_uuid = $cs.UUID
 baseboard_serial = $board.SerialNumber
 bios_serial = $bios.SerialNumber
 machine_guid = $guid
 disk0_serial = $disk.SerialNumber
 tpm_present = [bool]($tpm -and $tpm.TpmPresent)
 tpm_ready = [bool]($tpm -and $tpm.TpmReady)
 probe_errors = $errors
} | ConvertTo-Json -Compress
"""


def normalize(value: object) -> str | None:
    if value is None or isinstance(value, (list, dict, bool)):
        return None
    clean = " ".join(str(value).strip().lower().split())
    if clean in PLACEHOLDERS or set(clean) <= {"0", "-"}:
        return None
    return clean


def collect_windows() -> dict:
    if os.name != "nt":
        raise RuntimeError("Device probe requires Windows")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def digest_report(signals: dict, study_key: bytes) -> dict:
    if len(study_key) < 32:
        raise ValueError("Study key must contain at least 32 random bytes")
    digests = {}
    for field in FIELDS:
        value = normalize(signals.get(field))
        digests[field] = hmac.new(study_key, (field + "\0" + value).encode(), hashlib.sha256).hexdigest() if value else None
    return {"schema": 1, "signals": digests, "tpm_present": bool(signals.get("tpm_present")),
            "tpm_ready": bool(signals.get("tpm_ready")), "probe_errors": signals.get("probe_errors") or {}}


def compare(before: dict, after: dict) -> dict:
    """Classify evidence for a human PoC review; never return an authorization verdict."""
    if before.get("schema") != 1 or after.get("schema") != 1:
        raise ValueError("Unsupported probe report")
    old, new = before["signals"], after["signals"]
    matched = [field for field in FIELDS if old.get(field) and old.get(field) == new.get(field)]
    changed = [field for field in FIELDS if old.get(field) and new.get(field) and old[field] != new[field]]
    missing = [field for field in FIELDS if not old.get(field) or not new.get(field)]
    hardware_matches = [field for field in matched if field in HARDWARE]
    hardware_changes = [field for field in changed if field in HARDWARE]
    if len(hardware_matches) >= 2 and not hardware_changes:
        category = "candidate_same_hardware"
    elif hardware_changes:
        category = "hardware_conflict"
    else:
        category = "insufficient_evidence"
    return {"category": category, "matched": matched, "changed": changed, "missing": missing,
            "tpm_before": before.get("tpm_present"), "tpm_after": after.get("tpm_present"),
            "recovery_authorized": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Sarae Windows hardware-fingerprint PoC")
    sub = parser.add_subparsers(dest="action", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--study-key-hex", required=True, help="Same 32+ random bytes for both captures")
    capture.add_argument("--output", required=True)
    check = sub.add_parser("compare")
    check.add_argument("before")
    check.add_argument("after")
    args = parser.parse_args()
    if args.action == "capture":
        report = digest_report(collect_windows(), bytes.fromhex(args.study_key_hex))
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Saved keyed probe report to {args.output}")
    else:
        result = compare(json.loads(Path(args.before).read_text(encoding="utf-8")),
                         json.loads(Path(args.after).read_text(encoding="utf-8")))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
