#!/usr/bin/env bash
#
# Hermetic tests for docs/deploy-handoff/push-and-verify.sh.
#
# Every fixture is a throwaway bare repository plus a work clone whose origin is
# a fake HTTPS URL rewritten to the local bare repo through git's url.insteadOf,
# so the script's HTTPS precondition is exercised without touching the real
# GitHub origin. No network, no credentials, no deployment.
#
# Run: bash docs/deploy-handoff/test_push_and_verify.sh

set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SUBJECT="$HERE/push-and-verify.sh"
REAL_GIT=$(command -v git)
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/svc-a-handoff.XXXXXX")
FAKE_URL="https://git.example.test/svc-a.git"

cleanup() { rm -rf "$ROOT"; }
trap cleanup EXIT

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

new_fixture() {
  local name="$1" dir="$ROOT/$1"
  mkdir -p "$dir"
  "$REAL_GIT" init -q "$dir/seed" || return 1
  "$REAL_GIT" -C "$dir/seed" symbolic-ref HEAD refs/heads/main
  printf 'seed\n' > "$dir/seed/README.md"
  "$REAL_GIT" -C "$dir/seed" add README.md
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

add_docs_commit() { # fixture-dir branch
  local dir="$1" branch="$2"
  "$REAL_GIT" -C "$dir/work" checkout -q -b "$branch"
  mkdir -p "$dir/work/docs"
  printf '# Row #18 Deployment Documentation\n\nService: svc-a\nDelivery target: k8s-prod-a\n\nMarker: row #18\n' \
    > "$dir/work/docs/msdeploy.md"
  "$REAL_GIT" -C "$dir/work" add docs/msdeploy.md
  "$REAL_GIT" -C "$dir/work" commit -q -m 'docs: add row #18 msdeploy marker for svc-a'
}

run_subject() { # fixture-dir out-path
  local dir="$1" out="$2"
  shift 2
  ( cd "$dir/work" && bash "$SUBJECT" --out "$out" --repo-url "$FAKE_URL" "$@" ) \
    > "$dir/subject.out" 2>&1
  printf '%s' "$?"
}

json_field() { # file python-expression against `rec`
  python3 - "$1" "$2" <<'PY'
import json, sys
rec = json.load(open(sys.argv[1]))
print(sys.argv[2] and eval(sys.argv[2], {"rec": rec}) or "")
PY
}

# ---------------------------------------------------------------------------
# T1: main/master are refused before any push
# ---------------------------------------------------------------------------
D=$(new_fixture main_branch)
RC=$(run_subject "$D" "$D/handoff.json")
assert_eq "T1 main branch is refused (exit 2)" "$RC" "2"
assert_contains "T1 refusal names the default-branch guard" "$(cat "$D/subject.out")" \
  "refusing to push the default branch 'main'"
if [ -e "$D/handoff.json" ]; then
  notok "T1 no handoff record is written on refusal"
else
  ok "T1 no handoff record is written on refusal"
fi
assert_eq "T1 the default branch SHA did not move" \
  "$("$REAL_GIT" -C "$D/remote.git" rev-parse refs/heads/main)" \
  "$("$REAL_GIT" -C "$D/seed" rev-parse HEAD)"

# ---------------------------------------------------------------------------
# T2: happy path - push, SHA equality, handoff record contents
# ---------------------------------------------------------------------------
D=$(new_fixture happy)
add_docs_commit "$D" "feature/row18-svc-a-msdeploy"
LOCAL_SHA=$("$REAL_GIT" -C "$D/work" rev-parse HEAD)
RC=$(run_subject "$D" "$D/handoff.json")
assert_eq "T2 push + verification succeeds (exit 0)" "$RC" "0"
assert_eq "T2 remote feature ref equals local HEAD" \
  "$("$REAL_GIT" -C "$D/remote.git" rev-parse refs/heads/feature/row18-svc-a-msdeploy)" \
  "$LOCAL_SHA"
if [ -f "$D/handoff.json" ]; then
  ok "T2 handoff record exists"
  assert_eq "T2 record service" "$(json_field "$D/handoff.json" 'rec["service"]')" "svc-a"
  assert_eq "T2 record target" "$(json_field "$D/handoff.json" 'rec["target"]')" "k8s-prod-a"
  assert_eq "T2 record exact_sha is local HEAD" \
    "$(json_field "$D/handoff.json" 'rec["exact_sha"]')" "$LOCAL_SHA"
  assert_eq "T2 record manifest_reference" \
    "$(json_field "$D/handoff.json" 'rec["manifest_reference"]')" "pre-existing"
  assert_eq "T2 record repository" \
    "$(json_field "$D/handoff.json" 'rec["repository"]')" "$FAKE_URL"
  assert_eq "T2 record branch" \
    "$(json_field "$D/handoff.json" 'rec["branch"]')" "feature/row18-svc-a-msdeploy"
  assert_eq "T2 record ref" \
    "$(json_field "$D/handoff.json" 'rec["verification"]["ref"]')" \
    "refs/heads/feature/row18-svc-a-msdeploy"
  assert_eq "T2 record local/remote SHA match flag" \
    "$(json_field "$D/handoff.json" 'str(rec["verification"]["sha_match"]).lower()')" "true"
  assert_eq "T2 record local and remote SHA are equal" \
    "$(json_field "$D/handoff.json" 'rec["verification"]["local_sha"] == rec["verification"]["remote_sha"]')" \
    "True"
else
  notok "T2 handoff record exists" "$(cat "$D/subject.out")"
fi

# Re-running on an already-pushed identical branch is idempotent (no force push).
RC=$(run_subject "$D" "$D/handoff.json")
assert_eq "T2 re-run on an identical remote branch succeeds (exit 0)" "$RC" "0"
assert_contains "T2 re-run reports the branch was already pushed" \
  "$(cat "$D/subject.out")" "branch already pushed"

# ---------------------------------------------------------------------------
# T3: an existing remote branch at a different SHA fails, and is not force-pushed
# ---------------------------------------------------------------------------
D=$(new_fixture diverged)
add_docs_commit "$D" "feature/row18-svc-a-msdeploy"
"$REAL_GIT" -C "$D/work" push -q -u origin feature/row18-svc-a-msdeploy
OLD_REMOTE_SHA=$("$REAL_GIT" -C "$D/remote.git" rev-parse refs/heads/feature/row18-svc-a-msdeploy)
printf 'changed\n' >> "$D/work/docs/msdeploy.md"
"$REAL_GIT" -C "$D/work" add docs/msdeploy.md
"$REAL_GIT" -C "$D/work" commit -q --amend --no-edit
NEW_LOCAL_SHA=$("$REAL_GIT" -C "$D/work" rev-parse HEAD)
[ "$NEW_LOCAL_SHA" != "$OLD_REMOTE_SHA" ] || notok "T3 fixture amend changed the SHA"
RC=$(run_subject "$D" "$D/handoff.json")
assert_eq "T3 diverged remote branch is refused (exit 5)" "$RC" "5"
assert_contains "T3 refusal names the force-push guard" \
  "$(cat "$D/subject.out")" "refusing to force-push"
assert_eq "T3 remote branch was not overwritten" \
  "$("$REAL_GIT" -C "$D/remote.git" rev-parse refs/heads/feature/row18-svc-a-msdeploy)" \
  "$OLD_REMOTE_SHA"
if [ -e "$D/handoff.json" ]; then
  notok "T3 no handoff record on divergence"
else
  ok "T3 no handoff record on divergence"
fi

# ---------------------------------------------------------------------------
# T4: multiple ls-remote lines are filtered to the exact refs/heads/<branch>
# ---------------------------------------------------------------------------
D=$(new_fixture many_refs)
add_docs_commit "$D" "feature/row18-svc-a-msdeploy"
LOCAL_SHA=$("$REAL_GIT" -C "$D/work" rev-parse HEAD)
for decoy in "feature/row18-svc-a-msdeploy-2" "feature/row18-svc-a-msdeploy-old"; do
  "$REAL_GIT" -C "$D/seed" push -q origin "refs/heads/main:refs/heads/$decoy"
done
RC=$(run_subject "$D" "$D/handoff.json")
assert_eq "T4 push + verification with decoy refs succeeds (exit 0)" "$RC" "0"
assert_eq "T4 record binds the exact ref" \
  "$(json_field "$D/handoff.json" 'rec["verification"]["ref"]')" \
  "refs/heads/feature/row18-svc-a-msdeploy"
assert_eq "T4 record exact_sha is local HEAD" \
  "$(json_field "$D/handoff.json" 'rec["exact_sha"]')" "$LOCAL_SHA"
assert_eq "T4 exact ref on the origin equals local HEAD" \
  "$("$REAL_GIT" -C "$D/remote.git" rev-parse refs/heads/feature/row18-svc-a-msdeploy)" \
  "$LOCAL_SHA"

# ---------------------------------------------------------------------------
# T5: a remote SHA that keeps differing after the push is re-queried once and fails
# ---------------------------------------------------------------------------
D=$(new_fixture mismatch)
add_docs_commit "$D" "feature/row18-svc-a-msdeploy"
OTHER_SHA=$("$REAL_GIT" -C "$D/seed" rev-parse HEAD)
mkdir -p "$D/bin"
cat > "$D/bin/git" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$D/git-shim.log"
"$REAL_GIT" "\$@"
rc=\$?
case " \$* " in
  *" push "*)
    if [ ! -f "$D/push-seen" ]; then
      : > "$D/push-seen"
      "$REAL_GIT" -C "$D/remote.git" update-ref refs/heads/feature/row18-svc-a-msdeploy "$OTHER_SHA"
    fi
    ;;
