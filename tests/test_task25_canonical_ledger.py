import re
import unittest
from pathlib import Path


EXPECTED_SECTIONS = [
    ("A", "Basic Provider Management", 8),
    ("B", "Authentication and Connection", 8),
    ("C", "Multimodal Provider Support", 8),
    ("D", "Routing and Selection", 8),
    ("E", "Retry, Failure and Recovery", 8),
    ("F", "Health and Reliability", 8),
    ("G", "Output and Asset Safety", 8),
    ("H", "Cost, Free Credits and Quotas", 8),
    ("I", "Security, Privacy and Governance", 8),
    ("J", "Production Orchestration", 8),
    ("K", "Quality, Performance and Observability", 8),
    ("L", "Configuration, Compatibility and Change Control", 8),
    ("M", "Testing, Audit and Release Readiness", 14),
    ("N", "High-Advanced Routing Intelligence", 8),
    ("O", "Enterprise and Large-Production Resiliency", 10),
    ("P", "Future-Ready Extensibility", 10),
]


class Task25CanonicalLedgerTests(unittest.TestCase):
    def test_ledger_preserves_exact_sections_and_138_unique_feature_ids(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "projects" / "provider_store" / "TASK25_CANONICAL_138_ITEM_LEDGER.md"
        )
        content = path.read_text(encoding="utf-8")
        headings = re.findall(r"^## ([A-P]) — (.+)$", content, flags=re.MULTILINE)
        self.assertEqual(headings, [(letter, title) for letter, title, _ in EXPECTED_SECTIONS])

        section_counts = {}
        current_section = None
        items = []
        row = re.compile(
            r"^\|\s*(\d+)\s*\|\s*([🟢🟡🔴])\s*\|\s*(.+?)\s*\|\s*"
            r"(VERIFIED|PARTIAL|OPEN)\s*\|\s*(.+?)\s*\|$"
        )
        for line in content.splitlines():
            match = re.match(r"^## ([A-P]) — ", line)
            if match:
                current_section = match.group(1)
                section_counts[current_section] = 0
                continue
            match = row.match(line)
            if match:
                number = int(match.group(1))
                baseline, name, audit_status, evidence = match.group(2), match.group(3).strip(), match.group(4), match.group(5).strip()
                self.assertTrue(name, f"feature {number} has no name")
                self.assertTrue(evidence, f"feature {number} has no evidence/gap note")
                self.assertIn(audit_status, {"VERIFIED", "PARTIAL", "OPEN"})
                self.assertIn(baseline, {"🟢", "🟡", "🔴"})
                items.append((number, current_section))
                if current_section is not None:
                    section_counts[current_section] += 1

        self.assertEqual(len(items), 138)
        self.assertEqual(sorted(number for number, _ in items), list(range(1, 139)))
        self.assertEqual(len({number for number, _ in items}), 138)
        self.assertEqual(
            section_counts,
            {letter: count for letter, _, count in EXPECTED_SECTIONS},
        )
        expected_section_for_number = {}
        next_number = 1
        for letter, _, count in EXPECTED_SECTIONS:
            for number in range(next_number, next_number + count):
                expected_section_for_number[number] = letter
            next_number += count
        self.assertEqual(
            {number: section for number, section in items},
            expected_section_for_number,
        )


if __name__ == "__main__":
    unittest.main()
