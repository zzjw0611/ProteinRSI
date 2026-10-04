# SPDX-License-Identifier: MIT
"""Run only inside the explicitly configured ESMC environment; no task labels are provided."""
import json
from pathlib import Path
import sys


def main():
    from proteinrsi.protein.esmc import ESMCConfig, TransformersBackend
    from proteinrsi.storage import Store
    from proteinrsi.localtools.worker import offline_audit
    request = json.loads(Path(sys.argv[1]).read_text())
    snapshot = Path(request["snapshot"])
    if snapshot.name != request["revision"] or not snapshot.is_dir():
        raise ValueError("Invalid pinned model snapshot")
    sys.addaudithook(offline_audit)
    model = TransformersBackend(Store("worker-state"), ESMCConfig.model_validate(request["config"]))
    # Resolve only the already selected public model path; never download from the worker.
    model.resolve = lambda **kwargs: (str(snapshot), request["revision"])
    operation, data = request["operation"], request["data"]
    if operation in ("identity", "load"):
        if operation == "load":
            model.load()
        result = model.identity
    elif operation == "embed":
        result = model.embed(data["sequences"])
    elif operation == "masked":
        result = model.masked(data["reference"], data["positions"])
    else:
        raise ValueError("Unsupported ESMC operation")
    Path("result.json").write_text(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
