#!/usr/bin/env python3
"""Query OSV for known vulnerabilities in the versions actually installed.

    python3 tools/check-deps.py                    # what is installed here
    python3 tools/check-deps.py --remote           # what is installed on the box
    python3 tools/check-deps.py --strict           # exit 1 if anything is unreviewed

Written because requirements.txt pinned requests==2.31.0 from 2023 and nobody had ever
checked it. Three security reviews looked at this project and none of them asked what the
dependencies were carrying.

Reachability is the point, not the severity label. Of the advisories found on the first
run, every application-level one was unreachable in this deployment -- no verify=False, no
.netrc, no Flask sessions, no proxyauth -- and the one that mattered was a transitive
wheel bundling a vulnerable OpenSSL. A severity column would have ranked those backwards.
So findings are printed with a REACHABLE note where one has been established, and the
absence of a note means nobody has checked, not that it is fine.
"""
import json
import os
import subprocess
import sys
import urllib.request

# Advisories reviewed against this codebase, with the evidence. An entry here is a claim
# that the vulnerable code path cannot be reached, not that the advisory is unimportant --
# and it goes stale the moment the code grows the thing it says is absent.
REVIEWED = {
    "CVE-2024-35195": "requests Session verify=False: grep finds no verify=False outside tests",
    "CVE-2024-47081": "requests .netrc leak: no ~/.netrc for either account on the box",
    "CVE-2026-25645": "requests extract_zipped_paths(): never called",
    "CVE-2026-27205": "Flask session Vary: Cookie: no Flask sessions, no secret_key",
    "CVE-2026-40606": "mitmproxy LDAP injection: proxyauth/ldap not configured in any unit",
    "CVE-2026-69247": "cryptography PKCS#7 EnvelopedData oracle: no EnvelopedData decryption",
    "GHSA-537c-gmf6-5ccf":
        "bundled OpenSSL CVE-2026-45447 is in PKCS7_verify(); TLS and cert generation use "
        "X509_verify_cert, not the PKCS#7 APIs. The SYSTEM OpenSSL is separately patched "
        "(Debian 3.5.6-1~deb13u2+rpt1 names the CVE). Fix needs cryptography>=48.0.1, "
        "which needs mitmproxy>=12.2.3 to lift its cryptography<=46.1 cap.",
}


def installed(remote):
    if remote:
        # Host and path from the environment: hard-coding them here would put a real
        # tailnet address and deployment path into a public repository.
        host = os.environ.get("PI", "pi@raspberrypi.local")
        venv = os.environ.get("COOLDOWN_VENV", "/home/pi/cooldown/venv")
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, f"{venv}/bin/pip list --format=json"],
            capture_output=True, text=True, timeout=60).stdout
    else:
        out = subprocess.run([sys.executable, "-m", "pip", "list", "--format=json"],
                             capture_output=True, text=True).stdout
    return [(p["name"], p["version"]) for p in json.loads(out)]


def query(name, version):
    body = json.dumps({"package": {"name": name, "ecosystem": "PyPI"},
                       "version": version}).encode()
    req = urllib.request.Request("https://api.osv.dev/v1/query", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30)).get("vulns", [])


def main():
    remote = "--remote" in sys.argv
    strict = "--strict" in sys.argv
    pkgs = installed(remote)
    print(f"{len(pkgs)} packages installed {'on the box' if remote else 'here'}\n")
    unreviewed, failed = [], []

    for name, ver in sorted(pkgs):
        try:
            vulns = query(name, ver)
        except Exception as e:
            # A failed query is not a clean result. Saying so is the whole discipline.
            failed.append(name)
            print(f"  {name:16s} {ver:12s} QUERY FAILED ({type(e).__name__}) — UNKNOWN")
            continue
        if not vulns:
            continue
        seen = set()
        for v in vulns:
            ids = [v.get("id")] + (v.get("aliases") or [])
            key = next((i for i in ids if i.startswith("CVE")), v.get("id"))
            if key in seen:
                continue          # OSV returns the GHSA and its CVE alias separately
            seen.add(key)
            note = next((REVIEWED[i] for i in ids if i in REVIEWED), None)
            fixed = sorted({e["fixed"] for a in v.get("affected", [])
                            for r in a.get("ranges", []) for e in r.get("events", [])
                            if "fixed" in e})
            mark = "reviewed " if note else "UNREVIEWED"
            print(f"  {name:16s} {ver:12s} {mark} {key}  fixed in {', '.join(fixed) or '?'}")
            print(f"      {v.get('summary','')[:96]}")
            if note:
                print(f"      -> {note}")
            else:
                unreviewed.append((name, key))
            print()

    print(f"summary: {len(unreviewed)} unreviewed advisory/ies, "
          f"{len(failed)} package(s) could not be checked")
    for n, k in unreviewed:
        print(f"  UNREVIEWED  {n}  {k}")
    if failed:
        print(f"  UNCHECKED   {', '.join(failed)}  — treat as unknown")
    return 1 if strict and (unreviewed or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
