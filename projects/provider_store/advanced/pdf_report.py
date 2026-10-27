"""Dependency-free PDF rendering for the Advanced Provider Store release report."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _pdf_escape(value: str) -> str:
    safe = value.encode("cp1252", errors="replace").decode("cp1252")
    safe = safe.encode("ascii", errors="replace").decode("ascii")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _make_pdf(pages: list[list[str]]) -> bytes:
    objects: list[bytes] = []
    page_ids = [4 + i * 2 for i in range(len(pages))]
    content_ids = [5 + i * 2 for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_id, content_id, lines in zip(page_ids, content_ids, pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode("ascii")
        )
        commands = ["BT", "/F1 10 Tf", "50 750 Td", "14 TL"]
        for index, line in enumerate(lines):
            if index:
                commands.append("T*")
            commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("ET")
        stream = ("\n".join(commands) + "\n").encode("ascii")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream"
        )
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def write_pdf_report(output_path: str | Path, report: dict[str, Any]) -> Path:
    """Write a readable multi-page PDF summary without third-party dependencies."""
    lines = [
        "ADVANCED PROVIDER STORE - RELEASE REPORT",
        "",
        f"Completion status: {report.get('completion_status', 'UNKNOWN')}",
        f"Completed points: {report.get('points_completed', 0)} / {report.get('points_total', 150)}",
        f"Remaining points: {report.get('points_remaining', 150)}",
        f"Release ready: {report.get('release_ready', False)}",
        f"Live provider verification: {report.get('live_provider_verification', 'NOT CLAIMED')}",
        "",
        "STATUS COUNTS",
    ]
    counts = report.get("status_counts", {})
    if isinstance(counts, dict):
        for status, count in sorted(counts.items()):
            lines.append(f"{status}: {count}")
    lines.extend(["", "FEATURE LEDGER"])
    ledger = report.get("ledger", [])
    if isinstance(ledger, list):
        for row in ledger:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"{row.get('id', '?'):>3} | {row.get('status', 'UNVERIFIED')} | "
                f"{row.get('feature', 'Unnamed feature')}"
            )
            evidence = str(row.get("evidence", "")).strip()
            remaining = str(row.get("remaining", "")).strip()
            if evidence:
                lines.append(f"    Evidence: {evidence}")
            if remaining:
                lines.append(f"    Remaining: {remaining}")
    lines.extend([
        "",
        "SAFETY / LIMITATIONS",
        "This report reflects only evidence recorded in the feature ledger.",
        "Catalog presence does not prove live provider connectivity or available credits.",
        "No billable generation or subscription purchase is implied by this report.",
    ])

    wrapped: list[str] = []
    for line in lines:
        if len(line) <= 88:
            wrapped.append(line)
            continue
        words = line.split()
        current = ""
        for word in words:
            if current and len(current) + len(word) + 1 > 88:
                wrapped.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            wrapped.append(current)
    pages = [wrapped[i:i + 48] for i in range(0, len(wrapped), 48)] or [["No report data"]]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_make_pdf(pages))
    return path
