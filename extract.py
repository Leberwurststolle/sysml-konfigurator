"""Extract the design space of a SysML v2 model into a JSON file.

Usage:
    python extract.py <model folder or .sysml files...> [-o design_space.json]

Example:
    python extract.py "../SysML2 erste Versuche" -o design_space.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sysml_tool.extractor import ExtractionError, extract
from sysml_tool.toolkit import Toolkit, ToolkitError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("model", nargs="+", help="model folder(s) or .sysml/.kerml files")
    parser.add_argument("-o", "--output", default="design_space.json", help="output JSON file")
    parser.add_argument("--sysmlv2", help="path to the sysmlv2 executable")
    parser.add_argument("--lib", help="standard library folder or bundle file")
    args = parser.parse_args()

    try:
        toolkit = Toolkit(args.sysmlv2, args.lib)
        result = extract(args.model, toolkit)
    except (ToolkitError, ExtractionError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        for d in getattr(err, "diagnostics", []):
            print(f"  {d['file']}:{d['line']}:{d['col']}  {d['message']}", file=sys.stderr)
        return 1

    out = Path(args.output)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"{len(result['files'])} files, {result['elementCount']} elements, {result['extractionSeconds']} s")
    for pl in result["productLines"]:
        print(f"Product line {pl['qualifiedName']}: {len(pl['variationPoints'])} variation points, "
              f"{pl['combinations']} combinations, {len(pl['rules'])} rules, "
              f"{len(pl['requirements'])} requirements, {len(pl['configurations'])} configurations")
    if not result["productLines"]:
        print("No variation points found (no `variation` usages in the model).")
    for w in result["warnings"]:
        print(f"warning: {w}")
    print(f"Written to {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