esac
exit \$rc
SHIM
chmod +x "$D/bin/git"
RC=$(cd "$D/work" && PATH="$D/bin:$PATH" bash "$SUBJECT" --out "$D/handoff.json" \
  --repo-url "$FAKE_URL" > "$D/subject.out" 2>&1; printf '%s' "$?")
assert_eq "T5 persistent SHA mismatch fails (exit 4)" "$RC" "4"
assert_contains "T5 failure reports the re-query" "$(cat "$D/subject.out")" "re-querying once"
LSREMOTE_CALLS=$(grep -c '^ls-remote' "$D/git-shim.log" || true)
if [ "${LSREMOTE_CALLS:-0}" -ge 2 ]; then
  ok "T5 remote SHA was re-queried (${LSREMOTE_CALLS} ls-remote calls)"
else
  notok "T5 remote SHA was re-queried" "ls-remote calls: ${LSREMOTE_CALLS:-0}"
fi
if [ -e "$D/handoff.json" ]; then
  notok "T5 no handoff record on mismatch"
else
  ok "T5 no handoff record on mismatch"
fi

# ---------------------------------------------------------------------------
# T6: a rejected push stops the run and writes no handoff record
# ---------------------------------------------------------------------------
D=$(new_fixture push_rejected)
add_docs_commit "$D" "feature/row18-svc-a-msdeploy"
mkdir -p "$D/bin"
cat > "$D/bin/git" <<SHIM
#!/usr/bin/env bash
case " \$* " in
  *" push "*) printf 'remote: authentication failed\n' >&2; exit 128 ;;
