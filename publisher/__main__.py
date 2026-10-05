from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .publication import finalize, garbage_collect, prepare, upload_shard
from .resources import local_candidate, migration_candidate
from .storage import encode


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "prepare", "migrate", "gc"):
        command = commands.add_parser(name)
        command.add_argument("game", choices=("phigros", "rizline", "kyou"))
        if name != "gc":
            command.add_argument("--output", type=Path, default=Path("work/publication"))
        if name == "prepare":
            command.add_argument("--input", type=Path, required=True)
        if name == "gc":
            command.add_argument("--execute", action="store_true")
    shard = commands.add_parser("shard")
    shard.add_argument("--plan", type=Path, required=True)
    shard.add_argument("--shard", type=int, required=True)
    shard.add_argument("--payload", type=Path, required=True)
    shard.add_argument("--receipt", type=Path, required=True)
    commit = commands.add_parser("finalize")
    commit.add_argument("--plan", type=Path, required=True)
    commit.add_argument("--receipts", type=Path, required=True)
    single = commands.add_parser("publish-plan")
    single.add_argument("--plan", type=Path, required=True)
    single.add_argument("--payload", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        from .build import build
        build(args.game, args.output)
        return
    if args.command in ("prepare", "migrate"):
        migration = args.command == "migrate"
        candidate = migration_candidate(args.game, args.output / "metadata") if migration else local_candidate(args.game, args.input, args.output / "metadata")
        plan = prepare(args.game, candidate, args.output, migration=migration)
        result = {"id": plan["id"], "unchanged": plan["unchanged"], "summary": plan["summary"],
                  "shards": [{"id": row["id"], "bytes": row["bytes"], "count": len(row["keys"])} for row in plan["shards"]]}
        if path := os.environ.get("GITHUB_OUTPUT"):
            with open(path, "a", encoding="utf-8") as output:
                output.write("matrix=" + json.dumps({"shard": [row["id"] for row in plan["shards"]]}) + "\n")
                output.write(f"shards={len(plan['shards'])}\n")
                output.write(f"unchanged={str(plan['unchanged']).lower()}\n")
                output.write(f"attempt={os.environ.get('GITHUB_RUN_ATTEMPT', '1')}\n")
    elif args.command == "shard":
        result = upload_shard(json.loads(args.plan.read_bytes()), args.shard, args.payload, args.receipt)
    elif args.command == "finalize":
        result = finalize(json.loads(args.plan.read_bytes()), [json.loads(path.read_bytes()) for path in args.receipts.glob("**/receipt-*.json")])
    elif args.command == "publish-plan":
        plan = json.loads(args.plan.read_bytes())
        receipts = [upload_shard(plan, row["id"], args.payload / f"shard-{row['id']}", args.payload / f"receipt-{row['id']}.json") for row in plan["shards"]]
        result = finalize(plan, receipts)
    else:
        result = garbage_collect(args.game, execute=args.execute)
    print(encode(result).decode(), end="")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as summary:
            summary.write("```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()
