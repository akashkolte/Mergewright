#!/usr/bin/env bash
# Run one eval over many tasks when the disk cannot hold every task image. Task by task: pull
# its pinned image, re-validate it if its record is stale or missing, add its trials to the
# eval, remove the image. Cloud session: run as root after scripts/cloud-setup.sh --no-images
# (Mergewright then runs as the user mw). Own machine (Linux or macOS, bash 3.2 is enough): run
# as yourself, from the repo, with the venv in .venv.
#
#   bash scripts/eval-tasks.sh --split dev --system base --k 3 --llm live --jobs 3 \
#     [--eval-id ID] [task ids...]       # default: every task of the split, in list order
#
# The eval id is printed first. If the script stops, run it again with --eval-id ID: tasks whose
# k trials are all written are skipped, and `mergewright eval --resume` runs the missing trials.
# An invalid task is reported and skipped; any other error stops the script.
#
# Environment: MW_MIN_FREE_GB (default 15): no pull starts with less free disk (checked where
# Docker's data folder is on this host; skipped on Docker Desktop, whose disk is in a VM). MW_TASKS,
# MW_RESULTS, MW_WORK: tasks, results and work folders (default eval/tasks, results, runs).
# Only images pinned by digest are pulled and removed, and only those this script pulled; local
# tasks (mergewright-python:1) and the Docker tests' spectree fixture are never removed.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKS="${MW_TASKS:-$REPO/eval/tasks}"
RESULTS="${MW_RESULTS:-$REPO/results}"
WORK="${MW_WORK:-$REPO/runs}"
MIN_FREE_GB="${MW_MIN_FREE_GB:-15}"
FIXTURE=swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64  # as in cloud-setup.sh
log() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }
# As root (cloud), run Mergewright as mw: as root the sandbox user cannot write the workspace.
if [[ $EUID -eq 0 ]] && id mw >/dev/null 2>&1; then
  mw() { su mw -s /bin/bash -c "cd '$REPO' && . .venv/bin/activate && $*"; }
else
  mw() { bash -c "cd '$REPO' && . .venv/bin/activate && $*"; }
fi
free_gb() {  # free GB where Docker keeps images, or nothing if that folder is not on this host
  local dir
  dir=$(docker info --format '{{.DockerRootDir}}')
  [[ -d $dir ]] && df -Pk "$dir" | awk 'NR == 2 {print int($4 / 1048576)}'
}

SPLIT="" SYSTEM=base K="" LLM="" JOBS=1 EVAL_ID="" IDS=()
while (($#)); do
  case $1 in
    --split) SPLIT=$2; shift 2 ;;
    --system) SYSTEM=$2; shift 2 ;;
    --k) K=$2; shift 2 ;;
    --llm) LLM=$2; shift 2 ;;
    --jobs) JOBS=$2; shift 2 ;;
    --eval-id) EVAL_ID=$2; shift 2 ;;
    -*) die "unknown option $1" ;;
    *) IDS+=("$1"); shift ;;
  esac
done
[[ -n $SPLIT && -n $K && -n $LLM ]] || die "--split, --k and --llm are required"
EVAL_ID="${EVAL_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')}"
OUT="$RESULTS/$EVAL_ID"
mkdir -p "$RESULTS" "$WORK"
[[ $EUID -ne 0 ]] || chown -R mw:mw "$REPO" "$RESULTS" "$WORK" "$TASKS"
if ((${#IDS[@]} == 0)); then
  while read -r id _; do IDS+=("$id"); done < <(mw "mergewright tasks list --tasks-dir '$TASKS' --split $SPLIT")
  ((${#IDS[@]})) || die "no $SPLIT tasks in $TASKS"
fi
log "eval $EVAL_ID: ${#IDS[@]} tasks, k=$K, results in $OUT (resume: --eval-id $EVAL_ID)"

state() { mw "mergewright tasks list --tasks-dir '$TASKS'" | awk -v id="$1" '$1 == id {print $3}'; }
done_all() { for k in $(seq 1 "$K"); do [[ -f $OUT/runs/$1/$SYSTEM/$k.json ]] || return 1; done; }

for id in "${IDS[@]}"; do
  log "$id"
  if done_all "$id"; then echo "all $K trials written: skip"; continue; fi
  image=$(python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["image"])' "$TASKS/$id/task.json")
  pulled=0
  if [[ $image == *@sha256:* ]] && ! docker image inspect "$image" >/dev/null 2>&1; then
    free=$(free_gb || true)
    if [[ -n $free ]]; then
      ((free >= MIN_FREE_GB)) || die "only ${free} GB free (MW_MIN_FREE_GB=$MIN_FREE_GB); not pulling $image"
    fi
    echo "pull $image (${free:-unknown} GB free)"
    docker pull -q "$image" >/dev/null
    docker tag "$image" "${image%@*}:latest"
    pulled=1
  fi
  rc=0
  mw "mergewright tasks validate --tasks-dir '$TASKS' --work-dir '$WORK/oracle' --stale $id" || rc=$?
  ((rc <= 1)) || die "validation of $id failed (exit $rc)"
  if [[ $(state "$id") == valid ]]; then
    resume=$([[ -f $OUT/manifest.json ]] && echo "--resume" || echo "--eval-id")
    mw "mergewright eval --split $SPLIT --system $SYSTEM --k $K --llm $LLM --jobs $JOBS" \
      "--tasks-dir '$TASKS' --out '$RESULTS' --work-dir '$WORK/eval' $resume $EVAL_ID --tasks $id"
  else
    echo "INVALID: $id is skipped (see its validation.json)"
  fi
  if ((pulled)) && [[ ${image%@*} != "$FIXTURE" ]]; then
    docker rmi "${image%@*}:latest" "$image" >/dev/null && echo "removed $image"
  fi
done
log "done: eval $EVAL_ID"
echo "report: su mw -s /bin/bash -c '. .venv/bin/activate && mergewright report $OUT'"
