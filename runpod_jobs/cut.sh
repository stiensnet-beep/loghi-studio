#!/bin/bash
# Step 2 on Runpod, in the image loghi/docker.loghi-tooling (the hub pod, reached over SSH): turn the line
# pictures of step 1 into baselines and cut the line snippets. A book with cut.done is skipped.
BIN=/src/loghi-tooling/minions/target/appassembler/bin
NS=-use_2013_namespace
T="${THREADS:-8}"
VOL="${VOL:-/vol}"
for work in "$VOL"/work/*/; do
  name=$(basename "$work")
  [ -f "$work/lines.done" ] || continue
  [ -f "$work/cut.done" ] && continue
  mkdir -p "$work/tmp/imagesnippets"
  echo "$name baselines start $(date +%s)" >> "$work/times.txt"
  echo "=== $name baselines $(date +%T)"
  $BIN/MinionExtractBaselines -input_path_image "$work" -input_path_png "$work/page/" -input_path_page "$work/page/" \
    -output_path_page "$work/page/" -recalculate_textline_contours_from_baselines -as_single_region $NS \
    >> "$work/tool.log" 2>&1 || { echo "FAILED baselines $name"; tail -3 "$work/tool.log"; continue; }
  echo "$name cut start $(date +%s)" >> "$work/times.txt"
  echo "=== $name cut $(date +%T)"
  $BIN/MinionCutFromImageBasedOnPageXMLNew -input_path "$work" -outputbase "$work/tmp/imagesnippets/" \
    -tmpdir "$work/tmp" -output_type png -channels 4 -threads "$T" $NS \
    >> "$work/tool.log" 2>&1 || { echo "FAILED cut $name"; tail -3 "$work/tool.log"; continue; }
  echo "$name cut end $(date +%s)" >> "$work/times.txt"
  find "$work/tmp/imagesnippets" -type f -name '*.png' > "$work/tmp/lines.txt"
  date +%T > "$work/cut.done"
  echo "=== $name cut ready $(date +%T): $(wc -l < "$work/tmp/lines.txt") lines"
done
echo "CUT_ALL_DONE $(date +%T)"
