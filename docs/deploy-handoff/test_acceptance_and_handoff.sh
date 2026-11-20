#!/usr/bin/env bash
#
# Hermetic tests for docs/deploy-handoff/acceptance-and-handoff.sh.
#
# Every git fixture is a throwaway bare repository plus a work clone whose origin
# is a fake HTTPS URL rewritten to the local bare repository through git's
# url.insteadOf, so the script's HTTPS/branch/ls-remote guards run without
# touching the real GitHub origin. The acceptance surface (a1) is served by a
# loopback fixture server, or by an externally supplied URL through
# ROW18_HTTP_OK_URL / ROW18_HTTP_FAIL_URL / ROW18_HTTP_DEAD_URL.
#
# No network beyond loopback, no kubectl/helm, no deployment, no health check.
#
# Run: bash docs/deploy-handoff/test_acceptance_and_handoff.sh

set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SUBJECT="$HERE/acceptance-and-handoff.sh"
REAL_GIT=$(command -v git)
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/svc-a-acceptance.XXXXXX")
FAKE_URL="https://git.example.test/svc-a.git"
SVCB_URL="https://git.example.test/mockarty-row18-deploy-b.git"

PASS=0
FAIL=0
ok() {
  PASS=$((PASS + 1))
  printf 'ok %d - %s\n' "$PASS" "$1"
}
notok() {
  FAIL=$((FAIL + 1))
  printf 'not ok %d - %s\n' "$FAIL" "$1"
  [ -n "${2:-}" ] && printf '#   %s\n' "$2"
}
assert_eq() { # name actual expected
  if [ "$2" = "$3" ]; then ok "$1"; else notok "$1" "expected [$3] got [$2]"; fi
}
assert_contains() { # name haystack needle
  case "$2" in
    *"$3"*) ok "$1" ;;
    *) notok "$1" "expected output to contain [$3], got [$2]" ;;
  esac
}

