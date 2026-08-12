#!/usr/bin/env bash
set -euo pipefail

CALICO_VERSION="${CALICO_VERSION:-v3.32.1}"
POD_CIDR="${POD_CIDR:-100.64.0.0/16}"
MANIFEST_URL="https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

echo "Fetching Calico ${CALICO_VERSION}..."
curl -fsSL "$MANIFEST_URL" -o "$workdir/calico.yaml"

python3 - "$workdir/calico.yaml" "$POD_CIDR" <<'PY'
import re
import sys

path, cidr = sys.argv[1], sys.argv[2]
text = open(path).read()
pattern = re.compile(
    r"(?P<indent>[ ]*)# - name: CALICO_IPV4POOL_CIDR\n[ ]*#[ ]*value: \"[^\"]*\"\n"
)
replacement = f'\\g<indent>- name: CALICO_IPV4POOL_CIDR\n\\g<indent>  value: "{cidr}"\n'
text, count = pattern.subn(replacement, text)
if count != 1:
    sys.exit(
        f"expected exactly one CALICO_IPV4POOL_CIDR block, found {count} — "
        "the upstream manifest layout changed; update this script"
    )
open(path, "w").write(text)
print(f"  set CALICO_IPV4POOL_CIDR={cidr}")
PY

kubectl apply --server-side -f "$workdir/calico.yaml"

echo "Waiting for Calico to become ready..."
kubectl wait --for=condition=Ready pods -n kube-system -l k8s-app=calico-node --timeout=300s
kubectl wait --for=condition=Ready nodes --all --timeout=300s

echo "Calico ${CALICO_VERSION} ready, pod CIDR ${POD_CIDR}"
