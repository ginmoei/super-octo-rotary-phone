#!/usr/bin/env python3
"""TMDL Model Configurator — assembles a client model from prebuilt sections."""

import json
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Set

import yaml

ROOT = Path(__file__).parent


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python build.py <path/to/client.yaml>")
    build(sys.argv[1])


def build(config_path: str) -> None:
    config_file = Path(config_path)
    if not config_file.exists():
        sys.exit(f"Config not found: {config_path}")

    with open(config_file) as fh:
        config = yaml.safe_load(fh)

    client_slug = config_file.stem
    client_name = config.get("client_name", client_slug)
    model_name = config.get("model_name", client_name)
    sections: List[str] = config.get("sections") or []
    overrides: dict = config.get("overrides") or {}

    _validate_section_dirs(sections)
    _validate_section_isolation()

    safe_name = re.sub(r'[<>:"/\\|?*]', "_", model_name).strip()
    out_root = ROOT / "output" / client_slug
    if out_root.exists():
        shutil.rmtree(out_root)

    # TMDL lives inside the Dataset artifact's definition/ subfolder
    tmdl_dir = out_root / f"{safe_name}.Dataset" / "definition"
    (tmdl_dir / "tables").mkdir(parents=True)

    # Seed from base/
    base = ROOT / "base"
    database_content = (base / "database.tmdl").read_text()
    model_content = (base / "model.tmdl").read_text()
    for tbl in (base / "tables").glob("*.tmdl"):
        shutil.copy2(tbl, tmdl_dir / "tables" / tbl.name)

    # Process each requested section
    rel_blocks: List[str] = []
    for section in sections:
        sec = ROOT / "sections" / section
        tables_dir = sec / "tables"
        if tables_dir.exists():
            for tbl in tables_dir.glob("*.tmdl"):
                shutil.copy2(tbl, tmdl_dir / "tables" / tbl.name)
        rel_file = sec / "relationships.tmdl"
        if rel_file.exists():
            rel_blocks.append(rel_file.read_text().strip())

    # Auto-include integrations whose required sections are all present
    selected_set = set(sections)
    integrations_dir = ROOT / "integrations"
    included_integrations: List[str] = []
    if integrations_dir.exists():
        for integ in sorted(integrations_dir.iterdir()):
            if not integ.is_dir():
                continue
            manifest_file = integ / "manifest.yaml"
            if not manifest_file.exists():
                continue
            with open(manifest_file) as fh:
                manifest = yaml.safe_load(fh)
            required = set(manifest.get("requires") or [])
            if required and required.issubset(selected_set):
                tables_dir = integ / "tables"
                if tables_dir.exists():
                    for tbl in tables_dir.glob("*.tmdl"):
                        shutil.copy2(tbl, tmdl_dir / "tables" / tbl.name)
                included_integrations.append(integ.name)

    # Validate all relationship endpoints resolve to assembled tables
    table_names: Set[str] = {p.stem for p in (tmdl_dir / "tables").glob("*.tmdl")}
    _validate_relationships(rel_blocks, table_names)

    # Apply overrides
    database_content = _rename_database(database_content, model_name)
    if "compatibility_level" in overrides:
        database_content = _set_property(
            database_content, "compatibilityLevel", str(overrides["compatibility_level"])
        )
    if "culture" in overrides:
        model_content = _set_property(model_content, "culture", overrides["culture"])

    # Inject relationships into model.tmdl (indented inside model block)
    if rel_blocks:
        injected = "\n\n".join(
            "\n".join("\t" + line for line in block.splitlines())
            for block in rel_blocks
        )
        model_content = model_content.rstrip("\n") + "\n\n" + injected + "\n"

    (tmdl_dir / "database.tmdl").write_text(database_content)
    (tmdl_dir / "model.tmdl").write_text(model_content)

    _write_pbip(out_root, model_name, safe_name)

    table_count = len(list((tmdl_dir / "tables").glob("*.tmdl")))
    rel_count = sum(block.count("relationship ") for block in rel_blocks)
    print(f"Built        : {model_name}  ({client_name})")
    print(f"Sections     : {', '.join(sections) if sections else 'none'}")
    print(f"Integrations : {', '.join(included_integrations) if included_integrations else 'none'}")
    print(f"Tables       : {table_count}")
    print(f"Rels         : {rel_count}")
    print(f"Output       : {out_root / (safe_name + '.pbip')}")


# ---------------------------------------------------------------------------
# PBIP scaffold
# ---------------------------------------------------------------------------