cleanup() {
  if [ -n "${FIXTURE_PID:-}" ]; then
    kill "$FIXTURE_PID" 2>/dev/null
    wait "$FIXTURE_PID" 2>/dev/null
  fi
  rm -rf "$ROOT"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Acceptance surface (a1) fixture: 200 on /ok, 503 on /unavailable, plus a
# closed loopback port for the unreachable case.
# ---------------------------------------------------------------------------
free_port() {
  python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
}

HTTP_OK=""
HTTP_FAIL=""
HTTP_DEAD=""
LOCAL_OK=""
LOCAL_FAIL=""

start_http_fixture() {
  local port_file="$ROOT/http-port" i
  python3 "$HERE/http-fixture-server.py" "$port_file" >"$ROOT/http-fixture.log" 2>&1 &
  FIXTURE_PID=$!
  disown "$FIXTURE_PID" 2>/dev/null || true
  for i in $(seq 1 100); do
    [ -s "$port_file" ] && break
    sleep 0.05
  done
  [ -s "$port_file" ] || return 1
  local port
  port=$(cat "$port_file")
  LOCAL_OK="http://127.0.0.1:$port/ok"
  LOCAL_FAIL="http://127.0.0.1:$port/unavailable"
}

if [ -z "${ROW18_HTTP_OK_URL:-}" ] || [ -z "${ROW18_HTTP_FAIL_URL:-}" ] || [ -z "${ROW18_HTTP_DEAD_URL:-}" ]; then
  if ! start_http_fixture; then
    printf 'Bail out! cannot start the loopback acceptance-surface fixture\n'
    exit 1
  fi
fi
HTTP_OK=${ROW18_HTTP_OK_URL:-$LOCAL_OK}
HTTP_FAIL=${ROW18_HTTP_FAIL_URL:-$LOCAL_FAIL}
HTTP_DEAD=${ROW18_HTTP_DEAD_URL:-http://127.0.0.1:$(free_port)/}

# ---------------------------------------------------------------------------
# Git fixtures
# ---------------------------------------------------------------------------
new_fixture() {
  local name="$1" dir="$ROOT/$1"
  mkdir -p "$dir"
  "$REAL_GIT" init -q "$dir/seed" || return 1
  "$REAL_GIT" -C "$dir/seed" symbolic-ref HEAD refs/heads/main
  printf 'seed\n' > "$dir/seed/README.md"
  mkdir -p "$dir/seed/deploy"
  cat > "$dir/seed/deploy/app.yaml" <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: row18-svc-a
YAML
  "$REAL_GIT" -C "$dir/seed" add README.md deploy/app.yaml
  "$REAL_GIT" -C "$dir/seed" -c user.name=test -c user.email=test@example.test \
    commit -q -m seed
  "$REAL_GIT" init -q --bare "$dir/remote.git" || return 1
  "$REAL_GIT" -C "$dir/remote.git" symbolic-ref HEAD refs/heads/main
  "$REAL_GIT" -C "$dir/seed" remote add origin "$dir/remote.git"
  "$REAL_GIT" -C "$dir/seed" push -q origin main || return 1
  "$REAL_GIT" clone -q "$dir/remote.git" "$dir/work" || return 1
  "$REAL_GIT" -C "$dir/work" config user.name test
  "$REAL_GIT" -C "$dir/work" config user.email test@example.test
  "$REAL_GIT" -C "$dir/work" config "url.file://$dir/remote.git.insteadOf" "$FAKE_URL"
  "$REAL_GIT" -C "$dir/work" remote set-url origin "$FAKE_URL"
  printf '%s' "$dir"
}

BRANCH_NAME="feature/row18-svc-a-acceptance"

add_docs_commit() { # fixture-dir [marker-line]
  local dir="$1" marker="${2:-Marker: row #18}"
  "$REAL_GIT" -C "$dir/work" checkout -q -b "$BRANCH_NAME"
  mkdir -p "$dir/work/docs"
  printf '# Row #18 Deployment Documentation\n\nService: svc-a\nDelivery target: k8s-prod-a\n\n%s\n' \
    "$marker" > "$dir/work/docs/msdeploy.md"
  "$REAL_GIT" -C "$dir/work" add docs/msdeploy.md
  "$REAL_GIT" -C "$dir/work" commit -q -m 'docs: add row #18 msdeploy marker for svc-a'
}

push_branch() { # fixture-dir
  "$REAL_GIT" -C "$1/work" push -q -u origin "$BRANCH_NAME"
}

run_subject() { # fixture-dir extra-args...
  local dir="$1"
  shift
  ( cd "$dir/work" && bash "$SUBJECT" \
      --out "$dir/acceptance.json" --handoff "$dir/handoff.json" \
      --repo-url "$FAKE_URL" --http-timeout 3 "$@" ) > "$dir/subject.out" 2>&1
  printf '%s' "$?"
}

json_field() { # file python-expression against `rec`
  python3 - "$1" "$2" <<'PY'
import json, sys
rec = json.load(open(sys.argv[1]))
expr = sys.argv[2] if len(sys.argv) > 2 else ""
print(eval(expr, {"rec": rec}) if expr else "")
PY
}

# ---------------------------------------------------------------------------
# T1: main/master are refused and nothing is written
# ---------------------------------------------------------------------------
D=$(new_fixture main_branch)
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK")
assert_eq "T1 main branch is refused (exit 2)" "$RC" "2"
assert_contains "T1 refusal names the default-branch guard" "$(cat "$D/subject.out")" \
  "refusing to accept on the default branch 'main'"
if [ -e "$D/acceptance.json" ] || [ -e "$D/handoff.json" ]; then
  notok "T1 no records are written on a default-branch refusal"
else
  ok "T1 no records are written on a default-branch refusal"
fi

# ---------------------------------------------------------------------------
# T2: accepted change - record contents and the deploy-node handoff
# ---------------------------------------------------------------------------
D=$(new_fixture happy)
add_docs_commit "$D"
push_branch "$D"
LOCAL_SHA=$("$REAL_GIT" -C "$D/work" rev-parse HEAD)
REMOTE_SHA=$("$REAL_GIT" -C "$D/remote.git" rev-parse "refs/heads/$BRANCH_NAME")
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK")
assert_eq "T2 acceptance passes (exit 0)" "$RC" "0"
if [ -f "$D/acceptance.json" ]; then
  ok "T2 acceptance record exists"
  assert_eq "T2 a1_http_status" "$(json_field "$D/acceptance.json" 'rec["a1_http_status"]')" "200"
  assert_eq "T2 a2_file_exists" "$(json_field "$D/acceptance.json" 'rec["a2_file_exists"]')" "True"
  assert_eq "T2 a2_marker_present" "$(json_field "$D/acceptance.json" 'rec["a2_marker_present"]')" "True"
  assert_eq "T2 changed_files_only_docs" \
    "$(json_field "$D/acceptance.json" 'rec["changed_files_only_docs"]')" "True"
  assert_eq "T2 changed_files lists only the docs file" \
    "$(json_field "$D/acceptance.json" 'rec["changed_files"]')" "['docs/msdeploy.md']"
  assert_eq "T2 svc_b_untouched" "$(json_field "$D/acceptance.json" 'rec["svc_b_untouched"]')" "True"
  assert_eq "T2 accepted flag" "$(json_field "$D/acceptance.json" 'rec["accepted"]')" "True"
  assert_eq "T2 failed_checks is empty" "$(json_field "$D/acceptance.json" 'rec["failed_checks"]')" "[]"
  assert_eq "T2 record service" "$(json_field "$D/acceptance.json" 'rec["service"]')" "svc-a"
  assert_eq "T2 record target" "$(json_field "$D/acceptance.json" 'rec["target"]')" "k8s-prod-a"
  assert_eq "T2 record deploy_owner" \
    "$(json_field "$D/acceptance.json" 'rec["deploy_owner"]')" "deploy-node"
  assert_eq "T2 record branch" "$(json_field "$D/acceptance.json" 'rec["branch"]')" "$BRANCH_NAME"
  assert_eq "T2 record local_sha is HEAD" \
    "$(json_field "$D/acceptance.json" 'rec["local_sha"]')" "$LOCAL_SHA"
  assert_eq "T2 record remote_sha equals local_sha" \
    "$(json_field "$D/acceptance.json" 'rec["remote_sha"] == rec["local_sha"]')" "True"
  assert_eq "T2 record remote_sha is the origin ref" \
    "$(json_field "$D/acceptance.json" 'rec["remote_sha"]')" "$REMOTE_SHA"
else
  notok "T2 acceptance record exists" "$(cat "$D/subject.out")"
fi

if [ -f "$D/handoff.json" ]; then
  ok "T2 handoff record exists"
  assert_eq "T2 handoff exact_sha is the accepted SHA" \
    "$(json_field "$D/handoff.json" 'rec["exact_sha"]')" "$LOCAL_SHA"
  assert_eq "T2 handoff service" "$(json_field "$D/handoff.json" 'rec["service"]')" "svc-a"
  assert_eq "T2 handoff target" "$(json_field "$D/handoff.json" 'rec["target"]')" "k8s-prod-a"
  assert_eq "T2 handoff manifest_reference" \
    "$(json_field "$D/handoff.json" 'rec["manifest_reference"]')" "pre-existing"
  assert_eq "T2 handoff deploy_owner" \
    "$(json_field "$D/handoff.json" 'rec["deploy_owner"]')" "deploy-node"
  assert_eq "T2 handoff is pending, not deployed" \
    "$(json_field "$D/handoff.json" 'rec["deployment_status"]')" "pending-external-deploy-node"
  assert_eq "T2 handoff carries no fabricated health_status" \
    "$(json_field "$D/handoff.json" '"health_status" not in rec and "health_status" not in rec["expected_deployment_record"]')" \
    "True"
  assert_eq "T2 handoff binds the exact branch ref" \
    "$(json_field "$D/handoff.json" 'rec["exact_sha"] == rec["acceptance"]["remote_sha"]')" "True"
else
  notok "T2 handoff record exists" "$(cat "$D/subject.out")"
fi

# ---------------------------------------------------------------------------
# T3: missing row #18 marker line fails acceptance and writes no handoff
# ---------------------------------------------------------------------------
D=$(new_fixture marker_missing)
add_docs_commit "$D" "Marker: pending"
push_branch "$D"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK")
assert_eq "T3 missing marker line fails acceptance (exit 7)" "$RC" "7"
assert_eq "T3 the loose grep still matches the document title" \
  "$(json_field "$D/acceptance.json" 'rec["a2_marker_present"]')" "True"
assert_eq "T3 a2_marker_line_present is false" \
  "$(json_field "$D/acceptance.json" 'rec["a2_marker_line_present"]')" "False"
assert_eq "T3 a2_file_exists is still true" \
  "$(json_field "$D/acceptance.json" 'rec["a2_file_exists"]')" "True"
assert_contains "T3 failed_checks names the marker line" \
  "$(json_field "$D/acceptance.json" 'rec["failed_checks"]')" "a2_marker_line_present"
assert_contains "T3 output refuses to hand off" "$(cat "$D/subject.out")" \
  "no deploy-node handoff written"
if [ -e "$D/handoff.json" ]; then
  notok "T3 no handoff record when the marker line is missing"
else
  ok "T3 no handoff record when the marker line is missing"
fi

# ---------------------------------------------------------------------------
# T4: missing docs/msdeploy.md fails acceptance and writes no handoff
# ---------------------------------------------------------------------------
D=$(new_fixture file_missing)
"$REAL_GIT" -C "$D/work" checkout -q -b "$BRANCH_NAME"
printf 'docs only, wrong file\n' > "$D/work/docs-notes.md" 2>/dev/null || {
  mkdir -p "$D/work/docs"; printf 'wrong file\n' > "$D/work/docs/notes.md"
}
"$REAL_GIT" -C "$D/work" add -A
"$REAL_GIT" -C "$D/work" commit -q -m 'docs: other docs file'
push_branch "$D"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK")
assert_eq "T4 missing docs/msdeploy.md fails acceptance (exit 7)" "$RC" "7"
assert_eq "T4 a2_file_exists is false" \
  "$(json_field "$D/acceptance.json" 'rec["a2_file_exists"]')" "False"
assert_eq "T4 a2_marker_present is false" \
  "$(json_field "$D/acceptance.json" 'rec["a2_marker_present"]')" "False"
if [ -e "$D/handoff.json" ]; then
  notok "T4 no handoff record when docs/msdeploy.md is missing"
else
  ok "T4 no handoff record when docs/msdeploy.md is missing"
fi

# ---------------------------------------------------------------------------
# T5: a non-docs change fails the docs-only acceptance check
# ---------------------------------------------------------------------------
D=$(new_fixture non_docs)
add_docs_commit "$D"
printf 'changed\n' >> "$D/work/README.md"
"$REAL_GIT" -C "$D/work" add README.md
"$REAL_GIT" -C "$D/work" commit -q -m 'chore: touch the readme'
push_branch "$D"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK")
assert_eq "T5 non-docs change fails acceptance (exit 7)" "$RC" "7"
assert_eq "T5 changed_files_only_docs is false" \
  "$(json_field "$D/acceptance.json" 'rec["changed_files_only_docs"]')" "False"
assert_contains "T5 failed_checks names the docs-only rule" \
  "$(json_field "$D/acceptance.json" 'rec["failed_checks"]')" "changed_files_only_docs"
assert_eq "T5 the non-docs file is reported" \
  "$(json_field "$D/acceptance.json" 'rec["non_doc_files"]')" "['README.md']"
if [ -e "$D/handoff.json" ]; then
  notok "T5 no handoff record for a non-docs change"
else
  ok "T5 no handoff record for a non-docs change"
fi

# ---------------------------------------------------------------------------
# T6: modifying the pre-existing Kubernetes manifest fails acceptance
# ---------------------------------------------------------------------------
D=$(new_fixture manifest_changed)
add_docs_commit "$D"
printf '# drift\nreplicas: 3\n' >> "$D/work/deploy/app.yaml"
"$REAL_GIT" -C "$D/work" add deploy/app.yaml
"$REAL_GIT" -C "$D/work" commit -q -m 'chore: drift the manifest'
push_branch "$D"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK")
assert_eq "T6 manifest change fails acceptance (exit 7)" "$RC" "7"
assert_contains "T6 failed_checks names the manifest rule" \
  "$(json_field "$D/acceptance.json" 'rec["failed_checks"]')" "no_manifest_changed"
assert_eq "T6 the manifest path is reported" \
  "$(json_field "$D/acceptance.json" 'rec["manifest_files"]')" "['deploy/app.yaml']"
if [ -e "$D/handoff.json" ]; then
  notok "T6 no handoff record when a manifest changed"
else
  ok "T6 no handoff record when a manifest changed"
fi

# ---------------------------------------------------------------------------
# T7: a non-200 acceptance surface fails a1 and forbids the handoff
# ---------------------------------------------------------------------------
D=$(new_fixture http_not_200)
add_docs_commit "$D"
push_branch "$D"
RC=$(run_subject "$D" --acceptance-url "$HTTP_FAIL")
assert_eq "T7 non-200 surface fails acceptance (exit 7)" "$RC" "7"
assert_eq "T7 a1_http_status records the real status" \
  "$(json_field "$D/acceptance.json" 'rec["a1_http_status"]')" "503"
assert_contains "T7 failed_checks names a1" \
  "$(json_field "$D/acceptance.json" 'rec["failed_checks"]')" "a1_http_200"
if [ -e "$D/handoff.json" ]; then
  notok "T7 no handoff record when a1 fails"
else
  ok "T7 no handoff record when a1 fails"
fi

# ---------------------------------------------------------------------------
# T8: an unreachable acceptance surface fails a1 (status 0, no deployment claim)
# ---------------------------------------------------------------------------
D=$(new_fixture http_unreachable)
add_docs_commit "$D"
push_branch "$D"
RC=$(run_subject "$D" --acceptance-url "$HTTP_DEAD")
assert_eq "T8 unreachable surface fails acceptance (exit 7)" "$RC" "7"
assert_eq "T8 unreachable surface records status 0" \
  "$(json_field "$D/acceptance.json" 'rec["a1_http_status"]')" "0"
assert_contains "T8 output does not claim a deployment" \
  "$(cat "$D/subject.out")" "no deployment claimed"
if [ -e "$D/handoff.json" ]; then
  notok "T8 no handoff record when the surface is unreachable"
else
  ok "T8 no handoff record when the surface is unreachable"
fi

# ---------------------------------------------------------------------------
# T9: a svc-b remote is out of scope and fails acceptance
# ---------------------------------------------------------------------------
D=$(new_fixture svcb_remote)
add_docs_commit "$D"
push_branch "$D"
"$REAL_GIT" -C "$D/work" remote add origin-b "$SVCB_URL"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK")
assert_eq "T9 svc-b remote fails acceptance (exit 7)" "$RC" "7"
assert_eq "T9 svc_b_untouched is false" \
  "$(json_field "$D/acceptance.json" 'rec["svc_b_untouched"]')" "False"
assert_contains "T9 the svc-b remote is reported" \
  "$(json_field "$D/acceptance.json" 'rec["foreign_remotes"]')" "$SVCB_URL"
if [ -e "$D/handoff.json" ]; then
  notok "T9 no handoff record with a svc-b remote"
else
  ok "T9 no handoff record with a svc-b remote"
fi

# ---------------------------------------------------------------------------
# T10: a remote ref that does not equal local HEAD fails acceptance
# ---------------------------------------------------------------------------
D=$(new_fixture sha_mismatch)
add_docs_commit "$D"
push_branch "$D"
OTHER_SHA=$("$REAL_GIT" -C "$D/seed" rev-parse HEAD)
"$REAL_GIT" -C "$D/remote.git" update-ref "refs/heads/$BRANCH_NAME" "$OTHER_SHA"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK")
assert_eq "T10 remote SHA mismatch fails acceptance (exit 7)" "$RC" "7"
assert_eq "T10 local_sha still equals HEAD" \
  "$(json_field "$D/acceptance.json" 'rec["local_sha"]')" "$("$REAL_GIT" -C "$D/work" rev-parse HEAD)"
assert_eq "T10 remote_sha is the divergent origin SHA" \
  "$(json_field "$D/acceptance.json" 'rec["remote_sha"]')" "$OTHER_SHA"
assert_contains "T10 failed_checks names the SHA match" \
  "$(json_field "$D/acceptance.json" 'rec["failed_checks"]')" "remote_sha_matches_local"
if [ -e "$D/handoff.json" ]; then
  notok "T10 no handoff record on SHA mismatch"
else
  ok "T10 no handoff record on SHA mismatch"
fi

# ---------------------------------------------------------------------------
# T11: the deploy node's own record is validated, never fabricated
# ---------------------------------------------------------------------------
D=$(new_fixture deploy_record)
add_docs_commit "$D"
push_branch "$D"
LOCAL_SHA=$("$REAL_GIT" -C "$D/work" rev-parse HEAD)

write_deploy_record() { # dir exact_sha owner health [omit_health]
  local dir="$1" sha="$2" owner="$3" health="$4" omit="${5:-}"
  {
    printf '{\n'
    printf '  "service": "svc-a",\n'
    printf '  "target": "k8s-prod-a",\n'
    printf '  "exact_sha": "%s",\n' "$sha"
    printf '  "manifest_reference": "pre-existing",\n'
    [ -n "$omit" ] || printf '  "health_status": "%s",\n' "$health"
    printf '  "deployed_by": "%s"\n' "$owner"
    printf '}\n'
  } > "$dir/deploy-record.json"
}

write_deploy_record "$D" "$LOCAL_SHA" "deploy-node" "healthy"
BEFORE=$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$D/deploy-record.json")
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK" --deploy-record "$D/deploy-record.json")
assert_eq "T11a healthy deploy record is verified (exit 0)" "$RC" "0"
assert_contains "T11a the reported health status is echoed" \
  "$(cat "$D/subject.out")" "deploy record verified: service=svc-a target=k8s-prod-a exact_sha=$LOCAL_SHA health_status=healthy deployed_by=deploy-node"
AFTER=$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$D/deploy-record.json")
assert_eq "T11a this run does not modify the deploy record" "$AFTER" "$BEFORE"

write_deploy_record "$D" "$LOCAL_SHA" "deploy-node" "unhealthy"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK" --deploy-record "$D/deploy-record.json")
assert_eq "T11b a non-healthy status is reported, not fabricated (exit 9)" "$RC" "9"
assert_contains "T11b the failure quotes the reported status" \
  "$(cat "$D/subject.out")" "health_status='unhealthy'"

write_deploy_record "$D" "$("$REAL_GIT" -C "$D/seed" rev-parse HEAD)" "deploy-node" "healthy"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK" --deploy-record "$D/deploy-record.json")
assert_eq "T11c a wrong exact_sha is rejected (exit 8)" "$RC" "8"
assert_contains "T11c the mismatch names exact_sha" \
  "$(cat "$D/subject.out")" "exact_sha"

write_deploy_record "$D" "$LOCAL_SHA" "someone-else" "healthy"
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK" --deploy-record "$D/deploy-record.json")
assert_eq "T11d a foreign deployed_by is rejected (exit 8)" "$RC" "8"
assert_contains "T11d the mismatch names deployed_by" \
  "$(cat "$D/subject.out")" "deployed_by"

write_deploy_record "$D" "$LOCAL_SHA" "deploy-node" "healthy" omit
RC=$(run_subject "$D" --acceptance-url "$HTTP_OK" --deploy-record "$D/deploy-record.json")
assert_eq "T11e a missing health_status is a contract violation (exit 8)" "$RC" "8"
assert_contains "T11e the problem names health_status" \
  "$(cat "$D/subject.out")" "missing health_status"

RC=$(run_subject "$D" --acceptance-url "$HTTP_OK" --deploy-record "$D/no-such-record.json")
assert_eq "T11f a missing deploy record is not invented (exit 8)" "$RC" "8"
assert_contains "T11f the problem names the missing file" \
  "$(cat "$D/subject.out")" "deploy record not found"

# ---------------------------------------------------------------------------
# T12: usage errors are rejected before any check runs
# ---------------------------------------------------------------------------
D=$(new_fixture usage)
RC=$(run_subject "$D" --nonsense)
assert_eq "T12 an unknown argument is a usage error (exit 6)" "$RC" "6"

printf '\n1..%s\n' "$((PASS + FAIL))"
printf '# pass %s\n# fail %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
