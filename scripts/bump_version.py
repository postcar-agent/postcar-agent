#!/usr/bin/env python3
"""
Bump postcar-agent version according to version_rules.json.

Rules:
  z increments on each commit.
  When z > max_patch: z resets to 0, y increments.
  When y > max_minor: y resets to 0, x increments.

Updates: VERSION file and VERSION = "..." line in postcar_check.py.
"""
import json
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent

rules_path = root / "version_rules.json"
version_path = root / "VERSION"
kit_path = root / "postcar_check.py"

rules = json.loads(rules_path.read_text())
max_minor = int(rules["max_minor"])
max_patch = int(rules["max_patch"])

current = version_path.read_text().strip()
parts = current.split(".")
if len(parts) != 3:
    print(f"ERROR: invalid version '{current}' in VERSION file", file=sys.stderr)
    sys.exit(1)

x, y, z = int(parts[0]), int(parts[1]), int(parts[2])

z += 1
if z > max_patch:
    z = 0
    y += 1
if y > max_minor:
    y = 0
    x += 1

new_version = f"{x}.{y}.{z}"
print(f"version: {current} → {new_version}")

version_path.write_text(new_version + "\n")

kit_content = kit_path.read_text()
kit_content = re.sub(
    r'^VERSION = "[^"]*"',
    f'VERSION = "{new_version}"',
    kit_content,
    flags=re.MULTILINE,
)
kit_path.write_text(kit_content)
