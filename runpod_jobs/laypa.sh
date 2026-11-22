#!/bin/bash
# Step 1 on Runpod, in the image loghi/docker.laypa (user ubuntu, no SSH): enlarge the scans of every
# book in /vol/books and find the lines. Started as the pod's command; progress is in the pod log and in
# /vol/work/<book>/times.txt. A book with lines.done is skipped, so the job can be started again.
DEVICE="${DEVICE:-cuda}"
FACTOR="${FACTOR:-2}"
VOL="${VOL:-/vol}"
cd /src/laypa || exit 1
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null
for book in "$VOL"/books/*/; do
  name=$(basename "$book")
  work="$VOL/work/$name"
  [ -f "$work/lines.done" ] && continue
  mkdir -p "$work/page"
  echo "$name enlarge start $(date +%s)" >> "$work/times.txt"
  echo "=== $name enlarge $(date +%T)"
  python "$VOL/jobs/enlarge.py" "$book" "$work" "$FACTOR" || { echo "FAILED enlarge $name"; continue; }
  echo "$name lines start $(date +%s)" >> "$work/times.txt"
  echo "=== $name lines $(date +%T)"
  if python run.py -c "$VOL/models/baseline2/config.yaml" -i "$work" -o "$work" --opts MODEL.WEIGHTS "" \
      TEST.WEIGHTS "$VOL/models/baseline2/model_best_mIoU.pth" MODEL.DEVICE "$DEVICE" > "$work/laypa.log" 2>&1; then
    echo "$name lines end $(date +%s)" >> "$work/times.txt"
    date +%T > "$work/lines.done"
    echo "=== $name lines ready $(date +%T): $(ls "$work/page" | wc -l) files in page/"
  else
    echo "FAILED lines $name"; tail -5 "$work/laypa.log"
  fi
done
echo "LAYPA_ALL_DONE $(date +%T)"
date > "$VOL/work/laypa.all.done"
[ -n "$NOSLEEP" ] || sleep infinity
