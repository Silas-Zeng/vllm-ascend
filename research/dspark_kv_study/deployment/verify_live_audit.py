"""Save all live read-audit failures and reject an invalid window baseline."""

import json
import sys
from pathlib import Path

if __name__ == "__main__":
    stage, source, output = map(Path, sys.argv[1:])
    sys.path.insert(0, str(stage))
    from audit_draft_reads import summarize

    report = summarize(source)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    if not report["passed"]:
        raise AssertionError("live draft read/write positions failed; do not label this configuration validated")
