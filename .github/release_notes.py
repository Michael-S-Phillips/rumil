"""Print the CHANGELOG.md section for one version (used by the release workflow)."""

import re
import sys
from pathlib import Path

version = sys.argv[1]
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
m = re.search(rf"^## \[?{re.escape(version)}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
body = m.group(1).strip() if m else ""
print(body or f"Rumil {version}.")
print("\nInstall: download rumil-v" + version + ".zip below, unzip it, and run `bash install.sh` (see GETTING-STARTED.md)."
      "\nUpdate an existing installation: `~/Rumil/.venv/bin/rumil update`.")
