"""
Run all golden corpus generators in the correct order.
Usage: uv run python tests/fixtures/golden/generators/run_all.py
"""

import importlib
import sys
from pathlib import Path

# Add generators directory to path so each module can be imported
GENERATORS_DIR = Path(__file__).parent
sys.path.insert(0, str(GENERATORS_DIR))

MODULES = [
    "gen_clean_pdf",
    "gen_scanned_pdf",
    "gen_bloated_manual",
    "gen_boilerplate_docs",
    "gen_nested_tables",
    "gen_spreadsheets",
    "gen_confluence_html",
    "gen_near_duplicates",
    "gen_unservable",
    "gen_adversarial_pdf",
    "gen_form_pdf",
    "gen_malformed_pdf",
]


def main() -> None:
    print("=" * 60)
    print("Golden corpus generator — running all generators")
    print("=" * 60)
    for mod_name in MODULES:
        print(f"\n--- {mod_name} ---")
        mod = importlib.import_module(mod_name)
        mod.build()
    print("\n" + "=" * 60)
    print("All generators completed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
