"""Generate evidence-based Advanced Provider Store release reports and a source-only ZIP.

The package is deliberately marked incomplete unless all 150 ledger points are COMPLETED.
It never includes runtime state, environment files, backups, caches, or test fixtures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_NAME = "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md"
ALLOWED_SUFFIXES = {".py", ".json", ".md", ".toml"}
DENIED_PARTS = {
    ".git", "__pycache__", ".venv", "venv", "backup", "credential",
    "secret", "token", "state", ".env", "runtime", "cache",
}
STATUS_VALUES = {"COMPLETED", "PARTIAL", "NOT STARTED", "UNVERIFIED", "BLOCKED"}
LEDGER_ROW = re.compile(r"^\|\s*(\d{3})\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def read_ledger(ledger_path: Path) -> list[dict[str, Any]]:
    """Parse the canonical ledger and reject missing, duplicate, or malformed point IDs."""
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("completion ledger cannot be read") from exc
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = LEDGER_ROW.match(line)
        if not match:
            continue
        number, feature, status, evidence, remaining = (part.strip() for part in match.groups())
        if status not in STATUS_VALUES:
            raise ValueError(f"ledger point {number} has an invalid status")
        rows.append({
            "id": int(number),
            "feature": feature,
            "status": status,
            "evidence": evidence,
            "remaining": remaining,
        })
    ids = [row["id"] for row in rows]
    if len(rows) != 150 or sorted(ids) != list(range(1, 151)):
        raise ValueError("completion ledger must contain each point ID from 001 through 150 exactly once")
    return rows


def build_release_report(ledger_path: Path) -> dict[str, Any]:
    rows = read_ledger(ledger_path)
    counts = Counter(row["status"] for row in rows)
    complete = counts["COMPLETED"] == 150
    return {
        "schema_version": "1.0",
        "component": "Advanced Provider Store",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "completion_status": "COMPLETE" if complete else "INCOMPLETE",
        "points_total": 150,
        "points_completed": counts["COMPLETED"],
        "points_remaining": 150 - counts["COMPLETED"],
        "status_counts": {status: counts.get(status, 0) for status in sorted(STATUS_VALUES)},
        "live_provider_verification": "NOT_CLAIMED",
        "release_ready": complete,
        "ledger": rows,
    }


def _safe_source_files(repo_root: Path) -> list[Path]:
    source_root = repo_root / "projects" / "provider_store"
    files: list[Path] = []
    for path in source_root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        relative_parts = [part.casefold() for part in path.relative_to(repo_root).parts]
        name = path.name.casefold()
        if any(part in DENIED_PARTS for part in relative_parts):
            continue
        if any(part in name for part in (
            "backup", ".env", "secrets.json", "credentials.json", "token_store",
            "runtime_state", "provider_state.json",
        )) or name.endswith("_state.json") or name == "state.json":
            continue
        files.append(path)
    build_metadata = repo_root / "pyproject.toml"
    if build_metadata.is_file() and not build_metadata.is_symlink():
        files.append(build_metadata)
    return sorted(files, key=lambda path: path.relative_to(repo_root).as_posix())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_release_artifacts(output_dir: Path, *, repo_root: Path | None = None) -> dict[str, Any]:
    """Write JSON/Markdown evidence, source manifest, and a source-only ZIP."""
    root = (repo_root or _repo_root()).resolve()
    ledger_path = root / "projects" / "provider_store" / "advanced" / LEDGER_NAME
    report = build_release_report(ledger_path)
    files = _safe_source_files(root)
    if not files:
        raise ValueError("no eligible provider-store source files found")
    manifest_entries: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        # Fail closed on private-key material; don't copy it into a release archive.
        private_key_markers = (
            b"-----BEGIN " + b"PRIVATE KEY-----",
            b"-----BEGIN " + b"RSA PRIVATE KEY-----",
        )
        if any(marker in data for marker in private_key_markers):
            raise ValueError(f"private-key material detected in eligible source file: {relative}")
        manifest_entries.append({"path": relative, "size_bytes": len(data), "sha256": _sha256(data)})
        payloads.append((relative, data))

    output_dir.mkdir(parents=True, exist_ok=True)
    report_json = output_dir / "advanced_provider_store_report.json"
    report_md = output_dir / "advanced_provider_store_report.md"
    manifest_json = output_dir / "advanced_provider_store_manifest.json"
    archive = output_dir / "advanced_provider_store_source.zip"
    archive_checksum = output_dir / "advanced_provider_store_source.zip.sha256"
    pdf_report = output_dir / "advanced_provider_store_report.pdf"
    sbom_json = output_dir / "advanced_provider_store_sbom.spdx.json"
    report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        from .pdf_report import write_pdf_report
    except ImportError:  # Supports direct script execution as well as module execution.
        from pdf_report import write_pdf_report
    write_pdf_report(pdf_report, report)
    lines = [
        "# Advanced Provider Store Release Report",
        "",
        f"- Status: **{report['completion_status']}**",
        f"- Completed points: {report['points_completed']} / {report['points_total']}",
        f"- Remaining points: {report['points_remaining']}",
        "- Live provider verification: NOT CLAIMED",
        "",
        "## Status counts",
        "",
    ]
    for status, count in report["status_counts"].items():
        lines.append(f"- {status}: {count}")
    lines.extend(["", "## Release gate", "", "This report is not a production-readiness claim. Live provider verification requires configured credentials and explicit approval for any billable action.", ""])
    report_md.write_text("\n".join(lines), encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "component": "Advanced Provider Store",
        "archive": archive.name,
        "file_count": len(manifest_entries),
        "files": manifest_entries,
        "excluded": ["runtime state", "environment files", "backups", "credentials/secrets", "caches", "test fixtures"],
    }
    manifest_json.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "AI_CREATOR_AGENT Advanced Provider Store source bundle",
        "documentNamespace": "https://spdx.org/spdxdocs/ai-creator-agent-provider-store-" + _sha256(manifest_json.read_bytes()),
        "creationInfo": {
            "creators": ["Tool: AI_CREATOR_AGENT-release_report"],
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "comment": "Generated from source-file hashes; license conclusions are intentionally not inferred.",
        },
        "packages": [{
            "name": "AI_CREATOR_AGENT-Advanced-Provider-Store",
            "SPDXID": "SPDXRef-Package-AdvancedProviderStore",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": True,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
        }],
        "files": [{
            "fileName": entry["path"],
            "SPDXID": "SPDXRef-File-" + str(index + 1),
            "checksums": [{"algorithm": "SHA256", "checksumValue": entry["sha256"]}],
            "licenseConcluded": "NOASSERTION",
            "copyrightText": "NOASSERTION",
        } for index, entry in enumerate(manifest_entries)],
        "relationships": [{
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": "SPDXRef-Package-AdvancedProviderStore",
        }],
    }
    sbom_json.write_text(json.dumps(sbom, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for relative, data in payloads:
            zf.writestr(relative, data)
        zf.writestr("RELEASE_REPORT.json", report_json.read_bytes())
        zf.writestr("SOURCE_MANIFEST.json", manifest_json.read_bytes())
        zf.writestr("SBOM.spdx.json", sbom_json.read_bytes())
        zf.write(pdf_report, "ADVANCED_PROVIDER_STORE_REPORT.pdf")
    with zipfile.ZipFile(archive, "r") as zf:
        bad_member = zf.testzip()
        if bad_member is not None:
            raise ValueError(f"ZIP integrity check failed at {bad_member}")
        archived = set(zf.namelist())
        expected = {name for name, _ in payloads} | {"RELEASE_REPORT.json", "SOURCE_MANIFEST.json", "SBOM.spdx.json", "ADVANCED_PROVIDER_STORE_REPORT.pdf"}
        if archived != expected:
            raise ValueError("ZIP file list does not match the verified manifest")
        for entry in manifest_entries:
            archived_data = zf.read(entry["path"])
            if _sha256(archived_data) != entry["sha256"]:
                raise ValueError(f"ZIP checksum mismatch for {entry['path']}")
    archive_digest = _sha256(archive.read_bytes())
    archive_checksum.write_text(f"{archive_digest}  {archive.name}\n", encoding="ascii")
    if archive_checksum.read_text(encoding="ascii").split()[0] != _sha256(archive.read_bytes()):
        raise ValueError("release ZIP SHA-256 sidecar verification failed")
    return {
        "completion_status": report["completion_status"],
        "points_completed": report["points_completed"],
        "points_total": report["points_total"],
        "files_packaged": len(manifest_entries),
        "archive": str(archive),
        "report_json": str(report_json),
        "report_markdown": str(report_md),
        "report_pdf": str(pdf_report),
        "manifest": str(manifest_json),
        "sbom": str(sbom_json),
        "archive_sha256": archive_digest,
        "archive_checksum": str(archive_checksum),
        "zip_integrity": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/advanced-provider-store-release"))
    args = parser.parse_args()
    try:
        result = write_release_artifacts(args.output_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    # An incomplete ledger is a valid report, not a successful production release.
    return 0 if result["zip_integrity"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
