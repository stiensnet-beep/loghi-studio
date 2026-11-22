#!/bin/bash
# Step 4 on Runpod, in the hub pod (loghi/docker.loghi-tooling): put the text that was read into the PageXML
# and set the reading order. A book with merge.done is skipped. The PageXML is then packed for download.
BIN=/src/loghi-tooling/minions/target/appassembler/bin
NS=-use_2013_namespace
T="${THREADS:-8}"
MODEL="${MODEL:-globalise-2023-08}"
VOL="${VOL:-/vol}"
for work in "$VOL"/work/*/; do
  name=$(basename "$work")
  [ -f "$work/htr.done" ] || continue
  [ -f "$work/merge.done" ] && continue
  echo "=== $name merge $(date +%T)"
  $BIN/MinionLoghiHTRMergePageXML -input_path "$work/page" -results_file "$work/tmp/results.txt" \
    -config_file "$VOL/models/$MODEL/config.json" -htr_code_config_file "$work/tmp/output/config.json" $NS \
    >> "$work/tool.log" 2>&1 || { echo "FAILED merge $name"; tail -3 "$work/tool.log"; continue; }
  $BIN/MinionRecalculateReadingOrderNew -input_dir "$work/page/" -border_margin 50 -threads "$T" $NS \
    >> "$work/tool.log" 2>&1 || { echo "FAILED order $name"; tail -3 "$work/tool.log"; continue; }
  echo "$name merge end $(date +%s)" >> "$work/times.txt"
  tar cf "$work/pages.tar" -C "$work" --exclude='*.png' page times.txt
  date +%T > "$work/merge.done"
  echo "=== $name merge ready $(date +%T): $(ls "$work/page" | grep -c '\.xml$') pages"
done
echo "MERGE_ALL_DONE $(date +%T)"
