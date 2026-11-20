#!/usr/bin/env bash
#
# Row #18 section 3: verify acceptance for the svc-a change and hand the accepted
# exact SHA to the deploy node.
#
# This script only *reads* state (git, docs/msdeploy.md, one HTTP GET) and writes
# two evidence files: an acceptance record and a deploy-node handoff request. It
# never deploys, never runs kubectl/helm, never performs a health check and never
# writes a deployment record - deployment and health verification belong to the
# deploy node, which owns the deployment record.
#
# Checks (SDD section 3):
#   1  origin is the svc-a HTTPS repository and the branch is not main/master
#   2  docs/msdeploy.md exists and carries the row #18 marker
#   3  diff merge-base(origin/main, HEAD)..HEAD changes only docs/ and no
#      Kubernetes/Helm manifest
#   4  no svc-b files are changed and no svc-b (or foreign) remote exists
#   5  GET <acceptance-url> returns HTTP 200
#   6  hand off service svc-a, target k8s-prod-a, the exact SHA and the
#      pre-existing manifest reference; optionally validate the deploy node's own
#      deployment record instead of assuming deployment or health success
#
# Exit codes:
#   0  accepted and handed off (with --deploy-record: record verified healthy)
#   2  invalid branch (main/master, detached HEAD, unsafe ref name)
#   5  precondition failed (origin/repo URL, baseline ref, git plumbing)
#   6  usage error
#   7  acceptance check failed (a1/a2/docs-only/svc-b/remote-SHA) - no handoff
#   8  deploy record contract violated (owner/service/target/SHA/manifest/health)
#   9  deploy node reports a non-healthy health_status
#
# Usage:
#   docs/deploy-handoff/acceptance-and-handoff.sh [--out PATH] [--handoff PATH]
#                                                 [--branch NAME] [--remote NAME]
#                                                 [--baseline REF]
#                                                 [--acceptance-url URL]
#                                                 [--http-timeout SECONDS]
#                                                 [--deploy-record PATH]
#                                                 [--service NAME] [--target NAME]
#                                                 [--repo-url URL]
#                                                 [--manifest-reference VALUE]
#
# Both records default to the git-ignored docs/deploy-handoff/handoff/ directory
# so the accepted branch diff stays documentation-only. --deploy-record expects a
# record written by the deploy node; without it the handoff stays "pending" and
# this run claims no deployment.

set -uo pipefail

SERVICE="svc-a"
TARGET="k8s-prod-a"
REMOTE="origin"
EXPECTED_REPO_URL="https://github.com/Neytronik/mockarty-row18-deploy-a.git"
MANIFEST_REFERENCE="pre-existing"
BASELINE="origin/main"
ACCEPTANCE_URL="http://127.0.0.1:8798/"
HTTP_TIMEOUT="10"
BRANCH=""
OUT="docs/deploy-handoff/handoff/acceptance-svc-a.json"
HANDOFF="docs/deploy-handoff/handoff/deploy-handoff-svc-a.json"
DEPLOY_RECORD=""

die() {
  local rc="$1"; shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "$rc"
}

usage() {
  sed -n '2,47p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:-}"; shift 2 || die 6 "--out needs a value" ;;
    --handoff) HANDOFF="${2:-}"; shift 2 || die 6 "--handoff needs a value" ;;
    --branch) BRANCH="${2:-}"; shift 2 || die 6 "--branch needs a value" ;;
    --remote) REMOTE="${2:-}"; shift 2 || die 6 "--remote needs a value" ;;
    --baseline) BASELINE="${2:-}"; shift 2 || die 6 "--baseline needs a value" ;;
    --acceptance-url) ACCEPTANCE_URL="${2:-}"; shift 2 || die 6 "--acceptance-url needs a value" ;;
    --http-timeout) HTTP_TIMEOUT="${2:-}"; shift 2 || die 6 "--http-timeout needs a value" ;;
    --deploy-record) DEPLOY_RECORD="${2:-}"; shift 2 || die 6 "--deploy-record needs a value" ;;
    --service) SERVICE="${2:-}"; shift 2 || die 6 "--service needs a value" ;;
    --target) TARGET="${2:-}"; shift 2 || die 6 "--target needs a value" ;;
    --repo-url) EXPECTED_REPO_URL="${2:-}"; shift 2 || die 6 "--repo-url needs a value" ;;
    --manifest-reference) MANIFEST_REFERENCE="${2:-}"; shift 2 || die 6 "--manifest-reference needs a value" ;;
    -h|--help) usage; exit 0 ;;
    *) die 6 "unknown argument: $1" ;;
  esac
