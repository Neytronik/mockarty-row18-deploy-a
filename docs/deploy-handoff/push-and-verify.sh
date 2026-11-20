#!/usr/bin/env bash
#
# Push the per-run svc-a feature branch to the HTTPS origin and hand off the
# exact accepted SHA to the deploy node.
#
# The deploy node deploys svc-a to k8s-prod-a pinned to exact_sha from the
# emitted handoff record; the branch name is traceability only and is never a
# deployment reference.
#
# Exit codes:
#   0  branch pushed (or already present at the same SHA) and remote SHA equals local HEAD
#   2  invalid branch (main/master, detached HEAD, unsafe ref name)
#   3  push rejected / failed
#   4  remote ref SHA does not equal local HEAD after one re-query
#   5  precondition failed (repo URL, work tree state, ls-remote)
#   6  usage error
#
# Usage:
#   docs/deploy-handoff/push-and-verify.sh [--out PATH] [--branch NAME]
#                                          [--service NAME] [--target NAME]
#                                          [--remote NAME] [--repo-url URL]
#                                          [--manifest-reference VALUE]
#
# This script never force-pushes and never runs kubectl/helm.

set -uo pipefail

SERVICE="svc-a"
TARGET="k8s-prod-a"
REMOTE="origin"
EXPECTED_REPO_URL="https://github.com/Neytronik/mockarty-row18-deploy-a.git"
MANIFEST_REFERENCE="pre-existing"
BRANCH=""
OUT=""

die() {
  local rc="$1"; shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "$rc"
}

usage() {
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:-}"; shift 2 || die 6 "--out needs a value" ;;
    --branch) BRANCH="${2:-}"; shift 2 || die 6 "--branch needs a value" ;;
    --service) SERVICE="${2:-}"; shift 2 || die 6 "--service needs a value" ;;
    --target) TARGET="${2:-}"; shift 2 || die 6 "--target needs a value" ;;
    --remote) REMOTE="${2:-}"; shift 2 || die 6 "--remote needs a value" ;;
    --repo-url) EXPECTED_REPO_URL="${2:-}"; shift 2 || die 6 "--repo-url needs a value" ;;
    --manifest-reference) MANIFEST_REFERENCE="${2:-}"; shift 2 || die 6 "--manifest-reference needs a value" ;;
    -h|--help) usage; exit 0 ;;
    *) die 6 "unknown argument: $1" ;;
  esac
done

[ -n "$OUT" ] || die 6 "--out PATH is required"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 5 "not inside a git work tree"

# 1) Branch guard: never push main/master, never a detached HEAD.
if [ -z "$BRANCH" ]; then
  BRANCH=$(git rev-parse --abbrev-ref HEAD) || die 2 "cannot resolve the current branch"
fi
[ "$BRANCH" != "HEAD" ] || die 2 "detached HEAD: check out the per-run feature branch first"
case "$BRANCH" in
  main|master) die 2 "refusing to push the default branch '$BRANCH': use a per-run feature branch" ;;
esac
# Conservative branch charset: keeps the emitted JSON literal-safe and is a
# superset of every ref this repository uses.
case "$BRANCH" in
  ''|*[!A-Za-z0-9._/-]*) die 2 "unsafe branch name for handoff: '$BRANCH'" ;;
esac
git check-ref-format "refs/heads/$BRANCH" >/dev/null 2>&1 || die 2 "invalid branch name: '$BRANCH'"

# Origin must be the svc-a HTTPS repository. Read the configured value
# (url.<base>.insteadOf rewrites are a transport concern, not the declared URL).
REPO_URL=$(git config --get "remote.$REMOTE.url" 2>/dev/null) || die 5 "remote '$REMOTE' is not configured"
case "$REPO_URL" in
  https://*) ;;
  *) die 5 "remote '$REMOTE' is not HTTPS: $REPO_URL" ;;
esac
[ "$REPO_URL" = "$EXPECTED_REPO_URL" ] || \
  die 5 "remote '$REMOTE' is $REPO_URL, expected $EXPECTED_REPO_URL"

# The accepted SHA must contain the whole change: no unstaged/staged edits.
DIRTY=$(git status --porcelain --untracked-files=no) || die 5 "git status failed"
[ -z "$DIRTY" ] || die 5 "tracked changes present; commit them before handoff:
$DIRTY"

LOCAL_SHA=$(git rev-parse HEAD) || die 5 "cannot read local HEAD"
case "$LOCAL_SHA" in
  *[!0-9a-f]*) die 5 "HEAD is not a lowercase hex commit SHA: $LOCAL_SHA" ;;
