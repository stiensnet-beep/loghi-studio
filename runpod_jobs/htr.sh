#!/bin/bash
# Step 3 on Runpod, in the image loghi/docker.htr: read the cut lines of every book with the model named in
# MODEL (a folder in /vol/models). Started as the pod's command. A book with htr.done is skipped.
MODEL="${MODEL:-globalise-2023-08}"
VOL="${VOL:-/vol}"
GPU="${GPU:-0}"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null
cd /src/loghi-htr/src || exit 1
for work in "$VOL"/work/*/; do
  name=$(basename "$work")
  [ -f "$work/cut.done" ] || continue
  [ -f "$work/htr.done" ] && continue
  echo "$name htr start $(date +%s)" >> "$work/times.txt"
  echo "=== $name htr $(date +%T): $(wc -l < "$work/tmp/lines.txt") lines"
  if TF_USE_LEGACY_KERAS=0 LD_PRELOAD=/usr/lib/$(arch)-linux-gnu/libtcmalloc_minimal.so.4 \
      python3 main.py --model "$VOL/models/$MODEL" --batch_size 64 --gpu "$GPU" \
      --inference_list "$work/tmp/lines.txt" --results_file "$work/tmp/results.txt" \
      --output "$work/tmp/output/" --beam_width 1 > "$work/htr.log" 2>&1; then
    echo "$name htr end $(date +%s)" >> "$work/times.txt"
    date +%T > "$work/htr.done"
    echo "=== $name htr ready $(date +%T): $(wc -l < "$work/tmp/results.txt") lines read"
  else
    echo "FAILED htr $name"; tail -5 "$work/htr.log"
  fi
done
echo "HTR_ALL_DONE $(date +%T)"
date > "$VOL/work/htr.all.done"
[ -n "$NOSLEEP" ] || sleep infinity
