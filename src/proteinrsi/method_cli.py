# SPDX-License-Identifier: MIT
"""Operator-only method inspection and rollback. No model or laboratory calls."""
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store


def add_parser(subparsers):
    parser = subparsers.add_parser("methods", help="Inspect method history or perform an audited pointer-only rollback")
    parser.add_argument("action", choices=["status", "history", "export", "snapshot", "rollback", "abandon", "resume", "abandon-evaluation"])
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--target", choices=["workflow", "meta"])
    parser.add_argument("--version")
    parser.add_argument("--out", help="Export definition JSON and a provenance sidecar; existing differing files are never overwritten")
    parser.add_argument("--snapshot-ref")
    parser.add_argument("--patch-id")
    parser.add_argument("--evaluation-id")
    parser.add_argument("--operator")
    parser.add_argument("--reason")
    parser.add_argument("--acknowledge-uncertain", action="store_true",
        help="Confirm external work has been reconciled/stopped; this does not cancel or refund it")


def run(args):
    from pathlib import Path
    if not (Path(args.campaign)/"state.sqlite3").is_file():
        raise FileNotFoundError("No existing campaign database")
    campaign = Campaign(Store(args.campaign))
    methods = campaign.methods
    if args.action == "history":
        return methods.history()
    if args.action == "export":
        if not args.target or not args.out:
            raise ValueError("export requires --target and --out")
        import json
        result = methods.export(args.target, args.version)
        path = Path(args.out)
        paths = {path: result["definition"], path.with_name(path.name + ".provenance.json"): result["provenance"]}
        for destination, record in paths.items():
            text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
            if destination.exists() and destination.read_text() != text:
                raise FileExistsError("Refusing to overwrite a different export: " + str(destination))
        for destination, record in paths.items():
            if not destination.exists():
                with destination.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return {"path": str(path), **result["provenance"]}
    if args.action == "snapshot":
        if not args.snapshot_ref:
            raise ValueError("snapshot requires --snapshot-ref")
        record = campaign.store.get("method_snapshots", args.snapshot_ref)
        if record is None:
            raise ValueError("Unknown method snapshot")
        return record
    if args.action != "status":
        if not args.operator or not args.reason:
            raise ValueError("Method changes require --operator and --reason")
        if args.action == "rollback":
            if not args.target or not args.version:
                raise ValueError("rollback requires --target and --version")
            methods.rollback(args.target, args.version, operator=args.operator, reason=args.reason)
        elif args.action == "abandon":
            if not args.patch_id:
                raise ValueError("abandon requires --patch-id")
            methods.abandon(args.patch_id, operator=args.operator, reason=args.reason,
                            acknowledge_uncertain=args.acknowledge_uncertain)
        elif args.action == "abandon-evaluation":
            from proteinrsi.harness_evaluation import abandon_evaluation
            abandon_evaluation(campaign, args.evaluation_id, operator=args.operator, reason=args.reason,
                               acknowledge_uncertain=args.acknowledge_uncertain)
        elif args.action == "resume":
            methods.resume(operator=args.operator, reason=args.reason)
    return methods.report()
