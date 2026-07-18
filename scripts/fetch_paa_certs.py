#!/usr/bin/env python3
"""Fetch Matter PAA (Product Attestation Authority) root certificates.

Uses python-matter-server's own fetcher — DCL production + test ledgers and the connectedhomeip
git mirror — the same sources that produced the certificate store the matter tests run against
(including the Chip-Test development PAA the MVD chef devices commission against).

    python scripts/fetch_paa_certs.py [dest_dir]   # default: matter_data/credentials

The DCL occasionally serves a single malformed certificate; upstream's fetcher aborts the whole
source when one fails to parse. We wrap the per-cert writer to skip a malformed cert instead, so
every source completes fully.
"""

import asyncio
import sys
from pathlib import Path

import matter_server.server.helpers.paa_certificates as paa

MIN_CERTS = 30

_orig_write = paa.write_paa_root_cert


async def _resilient_write(*args, **kwargs):
    try:
        return await _orig_write(*args, **kwargs)
    except Exception as err:  # noqa: BLE001 — skip a malformed cert, keep fetching the rest
        print(f"  (skipping malformed certificate: {type(err).__name__})")
        return False


paa.write_paa_root_cert = _resilient_write  # ty: ignore[invalid-assignment]  # monkeypatch, module global


async def main() -> None:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("matter_data/credentials")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".version").unlink(missing_ok=True)  # force a full fetch

    total = await paa.fetch_certificates(dest)
    count = len(list(dest.glob("*.der"))) + len(list(dest.glob("*.pem")))
    if count < MIN_CERTS:
        sys.exit(f"Only {count} PAA certs fetched (< {MIN_CERTS}) — treating as a failed fetch")
    print(f"PAA certificate store ready: {count} certs (fetcher counted {total}) -> {dest}")


if __name__ == "__main__":
    asyncio.run(main())
