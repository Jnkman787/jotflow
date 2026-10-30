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

# Each probe prints "reached" for an HTTP answer from api.github.com, "refused" for
# a certificate error, and "unclear" for anything else, which fails the check, so a
# flaky network can never pass it.
probe_claude() {
    out=$(ANTHROPIC_BASE_URL=https://api.github.com ANTHROPIC_API_KEY=sk-ci-not-a-key \
        timeout 90 claude -p hi --max-turns 1 2>&1 || true)
    case "$out" in
        *"certificate verification failed"*) echo refused ;;
        # GitHub's 404 for /v1/messages reads to Claude Code as an unknown model.
        *"issue with the selected model"*) echo reached ;;
        *) echo unclear ;;
    esac
}
probe_codex() {
    out=$(CODEX_HOME="$work/codex" CI_KEY=sk-ci-not-a-key timeout 90 codex exec --skip-git-repo-check \
        -c 'model_provider="ci"' \
        -c 'model_providers.ci={name="ci",base_url="https://api.github.com/v1",wire_api="responses",env_key="CI_KEY"}' \
        -m gpt-5.5 hi 2>&1 || true)
    case "$out" in
        *'"status": "404"'*) echo reached ;;
        # Codex prints the same line for a refused certificate and for no network, so
        # the caller rules the network out around this probe.
        *"Reconnecting... waiting for network"*) echo refused ;;
        *) echo unclear ;;
    esac
}
probe_copilot() {
    rm -rf "$work/copilot-log"
    COPILOT_HOME="$work/copilot" COPILOT_OFFLINE=true COPILOT_PROVIDER_BASE_URL=https://api.github.com/v1 \
        COPILOT_PROVIDER_TYPE=openai COPILOT_PROVIDER_WIRE_API=responses COPILOT_PROVIDER_API_KEY=sk-ci-not-a-key \
        timeout 120 copilot -p hi --model gpt-5.5 --no-auto-update --log-level all \
        --log-dir "$work/copilot-log" >/dev/null 2>&1 || true
    if grep -rqs "HTTP 404" "$work/copilot-log"; then echo reached
    elif grep -rqs "certificate verify failed" "$work/copilot-log"; then echo refused
    else echo unclear; fi
}

# The network is up and api.github.com answers on 443, so a refusal isn't an outage.
network_up() {
    python3 -c 'import socket; socket.create_connection(("api.github.com", 443), 10).close()'
}

sectigo() { # trust or distrust Sectigo's roots in the system store only
    for f in $(ls /usr/share/ca-certificates/mozilla/ | grep -E 'Sectigo|USERTrust'); do
        if [ "$1" = distrust ]; then
            sed -i "s|^mozilla/$f$|!mozilla/$f|" /etc/ca-certificates.conf
        else
            sed -i "s|^!mozilla/$f$|mozilla/$f|" /etc/ca-certificates.conf
        fi
    done
    update-ca-certificates --fresh >/dev/null
}

clis=""
for cli in claude codex copilot; do
    command -v "$cli" >/dev/null 2>&1 && clis="$clis $cli"
done
[ -n "$clis" ] || { echo "no agent CLI is installed in this image"; exit 1; }

for cli in $clis; do
    got=$("probe_$cli")
    [ "$got" = reached ] || { echo "$cli: control failed ($got): it never reached api.github.com"; exit 1; }
done

sectigo distrust
failed=0
for cli in $clis; do
    network_up || { echo "the network went down, so nothing can be concluded"; exit 1; }
    got=$("probe_$cli")
    case "$got" in
        refused) echo "$cli: refused a root the system store distrusts" ;;
        reached) echo "$cli: still trusts a root the system store distrusts: its own bundle is in use"; failed=1 ;;
        *) echo "$cli: couldn't tell ($got)"; failed=1 ;;
    esac
done

# Codex can't tell a refused certificate from no network, so prove it reaches GitHub
# again once Sectigo is trusted: its refusal above was the certificate.
sectigo trust
case " $clis " in *" codex "*)
    got=$(probe_codex)
    if [ "$got" = reached ]; then
        echo "codex: reaches api.github.com again with the root trusted, so its refusal was the certificate"
    else
        echo "codex: didn't reach api.github.com after re-trusting ($got), so its refusal is unproven"
        failed=1
    fi ;;
esac
[ "$failed" = 0 ] && echo "every CLI trusts only the system CA store"
exit "$failed"