def _write_pbip(out_root: Path, model_name: str, safe_name: str) -> None:
    """Write the .pbip, .platform, and .pbir files around the assembled TMDL."""
    (out_root / f"{safe_name}.pbip").write_text(json.dumps({
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{safe_name}.Report"}}],
        "settings": {"enableTmdlSchemaVersion": 1}
    }, indent=2) + "\n")

    dataset_dir = out_root / f"{safe_name}.Dataset"
    (dataset_dir / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": model_name},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())}
    }, indent=2) + "\n")

    report_dir = out_root / f"{safe_name}.Report"
    report_dir.mkdir(exist_ok=True)
    (report_dir / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": model_name},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())}
    }, indent=2) + "\n")
    (report_dir / "definition.pbir").write_text(json.dumps({
        "version": "1.0",
        "datasetReference": {"byPath": {"path": f"../{safe_name}.Dataset"}}
    }, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Section isolation validation
# ---------------------------------------------------------------------------

def _validate_section_isolation() -> None:
    """Ensure no section table file references another section's tables in DAX."""
    sections_dir = ROOT / "sections"
    if not sections_dir.exists():
        return

    # Build a map of table stem → owning section for all sections
    table_to_section: Dict[str, str] = {}
    for sec_dir in sections_dir.iterdir():
        if not sec_dir.is_dir():
            continue
        tables_dir = sec_dir / "tables"
        if tables_dir.exists():
            for tbl_file in tables_dir.glob("*.tmdl"):
                table_to_section[tbl_file.stem] = sec_dir.name

    base_tables: Set[str] = {p.stem for p in (ROOT / "base" / "tables").glob("*.tmdl")}

    errors: List[str] = []
    for sec_dir in sorted(sections_dir.iterdir()):
        if not sec_dir.is_dir():
            continue
        tables_dir = sec_dir / "tables"
        if not tables_dir.exists():
            continue
        for tbl_file in sorted(tables_dir.glob("*.tmdl")):
            content = tbl_file.read_text()
            own_table = _get_table_name(content)
            for ref in _find_dax_table_refs(content):
                if ref in base_tables or ref == own_table or ref == tbl_file.stem:
                    continue
                owner = table_to_section.get(ref)
                if owner and owner != sec_dir.name:
                    errors.append(
                        f"sections/{sec_dir.name}/tables/{tbl_file.name}: "
                        f"'{own_table}' references '{ref}' from section '{owner}' — "
                        f"move this cross-section measure to integrations/"
                    )

    if errors:
        print("Section isolation violations:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)


def _get_table_name(content: str) -> str:
    m = re.match(r"^table\s+(?:'([^']+)'|(\S+))", content)
    if not m:
        return ""
    return m.group(1) or m.group(2)


def _find_dax_table_refs(content: str) -> Set[str]:
    """Extract DAX TableName[ references, skipping partition source blocks.

    Partition blocks (M or DAX source expressions) can contain bracket syntax
    that isn't a table reference, so we skip them entirely.
    """
    refs: Set[str] = set()
    in_partition = False
    for line in content.splitlines():
        if re.match(r"^\tpartition\b", line):
            in_partition = True
            continue
        # A single-tab non-partition line signals we've left the partition block
        if in_partition and re.match(r"^\t[^\t\n]", line):
            in_partition = False
        if in_partition:
            continue
        for m in re.finditer(r"'([^']+)'\s*\[|\b([A-Za-z_][A-Za-z\d_ ]*)\s*\[", line):
            refs.add(m.group(1) or m.group(2))
    return refs


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _validate_section_dirs(sections: List[str]) -> None:
    for section in sections:
        d = ROOT / "sections" / section
        if not d.is_dir():
            sys.exit(f"Unknown section '{section}' — no directory: {d}")


def _set_property(content: str, key: str, value: str) -> str:
    """Replace an existing property value, or insert it after the block header."""
    new, n = re.subn(
        rf"^(\t{key}:)\s*.+$", rf"\g<1> {value}", content, flags=re.MULTILINE
    )
    if n == 0:
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if re.match(r"^(database|model)\b", line):
                lines.insert(i + 1, f"\t{key}: {value}")
                return "\n".join(lines) + "\n"
    return new


def _rename_database(content: str, name: str) -> str:
    return re.sub(r"^(database\s+)'[^']+'", rf"\1'{name}'", content, flags=re.MULTILINE)


def _validate_relationships(blocks: List[str], table_names: Set[str]) -> None:
    errors: List[str] = []
    for block in blocks:
        for line in block.splitlines():
            s = line.strip()
            for directive in ("fromColumn:", "toColumn:"):
                if s.startswith(directive):
                    ref = s[len(directive):].strip()
                    m = re.match(r"'?([^'.]+)'?\.", ref)
                    if m and m.group(1) not in table_names:
                        errors.append(
                            f"{directive} references unknown table '{m.group(1)}'"
                        )
    if errors:
        print("Validation errors:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