esac
[ "${#LOCAL_SHA}" -eq 40 ] || [ "${#LOCAL_SHA}" -eq 64 ] || \
  die 5 "HEAD is not a 40- or 64-character commit SHA: $LOCAL_SHA"

# Read the SHA of exactly refs/heads/<branch>; ls-remote may return extra refs,
# so filter on the exact ref name instead of taking the first line.
query_remote_ref() {
  local want="refs/heads/$BRANCH" out line sha ref count=0 found=""
  out=$(git ls-remote "$REMOTE" "$want" 2>&1) || {
    printf 'ls-remote failed: %s\n' "$out" >&2
    return 1
  }
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    sha=${line%%[[:space:]]*}
    ref=${line#*[[:space:]]}
    [ "$ref" = "$want" ] || continue
    count=$((count + 1))
    found="$sha"
  done <<< "$out"
  [ "$count" -le 1 ] || {
    printf 'ls-remote returned %s lines for %s\n' "$count" "$want" >&2
    return 1
  }
  printf '%s' "$found"
}

REMOTE_SHA=$(query_remote_ref) || die 5 "cannot read remote ref refs/heads/$BRANCH"

if [ -n "$REMOTE_SHA" ]; then
  # A remote branch is a one-shot landing zone: reuse it only when it already
  # points at the accepted commit. Never force-push over someone else's work.
  if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
    die 5 "remote branch '$BRANCH' already exists at $REMOTE_SHA (local HEAD $LOCAL_SHA); refusing to force-push - push a unique branch"
  fi
  printf 'branch already pushed: %s at %s\n' "refs/heads/$BRANCH" "$REMOTE_SHA"
else
  # 2) Push the branch over HTTPS. No --force, no --force-with-lease.
  git push --set-upstream "$REMOTE" "refs/heads/$BRANCH:refs/heads/$BRANCH" || \
    die 3 "push of '$BRANCH' to '$REMOTE' failed; stopping (no retry, no force-push)"
fi

# 3) Verify the remote ref SHA equals local HEAD, re-querying exactly once.
REMOTE_SHA=$(query_remote_ref) || die 5 "cannot read remote ref refs/heads/$BRANCH"
if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
  printf 'mismatch: local=%s remote=%s - re-querying once\n' "$LOCAL_SHA" "$REMOTE_SHA" >&2
  REMOTE_SHA=$(query_remote_ref) || die 5 "cannot re-read remote ref refs/heads/$BRANCH"
  if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
    die 4 "remote SHA $REMOTE_SHA does not equal local HEAD $LOCAL_SHA after re-query; not handing off"
  fi
fi

# 4) Emit the deploy handoff record. exact_sha is the deployment reference.
OUT_DIR=$(dirname "$OUT")
mkdir -p "$OUT_DIR" || die 5 "cannot create $OUT_DIR"
TMP_OUT="$OUT.tmp.$$"
{
  printf '{\n'
  printf '  "service": "%s",\n' "$SERVICE"
  printf '  "target": "%s",\n' "$TARGET"
  printf '  "repository": "%s",\n' "$EXPECTED_REPO_URL"
  printf '  "branch": "%s",\n' "$BRANCH"
  printf '  "exact_sha": "%s",\n' "$LOCAL_SHA"
  printf '  "manifest_reference": "%s",\n' "$MANIFEST_REFERENCE"
  printf '  "verification": {\n'
  printf '    "remote": "%s",\n' "$REMOTE"
  printf '    "ref": "refs/heads/%s",\n' "$BRANCH"
  printf '    "local_sha": "%s",\n' "$LOCAL_SHA"
  printf '    "remote_sha": "%s",\n' "$REMOTE_SHA"
  printf '    "sha_match": true,\n'
  printf '    "verified_at": "%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '  }\n'
  printf '}\n'
} > "$TMP_OUT" || die 5 "cannot write $TMP_OUT"
mv "$TMP_OUT" "$OUT" || die 5 "cannot move handoff record into place"

printf 'verified: refs/heads/%s = %s (local HEAD)\n' "$BRANCH" "$LOCAL_SHA"
printf 'handoff: service=%s target=%s exact_sha=%s manifest_reference=%s\n' \
  "$SERVICE" "$TARGET" "$LOCAL_SHA" "$MANIFEST_REFERENCE"
printf 'record: %s\n' "$OUT"
