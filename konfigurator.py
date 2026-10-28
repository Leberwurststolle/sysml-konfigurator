"""Interactive configurator for SysML v2 product-line models.

Extracts the design space of the model, then serves a local web page where
you browse the model, assemble a configuration and see immediately whether it
is admissible: every rule and requirement is decided by the sysml-toolkit.

Usage:
    python konfigurator.py <model folder or .sysml files...> [--port 8765] [--no-browser]

Example:
    python konfigurator.py "../SysML2 erste Versuche"
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from sysml_tool.extractor import ExtractionError, collect_model_files, extract
from sysml_tool.server import AppState, serve
from sysml_tool.toolkit import Toolkit, ToolkitError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("model", nargs="+", help="model folder(s) or .sysml/.kerml files")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--save-json", help="also write the extracted design space to this file")
    parser.add_argument("--sysmlv2", help="path to the sysmlv2 executable")
    parser.add_argument("--lib", help="standard library folder or bundle file")
    args = parser.parse_args()

    try:
        toolkit = Toolkit(args.sysmlv2, args.lib)
        files = collect_model_files(args.model)

        def run_extraction() -> dict:
            ds = extract(files, toolkit)
            if args.save_json:
                Path(args.save_json).write_text(json.dumps(ds, indent=2, ensure_ascii=False), encoding="utf-8")
            return ds

        print(f"Reading {len(files)} model files with {toolkit.version()} ...")
        state = AppState(run_extraction, toolkit)
    except (ToolkitError, ExtractionError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        for d in getattr(err, "diagnostics", []):
            print(f"  {d['file']}:{d['line']}:{d['col']}  {d['message']}", file=sys.stderr)
        return 1

    for pl in state.design_space["productLines"]:
        print(f"  {pl['qualifiedName']}: {len(pl['variationPoints'])} variation points, {pl['combinations']} combinations")

    try:
        server = serve(state, args.host, args.port)
    except OSError as err:
        print(f"ERROR: cannot listen on {args.host}:{args.port} ({err}). Try --port <other>.", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{args.port}/"
    print(f"Konfigurator running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
