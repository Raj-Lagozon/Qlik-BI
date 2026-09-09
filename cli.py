"""QVF -> PBIX pipeline CLI.

Usage:
    python cli.py extract <path-to.qvf> [--name APP_NAME]
    python cli.py convert <app_name>
    python cli.py build <app_name>
    python cli.py run-all <path-to.qvf> [--name APP_NAME]
"""

from __future__ import annotations

import argparse
import io
import sys

from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to cp1252; the pbip-compiler dependency prints
# unicode arrows/emoji, so force UTF-8 stdout/stderr to avoid crashing on them.
# line_buffering=True is required here — a manually constructed TextIOWrapper
# defaults to full block buffering (not line buffering) even when writing to
# a live console, which is why every log line was only appearing all at once
# at the end instead of streaming as each step ran.
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Qlik .qvf -> Power BI .pbix pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="Import a .qvf into Qlik Cloud and pull its script/model/sheets")
    p_extract.add_argument("qvf_path")
    p_extract.add_argument("--name", default=None)
    p_extract.add_argument("--space-id", default=None, help="Qlik Cloud space id to import into (overrides QLIK_SPACE_ID)")
    p_extract.add_argument("--keep-app", action="store_true", help="Don't delete the imported app from Qlik Cloud afterward")

    p_convert = sub.add_parser("convert", help="Run the LLM skills over extracted data")
    p_convert.add_argument("app_name")

    p_build = sub.add_parser("build", help="Assemble + compile the .pbip project into a .pbix")
    p_build.add_argument("app_name")

    p_all = sub.add_parser("run-all", help="extract -> convert -> build, from a raw .qvf to a finished .pbix")
    p_all.add_argument("qvf_path")
    p_all.add_argument("--name", default=None)
    p_all.add_argument("--space-id", default=None, help="Qlik Cloud space id to import into (overrides QLIK_SPACE_ID)")
    p_all.add_argument("--keep-app", action="store_true", help="Don't delete the imported app from Qlik Cloud afterward")

    args = parser.parse_args()

    if args.command == "extract":
        from qlik_extract import extract_app
        extract_app(args.qvf_path, args.name, args.space_id, args.keep_app)

    elif args.command == "convert":
        from llm_convert import convert_app
        written = convert_app(args.app_name)
        print(f"[convert] wrote {sum(len(v) if isinstance(v, list) else 1 for v in written.values())} artifact(s)")

    elif args.command == "build":
        from pbip_build import build_project
        pbix_path = build_project(args.app_name)
        print(f"[build] .pbix ready: {pbix_path}")

    elif args.command == "run-all":
        from qlik_extract import extract_app
        from llm_convert import convert_app
        from pbip_build import build_project

        app_name = extract_app(args.qvf_path, args.name, args.space_id, args.keep_app)
        convert_app(app_name)
        pbix_path = build_project(app_name)
        print(f"\n[run-all] done -> {pbix_path}")
        print("[run-all] open it in Power BI Desktop and click Refresh to load real data.")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
