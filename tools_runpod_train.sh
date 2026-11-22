#!/usr/bin/env bash
# Fine-tune a Loghi-htr model on a rented Runpod pod (image loghi/docker.htr) and fetch the result.
# The base model is only read; the new model gets its own folder and name.
#
# Usage: tools_runpod_train.sh <host> <port> <new name> <base model folder> <data folder> <target folder> [epochs] [learning rate]
#   data folder    holds train.txt and val.txt ("<path><tab><text>", paths starting with /work/data/) and
#                  one folder per set with its lines/ (made by tools_train_set.py)
#   target folder  where the folder <new name> is made (must not exist yet)
# Steps: upload, measure the base model on val.txt, train, measure again, download, write a README.
set -eo pipefail
HOST="$1"; PORT="$2"; NAME="$3"; BASE="$4"; DATA="$5"; TARGET="$6"; EPOCHS="${7:-10}"; LR="${8:-0.00001}"
[ -n "$TARGET" ] || { echo "usage: see the top of this file"; exit 2; }
[ -e "$TARGET/$NAME" ] && { echo "$TARGET/$NAME exists already; choose another name"; exit 2; }
[ -s "$DATA/train.txt" ] && [ -s "$DATA/val.txt" ] || { echo "train.txt or val.txt missing in $DATA"; exit 2; }
S="ssh -i $HOME/.ssh/id_ed25519_runpod -p $PORT -o ConnectTimeout=30 -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new root@$HOST"
RUN="cd /src/loghi-htr/src && TF_USE_LEGACY_KERAS=0 LD_PRELOAD=/usr/lib/\$(arch)-linux-gnu/libtcmalloc_minimal.so.4 python3 main.py"
AUG="--aug_elastic_transform --aug_random_crop --aug_random_width --aug_random_shear"

echo "=== upload base model $(date +%T)"
tar cf - -C "$BASE" model.keras config.json tokenizer.json | $S "rm -rf /work && mkdir -p /work/base /work/out && tar xf - -C /work/base --no-same-owner"
echo "=== upload lines $(date +%T)"
tar cf - -C "$DATA" --exclude=tmp --exclude='*.jpg' --exclude=page --exclude=_addresses --exclude=out . | $S "mkdir -p /work/data && tar xf - -C /work/data --no-same-owner"
$S "wc -l /work/data/train.txt /work/data/val.txt; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"

echo "=== base model on the validation lines $(date +%T)"
$S "$RUN --model /work/base --do_validate --validation_list /work/data/val.txt --batch_size 64 --greedy \
  --output /work/out/before > /work/before.log 2>&1; grep -A4 'Final validation statistics' /work/before.log | tail -4"

echo "=== train $(date +%T): $EPOCHS epochs, learning rate $LR"
$S "nohup sh -c \"$RUN --model /work/base --train_list /work/data/train.txt --do_validate \
  --validation_list /work/data/val.txt --epochs $EPOCHS --batch_size 64 --learning_rate $LR --optimizer adam \
  $AUG --early_stopping_patience 5 --greedy --training_verbosity_mode 2 --output /work/out/train \
  > /work/train.log 2>&1; echo \\\$? > /work/train.rc\" > /dev/null 2>&1 &"
while ! $S "test -f /work/train.rc"; do
  sleep 60
  $S "echo \"\$(date +%T) epoch,CER,WER,loss,val_CER,val_WER,val_loss: \$(tail -n +2 /work/out/train/log.csv 2>/dev/null | tail -1)\"" || true
done
RC=$($S "cat /work/train.rc")
$S "grep -A4 'Final validation statistics' /work/train.log | tail -4; grep -a 'completed in\|Total time' /work/train.log"
[ "$RC" = "0" ] || { echo "training failed (rc=$RC):"; $S "tail -20 /work/train.log"; exit 1; }

echo "=== download $(date +%T)"
mkdir -p "$TARGET/$NAME/training"
BEST=$($S "dirname \$(find /work/out/train -path '*best_val/model.keras' | head -1)")
$S "tar cf - -C $BEST model.keras config.json tokenizer.json" | tar xf - -C "$TARGET/$NAME"
$S "cd /work && cp out/train/log.csv out/train/validation.csv out/train/cer_plot.png out/train/loss_plot.png . 2>/dev/null; \
  tar cf - train.log before.log log.csv validation.csv cer_plot.png loss_plot.png data/train.txt data/val.txt 2>/dev/null" \
  | tar xf - -C "$TARGET/$NAME/training" || true
BEFORE=$(grep -a 'INFO - CER = ' "$TARGET/$NAME/training/before.log" | tail -1 | sed 's/.*INFO - //')
AFTER=$(grep -a 'INFO - CER = ' "$TARGET/$NAME/training/train.log" | tail -1 | sed 's/.*INFO - //')
cat > "$TARGET/$NAME/README.txt" <<EOF
$NAME
Loghi-htr model, fine-tuned on $(date +%Y-%m-%d) with Loghi-studio (tools_runpod_train.sh).

Base model: $(basename "$BASE") = GLOBALISE Loghi Handwritten Text Recognition Model, August 2023,
Rutger van Koert, Stefan Klut, Martijn Maas (KNAW Humanities Cluster), https://hdl.handle.net/10622/X2JZYY,
licence CC-BY-4.0. That model is unchanged in its own folder; this is a changed copy.

What was changed: trained further for at most $EPOCHS epochs (learning rate $LR, batch 64, adam, the same
augmentations GLOBALISE used); the best epoch on the validation lines was kept. The letters the model knows
(tokenizer.json) are the same as in the base model.
Training lines: $(wc -l < "$DATA/train.txt") (training/data/train.txt). Validation lines: $(wc -l < "$DATA/val.txt") (training/data/val.txt).
Share of letters wrong on the validation lines: before "$BEFORE", after "$AFTER".
Sources of the lines: see training/data/train.txt and SOURCES.txt.
EOF
[ -f "$DATA/SOURCES.txt" ] && cp "$DATA/SOURCES.txt" "$TARGET/$NAME/"
ls -la "$TARGET/$NAME"
echo "=== done $(date +%T): $TARGET/$NAME | before: $BEFORE | after: $AFTER"
