"""Offline PoC scenarios; these are not proof of anti-spoofing strength."""
import unittest

from sarae_agent.activation.device_probe import compare, digest_report, normalize

KEY = bytes(range(32))
BASE = {"smbios_uuid": "11111111-2222-3333-4444-555555555555",
        "baseboard_serial": "BOARD-123", "bios_serial": "BIOS-123",
        "machine_guid": "win-install-1", "disk0_serial": "SSD-1",
        "tpm_present": True, "tpm_ready": True}


def result(**changes):
    return compare(digest_report(BASE, KEY), digest_report({**BASE, **changes}, KEY))


class ProbeScenarios(unittest.TestCase):
    def test_reinstall_windows_changes_guid_only(self):
        report = result(machine_guid="win-install-2")
        self.assertEqual(report["category"], "candidate_same_hardware")
        self.assertFalse(report["recovery_authorized"])

    def test_replace_ssd_and_ram_has_same_board_evidence(self):
        report = result(disk0_serial="SSD-2", machine_guid="win-install-2")
        self.assertEqual(report["category"], "candidate_same_hardware")

    def test_different_pc_conflicts(self):
        report = result(smbios_uuid="other-uuid", baseboard_serial="BOARD-OTHER",
                        bios_serial="BIOS-OTHER", machine_guid="other-guid")
        self.assertEqual(report["category"], "hardware_conflict")

    def test_duplicate_oem_values_are_insufficient(self):
        old = digest_report({**BASE, "smbios_uuid": "To be filled by O.E.M.",
                             "baseboard_serial": "Default String", "bios_serial": "System Serial Number"}, KEY)
        new = digest_report({**BASE, "smbios_uuid": "To be filled by O.E.M.",
                             "baseboard_serial": "Default String", "bios_serial": "System Serial Number",
                             "machine_guid": "another-install"}, KEY)
        self.assertEqual(compare(old, new)["category"], "insufficient_evidence")

    def test_board_replacement_requires_review(self):
        self.assertEqual(result(baseboard_serial="NEW-BOARD")["category"], "hardware_conflict")

    def test_no_tpm_is_only_a_probe_attribute(self):
        report = result(tpm_present=False, tpm_ready=False)
        self.assertEqual(report["category"], "candidate_same_hardware")
        self.assertFalse(report["recovery_authorized"])

    def test_report_is_keyed_and_no_raw_hardware_leaks(self):
        report = digest_report(BASE, KEY)
        self.assertNotIn("BOARD-123", str(report))
        self.assertNotEqual(digest_report(BASE, b"x" * 32)["signals"], report["signals"])
        self.assertIsNone(normalize("  To be filled by O.E.M. "))
        with self.assertRaises(ValueError):
            digest_report(BASE, b"short")


if __name__ == "__main__":
    unittest.main()
