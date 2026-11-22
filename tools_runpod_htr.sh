#!/usr/bin/env bash
# Upload cut line snippets of one book to a Runpod pod, read them there with an HTR model on the GPU,
# and fetch results.txt + the output config back. Nothing in the book folders is touched.
# Usage: run_htr_remote.sh <work dir with tmp/imagesnippets> <host> <port> <model>
set -eo pipefail
WORK="$1"; HOST="$2"; PORT="$3"; MODEL="$4"
S="ssh -i $HOME/.ssh/id_ed25519_runpod -p $PORT -o ConnectTimeout=30 -o ServerAliveInterval=30 root@$HOST"
cd "$WORK/tmp"
echo "=== upload $(date +%T)"
tar cf - imagesnippets | $S "rm -rf /work/tmp && mkdir -p /work/tmp && tar xf - -C /work/tmp --no-same-owner"
echo "=== htr $(date +%T)"
$S "cd /work/tmp && find /work/tmp/imagesnippets -type f -name '*.png' > lines.txt && wc -l < lines.txt && \
  TF_USE_LEGACY_KERAS=0 LD_PRELOAD=/usr/lib/\$(arch)-linux-gnu/libtcmalloc_minimal.so.4 \
  python3 /src/loghi-htr/src/main.py --model /models/$MODEL --batch_size 64 \
  --inference_list lines.txt --results_file results.txt --output output/ --beam_width 1 > htr.log 2>&1; \
  rc=\$?; tail -3 htr.log; exit \$rc"
echo "=== download $(date +%T)"
$S "cd /work/tmp && tar cf - results.txt output/config.json htr.log" | tar xf - -C "$WORK/tmp"
wc -l "$WORK/tmp/results.txt"
echo "=== done $(date +%T)"