done

[ -n "$OUT" ] || die 6 "--out PATH must not be empty"
[ -n "$HANDOFF" ] || die 6 "--handoff PATH must not be empty"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 5 "not inside a git work tree"
command -v curl >/dev/null 2>&1 || die 5 "curl is required to check the acceptance surface"

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Escape a value for use inside a JSON string literal.
json_escape() {
  printf '%s' "$1" \
    | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' \
    | tr -d '\001-\010\013\014\016-\037'
}

json_array() { # values...
  local first=1 value out=""
  for value in "$@"; do
    if [ "$first" -eq 1 ]; then first=0; else out="$out, "; fi
    out="$out\"$(json_escape "$value")\""
  done
  printf '[%s]' "$out"
}

bool_of() { # 1 -> true, anything else -> false
  case "${1:-0}" in
    1) printf 'true' ;;
    *) printf 'false' ;;
  esac
}

# ---------------------------------------------------------------------------
# 1) Branch and origin guards (fail fast: nothing is written on these)
# ---------------------------------------------------------------------------
if [ -z "$BRANCH" ]; then
  BRANCH=$(git rev-parse --abbrev-ref HEAD) || die 2 "cannot resolve the current branch"
fi
[ "$BRANCH" != "HEAD" ] || die 2 "detached HEAD: check out the per-run feature branch first"
case "$BRANCH" in
  main|master) die 2 "refusing to accept on the default branch '$BRANCH': use a per-run feature branch" ;;
esac
case "$BRANCH" in
  ''|*[!A-Za-z0-9._/-]*) die 2 "unsafe branch name for the acceptance record: '$BRANCH'" ;;
esac
git check-ref-format "refs/heads/$BRANCH" >/dev/null 2>&1 || die 2 "invalid branch name: '$BRANCH'"

REPO_URL=$(git config --get "remote.$REMOTE.url" 2>/dev/null) || die 5 "remote '$REMOTE' is not configured"
case "$REPO_URL" in
  https://*) ;;
  *) die 5 "remote '$REMOTE' is not HTTPS: $REPO_URL" ;;
esac
[ "$REPO_URL" = "$EXPECTED_REPO_URL" ] || \
  die 5 "remote '$REMOTE' is $REPO_URL, expected $EXPECTED_REPO_URL"

BASE_COMMIT=$(git rev-parse --verify --quiet "$BASELINE^{commit}") || \
  die 5 "baseline '$BASELINE' is not available; fetch it first (git fetch origin main)"

LOCAL_SHA=$(git rev-parse HEAD) || die 5 "cannot read local HEAD"
case "$LOCAL_SHA" in
  *[!0-9a-f]*) die 5 "HEAD is not a lowercase hex commit SHA: $LOCAL_SHA" ;;
esac
[ "${#LOCAL_SHA}" -eq 40 ] || [ "${#LOCAL_SHA}" -eq 64 ] || \
  die 5 "HEAD is not a 40- or 64-character commit SHA: $LOCAL_SHA"

# Read the SHA of exactly refs/heads/<branch>; ls-remote may return extra refs.
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