esac
exec "$REAL_GIT" "\$@"
SHIM
chmod +x "$D/bin/git"
RC=$(cd "$D/work" && PATH="$D/bin:$PATH" bash "$SUBJECT" --out "$D/handoff.json" \
  --repo-url "$FAKE_URL" > "$D/subject.out" 2>&1; printf '%s' "$?")
assert_eq "T6 rejected push stops the run (exit 3)" "$RC" "3"
assert_contains "T6 failure names the push" "$(cat "$D/subject.out")" "push of"
if [ -e "$D/handoff.json" ]; then
  notok "T6 no handoff record when the push is rejected"
else
  ok "T6 no handoff record when the push is rejected"
fi
assert_eq "T6 nothing reached the origin" \
  "$("$REAL_GIT" -C "$D/remote.git" rev-parse --verify -q refs/heads/feature/row18-svc-a-msdeploy >/dev/null && echo present || echo absent)" \
  "absent"

# ---------------------------------------------------------------------------
# T7: a non-HTTPS origin or a foreign repository URL is refused before pushing
# ---------------------------------------------------------------------------
D=$(new_fixture repo_guard)
add_docs_commit "$D" "feature/row18-svc-a-msdeploy"
"$REAL_GIT" -C "$D/work" config remote.origin.url "http://git.example.test/svc-a.git"
RC=$(run_subject "$D" "$D/handoff.json")
assert_eq "T7a non-HTTPS origin is refused (exit 5)" "$RC" "5"
assert_contains "T7a refusal names the HTTPS requirement" "$(cat "$D/subject.out")" "not HTTPS"
assert_eq "T7a non-HTTPS origin never receives the branch" \
  "$("$REAL_GIT" -C "$D/remote.git" rev-parse --verify -q refs/heads/feature/row18-svc-a-msdeploy >/dev/null && echo present || echo absent)" \
  "absent"

D=$(new_fixture foreign_repo)
add_docs_commit "$D" "feature/row18-svc-a-msdeploy"
RC=$(cd "$D/work" && bash "$SUBJECT" --out "$D/handoff.json" \
  --repo-url "https://git.example.test/other-service.git" > "$D/subject.out" 2>&1; printf '%s' "$?")
assert_eq "T7b foreign repository URL is refused (exit 5)" "$RC" "5"
assert_contains "T7b refusal names the expected repository" "$(cat "$D/subject.out")" \
  "expected https://git.example.test/other-service.git"
assert_eq "T7b foreign origin never receives the branch" \
  "$("$REAL_GIT" -C "$D/remote.git" rev-parse --verify -q refs/heads/feature/row18-svc-a-msdeploy >/dev/null && echo present || echo absent)" \
  "absent"

printf '\n1..%s\n' "$((PASS + FAIL))"
printf '# pass %s\n# fail %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
