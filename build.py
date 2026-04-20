#!/usr/bin/env python3
"""TMDL Model Configurator — assembles a client model from prebuilt sections."""

import re
import shutil
import sys
from pathlib import Path
from typing import List, Set

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

    out = ROOT / "output" / client_slug
    if out.exists():
        shutil.rmtree(out)
    (out / "tables").mkdir(parents=True)

    # Seed from base/
    base = ROOT / "base"
    database_content = (base / "database.tmdl").read_text()
    model_content = (base / "model.tmdl").read_text()
    for tbl in (base / "tables").glob("*.tmdl"):
        shutil.copy2(tbl, out / "tables" / tbl.name)

    # Process each requested section
    rel_blocks: List[str] = []
    for section in sections:
        sec = ROOT / "sections" / section
        tables_dir = sec / "tables"
        if tables_dir.exists():
            for tbl in tables_dir.glob("*.tmdl"):
                shutil.copy2(tbl, out / "tables" / tbl.name)
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
                        shutil.copy2(tbl, out / "tables" / tbl.name)
                included_integrations.append(integ.name)

    # Validate all relationship endpoints resolve to assembled tables
    table_names: Set[str] = {p.stem for p in (out / "tables").glob("*.tmdl")}
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

    (out / "database.tmdl").write_text(database_content)
    (out / "model.tmdl").write_text(model_content)

    table_count = len(list((out / "tables").glob("*.tmdl")))
    rel_count = sum(block.count("relationship ") for block in rel_blocks)
    print(f"Built    : {model_name}  ({client_name})")
    print(f"Sections : {', '.join(sections) if sections else 'none'}")
    print(f"Integrations: {', '.join(included_integrations) if included_integrations else 'none'}")
    print(f"Tables   : {table_count}")
    print(f"Rels     : {rel_count}")
    print(f"Output   : {out}")


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
