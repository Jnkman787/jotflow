#!/bin/sh
# Prove every agent CLI in the image trusts only the system CA store, which the image
# cleaned of the eMudhra/emSign roots (#360). CI runs this as root in a throwaway
# container of the agent image built with the CLIs installed. It needs the network.
#
# For each installed CLI, launched with the environment the runner gives it:
# 1. control: it reaches https://api.github.com, so TLS works at all;
# 2. with Sectigo's roots, which api.github.com chains to, distrusted in the system
#    store only, it must fail the handshake. Still reaching GitHub would mean it also
#    trusts its own compiled-in roots, which include emSign.
set -eu

# The runner gives every run this option (with_system_ca in adapters/runner.py).
export NODE_OPTIONS=--use-openssl-ca
work=$(mktemp -d)
export HOME="$work/home"
mkdir -p "$HOME" "$work/codex" "$work/copilot"

python3 - <<'EOF'
import ssl, sys
paths = ssl.get_default_verify_paths()
certs = ssl.create_default_context(cafile=paths.openssl_cafile).get_ca_certs()
bad = [c for c in certs if "emsign" in str(c).casefold() or "emudhra" in str(c).casefold()]
if bad:
    sys.exit(f"the system CA store still trusts {len(bad)} eMudhra/emSign root(s)")
print(f"system CA store: {len(certs)} roots, no eMudhra/emSign")
EOF

# Each probe prints "reached" if the CLI got an HTTP answer from api.github.com.
probe_claude() {
    out=$(ANTHROPIC_BASE_URL=https://api.github.com ANTHROPIC_API_KEY=sk-ci-not-a-key \
        timeout 90 claude -p hi --max-turns 1 2>&1 || true)
    case "$out" in *"certificate verification failed"*) echo refused ;; *) echo reached ;; esac
}
probe_codex() {
    out=$(CODEX_HOME="$work/codex" CI_KEY=sk-ci-not-a-key timeout 90 codex exec --skip-git-repo-check \
        -c 'model_provider="ci"' \
        -c 'model_providers.ci={name="ci",base_url="https://api.github.com/v1",wire_api="responses",env_key="CI_KEY"}' \
        -m gpt-5.5 hi 2>&1 || true)
    case "$out" in *'"status": "404"'*) echo reached ;; *) echo refused ;; esac
}
probe_copilot() {
    rm -rf "$work/copilot-log"
    COPILOT_HOME="$work/copilot" COPILOT_OFFLINE=true COPILOT_PROVIDER_BASE_URL=https://api.github.com/v1 \
        COPILOT_PROVIDER_TYPE=openai COPILOT_PROVIDER_WIRE_API=responses COPILOT_PROVIDER_API_KEY=sk-ci-not-a-key \
        timeout 120 copilot -p hi --model gpt-5.5 --no-auto-update --log-level all \
        --log-dir "$work/copilot-log" >/dev/null 2>&1 || true
    if grep -rqs "HTTP 404" "$work/copilot-log"; then echo reached; else echo refused; fi
}

clis=""
for cli in claude codex copilot; do
    command -v "$cli" >/dev/null 2>&1 && clis="$clis $cli"
done
[ -n "$clis" ] || { echo "no agent CLI is installed in this image"; exit 1; }

for cli in $clis; do
    got=$("probe_$cli")
    [ "$got" = reached ] || { echo "$cli: control failed ($got): TLS to api.github.com never worked"; exit 1; }
done

for f in $(ls /usr/share/ca-certificates/mozilla/ | grep -E 'Sectigo|USERTrust'); do
    sed -i "s|^mozilla/$f$|!mozilla/$f|" /etc/ca-certificates.conf
done
update-ca-certificates --fresh >/dev/null

failed=0
for cli in $clis; do
    got=$("probe_$cli")
    if [ "$got" = refused ]; then
        echo "$cli: trusts only the system CA store"
    else
        echo "$cli: still trusts a root the system store distrusts: its own bundle is in use"
        failed=1
    fi
done
exit "$failed"
