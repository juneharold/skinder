"""Export API types without opening the database or starting Codex."""

import json
import sys
from pathlib import Path

from skinder.app import create_app

schema = json.dumps(create_app().openapi(), indent=2) + "\n"
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(schema, encoding="utf-8")
else:
    print(schema, end="")
