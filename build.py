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

    # TMDL lives inside the SemanticModel artifact's definition/ subfolder
    tmdl_dir = out_root / f"{safe_name}.SemanticModel" / "definition"
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
    if "compatibility_level" in overrides:
        database_content = _set_property(
            database_content, "compatibilityLevel", str(overrides["compatibility_level"])
        )
    if "culture" in overrides:
        model_content = _set_property(model_content, "culture", overrides["culture"])
        model_content = _set_property(model_content, "sourceQueryCulture", overrides["culture"])

    # Write relationships to their own file (not embedded in model.tmdl)
    if rel_blocks:
        (tmdl_dir / "relationships.tmdl").write_text("\n\n".join(rel_blocks) + "\n")

    # Extract measures from every assembled table into a single _Measures table.
    # Measure-only tables (no columns) are deleted after extraction.
    all_measures: List[str] = []
    for tbl_file in sorted((tmdl_dir / "tables").glob("*.tmdl")):
        content = tbl_file.read_text()
        stripped, measures = _extract_and_strip_measures(content)
        all_measures.extend(measures)
        if measures:
            if _has_columns(stripped):
                tbl_file.write_text(stripped)
            else:
                tbl_file.unlink()

    (tmdl_dir / "tables" / "_Measures.tmdl").write_text(
        "table _Measures\n\n"
        + ("\n\n".join(all_measures) + "\n\n" if all_measures else "")
        + "\tpartition '_Measures-partition' = m\n"
        + "\t\tmode: import\n"
        + "\t\tsource =\n"
        + "\t\t\tlet\n"
        + "\t\t\t\tMeasures = #table(type table [], {})\n"
        + "\t\t\tin\n"
        + "\t\t\t\tMeasures\n"
    )

    # Build top-level annotations and ref table declarations (outside model block, no indent)
    table_entries = []
    for tbl_file in sorted((tmdl_dir / "tables").glob("*.tmdl")):
        name = _get_table_name(tbl_file.read_text())
        table_entries.append(name)

    query_order = json.dumps(table_entries)
    ref_lines = "\n".join(
        f"ref table '{n}'" if (" " in n or "&" in n) else f"ref table {n}"
        for n in table_entries
    )
    model_content = (
        model_content.rstrip("\n")
        + "\n\nannotation __PBI_TimeIntelligenceEnabled = 0"
        + f"\n\nannotation PBI_QueryOrder = {query_order}"
        + f"\n\n{ref_lines}\n"
    )

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
        "settings": {"enableAutoRecovery": True}
    }, indent=2) + "\n")

    sm_dir = out_root / f"{safe_name}.SemanticModel"
    (sm_dir / "definition.pbism").write_text(json.dumps({
        "version": "4.1",
        "settings": {}
    }, indent=2) + "\n")
    (sm_dir / ".platform").write_text(json.dumps({
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
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{safe_name}.SemanticModel"}}
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


def _extract_and_strip_measures(content: str) -> tuple:
    """Return (stripped_content, measure_blocks).

    Walks the table TMDL line by line, pulling out each measure block
    (the measure declaration plus its double-indented properties).
    Trailing blank lines inside a block are trimmed before saving.
    Consecutive blank lines left behind in the stripped content are collapsed.
    """
    lines = content.splitlines()
    result: List[str] = []
    measures: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\tmeasure\b", line):
            block = [line]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if re.match(r"^\t\t", nxt) or nxt.strip() == "":
                    block.append(nxt)
                    i += 1
                else:
                    break
            while block and block[-1].strip() == "":
                block.pop()
            measures.append("\n".join(block))
        else:
            result.append(line)
            i += 1

    cleaned: List[str] = []
    prev_blank = False
    for line in result:
        blank = line.strip() == ""
        if blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = blank

    return "\n".join(cleaned).rstrip("\n") + "\n", measures


def _has_columns(content: str) -> bool:
    return bool(re.search(r"^\tcolumn\b", content, re.MULTILINE))


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
