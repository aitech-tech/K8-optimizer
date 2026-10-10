"""Command-line entrypoint.

    python -m k8optimizer.cli --file workloads.json
    python -m k8optimizer.cli --serve

``workloads.json`` is a list of workload dicts (same schema as the API).
"""

from __future__ import annotations

import argparse
import json
import sys

from .server import evaluate, serve, workload_from_dict


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="k8-optimizer")
    p.add_argument("--file", help="JSON file of workloads to evaluate")
    p.add_argument("--serve", action="store_true", help="run the REST API server")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args(argv)

    if args.serve:
        serve(port=args.port)
        return 0

    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        workloads = [workload_from_dict(d) for d in data.get("workloads", data)]
        print(json.dumps(evaluate(workloads), indent=2))
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())