REMOTE_SHA=""
REMOTE_SHA_READ=0
if REMOTE_SHA=$(query_remote_ref); then
  REMOTE_SHA_READ=1
else
  REMOTE_SHA=""
fi

# ---------------------------------------------------------------------------
# 2) docs/msdeploy.md exists and carries the row #18 marker
# ---------------------------------------------------------------------------
A2_FILE=0
[ -f "docs/msdeploy.md" ] && A2_FILE=1
A2_MARKER=0
A2_MARKER_LINE=0
if [ "$A2_FILE" -eq 1 ]; then
  # The SDD test: the file mentions the row #18 marker at all.
  grep -qi 'row #18' docs/msdeploy.md && A2_MARKER=1
  # The data model's marker line. Checked separately because the document title
  # also contains "Row #18", so the loose grep alone would accept a file whose
  # marker line was lost.
  grep -Eqi '^[[:space:]]*Marker:[[:space:]]*row #18[[:space:]]*$' docs/msdeploy.md && \
    A2_MARKER_LINE=1
fi

# ---------------------------------------------------------------------------
# 3) + 4) documentation-only diff, no manifest, no svc-b footprint
# ---------------------------------------------------------------------------
# A path is manifest-like when it is YAML/templated or lives in a
# Kubernetes/Helm directory segment. Whole-segment matching keeps a docs
# directory such as docs/deploy-handoff/ from being mistaken for deploy/.
is_manifest_path() {
  local path="$1" base seg rest
  base=${path##*/}
  case "$base" in
    *.yaml|*.yml|*.tpl) return 0 ;;
    Chart.yaml|Chart.lock|kustomization.json) return 0 ;;
  esac
  rest="$path"
  while [ -n "$rest" ]; do
    seg=${rest%%/*}
    case "$seg" in
      helm|charts|k8s|k8s-*|kubernetes|manifests|manifest|kustomize|overlays|templates|deploy) return 0 ;;
    esac
    case "$rest" in
      */*) rest=${rest#*/} ;;
      *) rest="" ;;
    esac
  done
  return 1
}

CHANGED=$(git diff --name-only "$BASE_COMMIT"..HEAD) || die 5 "cannot diff $BASE_COMMIT..HEAD"
CHANGED_NONEMPTY=0
ONLY_DOCS=1
CHANGED_FILES=()
NON_DOC_FILES=()
MANIFEST_FILES=()
SVCB_FILES=()

