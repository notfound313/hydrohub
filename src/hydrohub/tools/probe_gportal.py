from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Probe G-Portal SFTP to list available GSMaP daily directory paths")
    result.add_argument("--report-file", type=output_path, required=True)
    result.add_argument("--host", default="ftp.gportal.jaxa.jp")
    result.add_argument("--port", type=int, default=2051)
    result.add_argument("--username", default=None)
    result.add_argument("--password", default=None)
    return result


def main() -> int:
    args = parser().parse_args()
    paramiko = require("paramiko")

    import os

    username = args.username or os.getenv("GPORTAL_USER")
    password = args.password or os.getenv("GPORTAL_PASSWORD")
    if not username or not password:
        raise RuntimeError("Set GPORTAL_USER and GPORTAL_PASSWORD.")

    paths = ["/", "/standard", "/standard/v8", "/standard/v8/daily_G", "/standard/v8/daily_G/00Z-23Z"]
    results = {}
    transport = paramiko.Transport((args.host, args.port))
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        for path in paths:
            try:
                entries = [e.filename for e in sftp.listdir_attr(path)]
                results[path] = {"exists": True, "entries": entries[:200], "entry_count": len(entries)}
                print(f"[OK] {path}: {len(entries)} entries")
            except Exception as exc:
                results[path] = {"exists": False, "error": repr(exc)}
                print(f"[MISS] {path}: {exc}")
    finally:
        sftp.close()
        transport.close()

    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"CREATED {args.report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())