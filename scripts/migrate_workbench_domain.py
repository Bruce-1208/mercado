"""Preview/apply the .cn domain in this project's macOS launchd settings.

This only edits saved configuration. Reload the installed launchd job afterward
to update the running process environment. Existing .com ingress stays intact.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.zeshun.mercado-workbench"
URL_KEYS = ("BIT_DB_API_BASE_URL", "BIT_PUBLIC_WORKBENCH_URL",
            "MELI_REDIRECT_URI", "MERCADO_REDIRECT_URI")
ORIGINS_KEY = "BIT_LOCAL_EXECUTOR_ALLOWED_ORIGINS"


def migrated_url(value):
    parsed = urlsplit(value)
    if parsed.hostname not in {"wuhanzeshun.com", "www.wuhanzeshun.com"}:
        return value
    if parsed.username or parsed.password:
        raise ValueError("Domain URL must not contain credentials")
    hostname = parsed.hostname.replace("wuhanzeshun.com", "wuhanzeshun.cn")
    netloc = hostname + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit(parsed._replace(netloc=netloc))


def migrate_environment(environment):
    result = dict(environment)
    for key in URL_KEYS:
        if key in result:
            result[key] = migrated_url(result[key])
    origins = [value.strip() for value in result.get(ORIGINS_KEY, "").split(",") if value.strip()]
    for host in ("wuhanzeshun.cn", "www.wuhanzeshun.cn", "wuhanzeshun.com", "www.wuhanzeshun.com"):
        origin = "https://" + host
        if origin not in origins:
            origins.append(origin)
    result[ORIGINS_KEY] = ",".join(origins)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write settings; default is preview only")
    args = parser.parse_args()
    paths = [Path("/Library/LaunchDaemons") / (LABEL + ".plist"),
             ROOT.parent / "Library/LaunchAgents" / (LABEL + ".plist")]
    pending = []
    for path in paths:
        if not path.exists():
            continue
        original = path.read_bytes()
        config = plistlib.loads(original)
        if config.get("Label") != LABEL or Path(config.get("WorkingDirectory", "")).resolve() != ROOT:
            raise SystemExit(f"Refusing unrelated service: {path}")
        previous = config.get("EnvironmentVariables", {})
        updated = migrate_environment(previous)
        changed = [key for key in updated if updated[key] != previous.get(key)]
        print(f"{path}: {', '.join(changed) or 'already current'}")
        if changed:
            config["EnvironmentVariables"] = updated
            pending.append((path, original, config))
    if not args.apply:
        print("Preview only. Use sudo python3 scripts/migrate_workbench_domain.py --apply to write.")
        return
    # Check every destination before making any changes.
    for path, _, _ in pending:
        if not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK):
            raise SystemExit(f"Administrator access required: {path}")
    for path, original, config in pending:
        backup = path.with_suffix(".plist.before-cn")
        if not backup.exists():
            shutil.copy2(path, backup)
            backup.chmod(0o600)
        if path.read_bytes() != original:
            raise SystemExit(f"Configuration changed during migration: {path}")
        # Retain the installed file's ownership/mode.
        path.write_bytes(plistlib.dumps(config, sort_keys=False))
        print(f"Updated {path}; backup: {backup}")
    print("Reload the system launchd job to apply its saved environment; see docs/domain-migration.md.")


if __name__ == "__main__":
    main()
