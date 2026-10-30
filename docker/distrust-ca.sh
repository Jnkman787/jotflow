#!/bin/sh
# Distrust certificate authorities the system store must not trust, then prove it (#360).
#
# eMudhra's emSign roots: an intermediate under them had its keys compromised
# (GHSA-hq85-5cpj-mr62). uv 0.13.4 and certifi 2026.10.19 dropped them, but the
# pinned Debian base still ships them in ca-certificates. JotFlow's own HTTPS,
# Debian's Node in its default mode, and github-mcp-server all read this store.
#
# Each root is marked "!" in /etc/ca-certificates.conf, which update-ca-certificates
# honours, so a later reinstall of ca-certificates keeps it distrusted. The build
# fails if any emSign certificate is still loadable, or if the store looks broken.
set -eu

conf=/etc/ca-certificates.conf
sed -i 's|^\(mozilla/emSign_[^/]*\.crt\)$|!\1|' "$conf"
update-ca-certificates --fresh >/dev/null

python3 - <<'EOF'
import ssl
import sys

paths = ssl.get_default_verify_paths()
certs = ssl.create_default_context(cafile=paths.openssl_cafile).get_ca_certs()
names = [str(cert.get("subject", "")).casefold() for cert in certs]
bad = [name for name in names if "emsign" in name or "emudhra" in name]
if bad:
    sys.exit(f"the system CA store still trusts {len(bad)} eMudhra/emSign root(s)")
if len(certs) < 100:
    sys.exit(f"the system CA store has only {len(certs)} roots; something went wrong")
print(f"system CA store: {len(certs)} roots, no eMudhra/emSign")
EOF
