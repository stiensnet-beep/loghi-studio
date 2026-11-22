#!/usr/bin/env bash
# Put the job scripts, the models and the scans of one book on the shared Runpod volume, through the hub pod
# (image loghi/docker.loghi-tooling with the volume at /vol). The book folder is only read.
# Usage: tools_runpod_upload.sh <host> <port> <book folder> <name on the volume> [model=globalise-2023-08]
set -eo pipefail
HOST="$1"; PORT="$2"; BOOK="$3"; NAME="$4"; MODEL="${5:-globalise-2023-08}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MODELS="$HERE/../loghi/models"
S="ssh -i $HOME/.ssh/id_ed25519_runpod -p $PORT -o ConnectTimeout=15 -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new -o BatchMode=yes root@$HOST"
for i in $(seq 1 40); do $S "echo ssh-ok" 2>/dev/null && break; sleep 10; done
echo "=== folders and jobs $(date +%T)"
$S "mkdir -p /vol/jobs /vol/models /vol/books/$NAME /vol/work && chmod 1777 /vol/work && df -h /vol | tail -1"
tar cf - -C "$HERE/runpod_jobs" . | $S "tar xf - -C /vol/jobs --no-same-owner"
if ! $S "test -f /vol/models/$MODEL/model.keras && test -f /vol/models/baseline2/model_best_mIoU.pth"; then
  echo "=== models $(date +%T)"
  tar cf - -C "$MODELS" baseline2 "$MODEL/model.keras" "$MODEL/config.json" "$MODEL/tokenizer.json" \
    | $S "tar xf - -C /vol/models --no-same-owner"
fi
echo "=== scans of $NAME $(date +%T)"
(cd "$BOOK" && find . -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' \) -printf '%f\n' | tar cf - -T -) \
  | $S "tar xf - -C /vol/books/$NAME --no-same-owner"
$S "chmod -R a+rX /vol/jobs /vol/models /vol/books; ls /vol/books/$NAME | wc -l; du -sh /vol/books /vol/models | tr '\n' ' '; nproc; free -g | sed -n 2p"
echo "=== uploaded $(date +%T)"
