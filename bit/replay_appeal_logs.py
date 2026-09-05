"""Replay durable appeal events after a DB outage. Preview by default."""

import argparse
import json
from pathlib import Path


def replay(paths, apply=False):
    from bit.bit_db_api import insert_ai_appeal_record, insert_appeal_chat_record

    counts = {"events": 0, "invalid": 0, "legacy_skipped": 0, "written": 0, "failed": 0}
    seen = set()
    for path in paths:
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("invalid record")
                except (ValueError, TypeError):
                    counts["invalid"] += 1
                    continue
                event_id = event.get("event_id")
                if not event_id:
                    counts["legacy_skipped"] += 1
                    continue
                if event_id in seen:
                    continue
                seen.add(event_id)
                counts["events"] += 1
                if not apply:
                    continue
                try:
                    if event.get("event") == "appeal_record":
                        insert_ai_appeal_record(event["record"])
                    else:
                        insert_appeal_chat_record(event)
                    counts["written"] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    print(f"{event_id}: {exc}")
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--apply", action="store_true", help="Write to database; event IDs prevent duplicates")
    args = parser.parse_args()
    print(json.dumps(replay(args.paths, apply=args.apply), ensure_ascii=False))