while IFS= read -r path; do
  [ -n "$path" ] || continue
  CHANGED_NONEMPTY=1
  CHANGED_FILES+=("$path")
  case "$path" in
    docs/*) ;;
    *) ONLY_DOCS=0; NON_DOC_FILES+=("$path") ;;
  esac
  if is_manifest_path "$path"; then
    MANIFEST_FILES+=("$path")
  fi
  case "$path" in
    *svc-b*|*/svc-b/*) SVCB_FILES+=("$path") ;;
  esac
done <<< "$CHANGED"

FOREIGN_REMOTES=()
for remote_name in $(git remote); do
  remote_url=$(git config --get "remote.$remote_name.url" 2>/dev/null) || remote_url=""
  case "$remote_url" in
    *row18-deploy-b*|*svc-b*)
      FOREIGN_REMOTES+=("$remote_name $remote_url")
      continue
      ;;
  esac
  if [ "$remote_name" != "$REMOTE" ] && [ "$remote_url" != "$EXPECTED_REPO_URL" ]; then
    FOREIGN_REMOTES+=("$remote_name $remote_url")
  fi
done

# ---------------------------------------------------------------------------
# 5) deploy stand acceptance surface must answer HTTP 200
# ---------------------------------------------------------------------------
HTTP_STATUS=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$HTTP_TIMEOUT" \
  "$ACCEPTANCE_URL" 2>/dev/null)
case "$HTTP_STATUS" in
  ''|*[!0-9]*) HTTP_STATUS=0 ;;
esac
# curl reports 000 when no status line arrived; 000 is not valid JSON, so record
# the numeric 0 instead.
case "$HTTP_STATUS" in
  0*) HTTP_STATUS=$(printf '%d' "$HTTP_STATUS" 2>/dev/null || printf '0') ;;
esac
A1_OK=0
[ "$HTTP_STATUS" = "200" ] && A1_OK=1

# ---------------------------------------------------------------------------
# Acceptance verdict
# ---------------------------------------------------------------------------
FAILED=()
[ "$A1_OK" -eq 1 ] || FAILED+=("a1_http_200")
[ "$A2_FILE" -eq 1 ] || FAILED+=("a2_file_exists")
[ "$A2_MARKER" -eq 1 ] || FAILED+=("a2_marker_present")
[ "$A2_MARKER_LINE" -eq 1 ] || FAILED+=("a2_marker_line_present")
[ "$CHANGED_NONEMPTY" -eq 1 ] || FAILED+=("changed_files_nonempty")
[ "$ONLY_DOCS" -eq 1 ] || FAILED+=("changed_files_only_docs")
[ "${#MANIFEST_FILES[@]}" -eq 0 ] || FAILED+=("no_manifest_changed")
[ "${#SVCB_FILES[@]}" -eq 0 ] || FAILED+=("no_svc_b_files")
[ "${#FOREIGN_REMOTES[@]}" -eq 0 ] || FAILED+=("no_svc_b_or_foreign_remotes")
[ "$REMOTE_SHA_READ" -eq 1 ] || FAILED+=("remote_ref_readable")
if [ "$REMOTE_SHA_READ" -eq 1 ] && [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
  FAILED+=("remote_sha_matches_local")
fi

ACCEPTED=1
[ "${#FAILED[@]}" -eq 0 ] || ACCEPTED=0

mkdir -p "$(dirname "$OUT")" || die 5 "cannot create $(dirname "$OUT")"
TMP_OUT="$OUT.tmp.$$"
{
  printf '{\n'
  printf '  "service": "%s",\n' "$(json_escape "$SERVICE")"
  printf '  "target": "%s",\n' "$(json_escape "$TARGET")"
  printf '  "deploy_owner": "deploy-node",\n'
  printf '  "branch": "%s",\n' "$(json_escape "$BRANCH")"
  printf '  "repository": "%s",\n' "$(json_escape "$REPO_URL")"
  printf '  "baseline_ref": "%s",\n' "$(json_escape "$BASELINE")"
  printf '  "merge_base": "%s",\n' "$BASE_COMMIT"
  printf '  "local_sha": "%s",\n' "$LOCAL_SHA"
  printf '  "remote_sha": "%s",\n' "$REMOTE_SHA"
  printf '  "a1_url": "%s",\n' "$(json_escape "$ACCEPTANCE_URL")"
  printf '  "a1_http_status": %s,\n' "$HTTP_STATUS"
  printf '  "a2_file_exists": %s,\n' "$(bool_of "$A2_FILE")"
  printf '  "a2_marker_present": %s,\n' "$(bool_of "$A2_MARKER")"
  printf '  "a2_marker_line_present": %s,\n' "$(bool_of "$A2_MARKER_LINE")"
  printf '  "changed_files_only_docs": %s,\n' "$(bool_of "$ONLY_DOCS")"
  printf '  "changed_files": %s,\n' "$(json_array "${CHANGED_FILES[@]:-}")"
  printf '  "non_doc_files": %s,\n' "$(json_array ${NON_DOC_FILES[@]+"${NON_DOC_FILES[@]}"})"
  printf '  "manifest_files": %s,\n' "$(json_array ${MANIFEST_FILES[@]+"${MANIFEST_FILES[@]}"})"
  printf '  "svc_b_untouched": %s,\n' "$(bool_of "$(if [ "${#SVCB_FILES[@]}" -eq 0 ] && [ "${#FOREIGN_REMOTES[@]}" -eq 0 ]; then echo 1; else echo 0; fi)")"
  printf '  "svc_b_files": %s,\n' "$(json_array ${SVCB_FILES[@]+"${SVCB_FILES[@]}"})"
  printf '  "foreign_remotes": %s,\n' "$(json_array ${FOREIGN_REMOTES[@]+"${FOREIGN_REMOTES[@]}"})"
  printf '  "accepted": %s,\n' "$(bool_of "$ACCEPTED")"
  printf '  "failed_checks": %s,\n' "$(json_array ${FAILED[@]+"${FAILED[@]}"})"
  printf '  "verified_at": "%s"\n' "$(now_utc)"
  printf '}\n'
} > "$TMP_OUT" || die 5 "cannot write $TMP_OUT"
mv "$TMP_OUT" "$OUT" || die 5 "cannot move the acceptance record into place"

printf 'acceptance: a1_http_status=%s a2_file_exists=%s a2_marker_present=%s a2_marker_line_present=%s changed_files_only_docs=%s remote_sha_match=%s\n' \
  "$HTTP_STATUS" "$(bool_of "$A2_FILE")" "$(bool_of "$A2_MARKER")" "$(bool_of "$A2_MARKER_LINE")" \
  "$(bool_of "$ONLY_DOCS")" \
  "$(bool_of "$(if [ "$REMOTE_SHA_READ" -eq 1 ] && [ "$REMOTE_SHA" = "$LOCAL_SHA" ]; then echo 1; else echo 0; fi)")"
printf 'record: %s\n' "$OUT"

if [ "$ACCEPTED" -ne 1 ]; then
  for failed in "${FAILED[@]}"; do
    printf 'FAILED check: %s\n' "$failed" >&2
  done
  [ "${#NON_DOC_FILES[@]}" -eq 0 ] || \
    printf 'non-docs changes: %s\n' "${NON_DOC_FILES[*]}" >&2
  [ "${#MANIFEST_FILES[@]}" -eq 0 ] || \
    printf 'manifest changes: %s\n' "${MANIFEST_FILES[*]}" >&2
  [ "${#FOREIGN_REMOTES[@]}" -eq 0 ] || \
    printf 'out-of-scope remotes: %s\n' "${FOREIGN_REMOTES[*]}" >&2
  printf 'acceptance NOT met for %s on branch %s: no deploy-node handoff written, no deployment claimed\n' \
    "$SERVICE" "$BRANCH" >&2
  exit 7
fi

# ---------------------------------------------------------------------------
# 6) Deploy-node handoff. This is a REQUEST: deployment and health verification
#    remain with the deploy node, so no health_status is written here.
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$HANDOFF")" || die 5 "cannot create $(dirname "$HANDOFF")"
TMP_HANDOFF="$HANDOFF.tmp.$$"
{
  printf '{\n'
  printf '  "service": "%s",\n' "$(json_escape "$SERVICE")"
  printf '  "target": "%s",\n' "$(json_escape "$TARGET")"
  printf '  "repository": "%s",\n' "$(json_escape "$REPO_URL")"
  printf '  "branch": "%s",\n' "$(json_escape "$BRANCH")"
  printf '  "exact_sha": "%s",\n' "$LOCAL_SHA"
  printf '  "manifest_reference": "%s",\n' "$(json_escape "$MANIFEST_REFERENCE")"
  printf '  "deploy_owner": "deploy-node",\n'
  printf '  "deployment_status": "pending-external-deploy-node",\n'
  printf '  "acceptance": {\n'
  printf '    "record": "%s",\n' "$(json_escape "$OUT")"
  printf '    "a1_http_status": %s,\n' "$HTTP_STATUS"
  printf '    "a2_file_exists": %s,\n' "$(bool_of "$A2_FILE")"
  printf '    "a2_marker_present": %s,\n' "$(bool_of "$A2_MARKER")"
  printf '    "a2_marker_line_present": %s,\n' "$(bool_of "$A2_MARKER_LINE")"
  printf '    "changed_files_only_docs": %s,\n' "$(bool_of "$ONLY_DOCS")"
  printf '    "local_sha": "%s",\n' "$LOCAL_SHA"
  printf '    "remote_sha": "%s"\n' "$REMOTE_SHA"
  printf '  },\n'
  printf '  "expected_deployment_record": {\n'
  printf '    "service": "%s",\n' "$(json_escape "$SERVICE")"
  printf '    "target": "%s",\n' "$(json_escape "$TARGET")"
  printf '    "exact_sha": "%s",\n' "$LOCAL_SHA"
  printf '    "manifest_reference": "%s",\n' "$(json_escape "$MANIFEST_REFERENCE")"
  printf '    "deployed_by": "deploy-node",\n'
  printf '    "health_status_source": "deploy-node"\n'
  printf '  },\n'
  printf '  "handed_off_at": "%s"\n' "$(now_utc)"
  printf '}\n'
} > "$TMP_HANDOFF" || die 5 "cannot write $TMP_HANDOFF"
mv "$TMP_HANDOFF" "$HANDOFF" || die 5 "cannot move the handoff record into place"

printf 'handoff: service=%s target=%s exact_sha=%s manifest_reference=%s deploy_owner=deploy-node\n' \
  "$SERVICE" "$TARGET" "$LOCAL_SHA" "$MANIFEST_REFERENCE"
printf 'handoff record: %s\n' "$HANDOFF"
printf 'deployment: pending - the deploy node performs the deploy and the health check\n'

if [ -z "$DEPLOY_RECORD" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Optional: validate the deploy node's own record. Never fabricates a value.
# ---------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || \
  die 5 "python3 is required to validate --deploy-record"

python3 - "$DEPLOY_RECORD" "$SERVICE" "$TARGET" "$LOCAL_SHA" "$MANIFEST_REFERENCE" <<'PY'
import json
import sys

path, service, target, sha, manifest = sys.argv[1:6]
try:
    with open(path) as handle:
        record = json.load(handle)
except FileNotFoundError:
    print('deploy record not found: %s' % path)
    sys.exit(8)
except Exception as exc:  # noqa: BLE001 - any parse failure is a contract failure
    print('deploy record is not readable JSON (%s): %s' % (path, exc))
    sys.exit(8)
if not isinstance(record, dict):
    print('deploy record is not a JSON object: %s' % path)
    sys.exit(8)

problems = []
for key, want in (
    ('service', service),
    ('target', target),
    ('exact_sha', sha),
    ('manifest_reference', manifest),
    ('deployed_by', 'deploy-node'),
):
    got = record.get(key)
    if got is None or got == '':
        problems.append('missing field %s' % key)
    elif got != want:
        problems.append('%s is %r, expected %r' % (key, got, want))

health = record.get('health_status')
if not isinstance(health, str) or not health.strip():
    problems.append('missing health_status (the deploy node must report it)')

if problems:
    for problem in problems:
        print('deploy record: %s' % problem)
    sys.exit(8)

status = health.strip().lower()
healthy = {'healthy', 'ok', 'pass', 'passed', 'success', 'succeeded', 'ready',
           'green', 'up', 'available'}
print('deploy record verified: service=%s target=%s exact_sha=%s health_status=%s deployed_by=%s'
      % (service, target, sha, health.strip(), record.get('deployed_by')))
if status not in healthy:
    print('deploy node reports health_status=%r, which is not a healthy result; recording failure'
          % health.strip())
    sys.exit(9)
sys.exit(0)
PY
rc=$?
case "$rc" in
  0) exit 0 ;;
  8|9) exit "$rc" ;;
  *) die 5 "deploy record validation could not run (exit $rc)" ;;
esac
