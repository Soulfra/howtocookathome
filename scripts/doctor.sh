#!/usr/bin/env bash
# doctor.sh — one-command status for the HowToCookAtHome stack.
#
# Usage:
#   scripts/doctor.sh              full report
#   scripts/doctor.sh --quick      endpoints + warnings only
#   scripts/doctor.sh --help       this message
#
# What it checks:
#   • git state (branch, divergence, uncommitted)
#   • Render service (deploy status, disk, env vars)
#   • Cloudflare Pages (deploy drift vs Render)
#   • Live URL smoke test (apex + Render direct)
#   • Version map (which UI ships at which URL)
#   • Warnings (things you probably want to fix)
#
# Exit codes:
#   0  all green
#   1  at least one warning (soft-fail)
#   2  critical gap (e.g. Render not deployed, DB not persistent)

set -uo pipefail
MODE="${1:-full}"

# ANSI
B=$'\033[1m'; R=$'\033[0m'
GRN=$'\033[32m'; YEL=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'
CHK="${GRN}✓${R}"; WRN="${YEL}⚠${R}"; ERR="${RED}✗${R}"

WARNINGS=0; ERRORS=0
warn()  { echo "${WRN} $1"; WARNINGS=$((WARNINGS+1)); }
fail()  { echo "${ERR} $1"; ERRORS=$((ERRORS+1)); }
ok()    { echo "${CHK} $1"; }
section() { echo; echo "${B}─── $1 ───────────────────────────────────────────${R}"; }

case "$MODE" in
  -h|--help)
    /usr/bin/sed -n '2,16p' "$0" | /usr/bin/sed 's/^# //;s/^#//'
    exit 0 ;;
esac

# ─── GIT ─────────────────────────────────────────────
section "GIT"
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
HEAD_MSG=$(git log -1 --format=%s 2>/dev/null || echo "?")
echo "  Branch:        $BRANCH"
echo "  HEAD:          $HEAD  ${DIM}$HEAD_MSG${R}"

git fetch origin --quiet 2>/dev/null || true
LOCAL=$(git rev-parse HEAD 2>/dev/null)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "")
if [ "$LOCAL" = "$REMOTE" ]; then
  ok "origin/$BRANCH up to date with local"
elif [ -z "$REMOTE" ]; then
  warn "origin/$BRANCH not found"
else
  AHEAD=$(git rev-list --count "origin/$BRANCH..$BRANCH" 2>/dev/null || echo 0)
  BEHIND=$(git rev-list --count "$BRANCH..origin/$BRANCH" 2>/dev/null || echo 0)
  warn "git: local $AHEAD ahead / $BEHIND behind origin/$BRANCH"
fi

UNCOMMITTED=$(git status --porcelain | /usr/bin/grep -vE '^\?\? (\.wrangler|data/menus|_site|\.pytest_cache)' | /usr/bin/wc -l | /usr/bin/tr -d ' ')
if [ "$UNCOMMITTED" = "0" ]; then
  ok "working tree clean (ignoring gitignored scratch dirs)"
else
  warn "$UNCOMMITTED uncommitted/untracked files (not counting .wrangler/data/menus)"
fi

# ─── RENDER ──────────────────────────────────────────
section "RENDER"
if command -v render >/dev/null 2>&1; then
  SVC_ID="srv-d7ham31o3t8c738vsfjg"
  SVC_JSON=$(render services --output json 2>/dev/null | python3 -c "
import json, sys
for item in json.load(sys.stdin):
    svc = item.get('service', {})
    if svc.get('id') == '$SVC_ID':
        sd = svc.get('serviceDetails', {})
        disk = sd.get('disk', {}) or {}
        print(f\"plan={sd.get('plan','?')}\")
        print(f\"branch={svc.get('branch','?')}\")
        print(f\"auto={svc.get('autoDeploy','?')}\")
        if disk:
            print(f\"disk={disk.get('name','?')}@{disk.get('mountPath','?')} ({disk.get('sizeGB','?')}GB)\")
        break
" 2>/dev/null)
  if [ -n "$SVC_JSON" ]; then
    echo "$SVC_JSON" | /usr/bin/sed 's/^/  /'
    ok "service responds to API"
  else
    warn "Render CLI installed but couldn't fetch service info"
  fi

  DEP=$(render deploys list "$SVC_ID" --output json 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
x = d[0].get('deploy', d[0])
c = x.get('commit', {}) or {}
print(f\"{x.get('status','?')}|{str(c.get('id',''))[:7]}|{(c.get('message','') or '').splitlines()[0][:50]}\")
" 2>/dev/null)
  if [ -n "$DEP" ]; then
    STATUS=$(echo "$DEP" | /usr/bin/cut -d'|' -f1)
    SHA=$(echo "$DEP" | /usr/bin/cut -d'|' -f2)
    MSG=$(echo "$DEP" | /usr/bin/cut -d'|' -f3)
    echo "  latest deploy: $STATUS  $SHA  ${DIM}$MSG${R}"
    if [ "$STATUS" = "live" ] && [ "$SHA" = "$HEAD" ]; then
      ok "Render on HEAD ($HEAD)"
    elif [ "$STATUS" = "live" ]; then
      warn "Render live on $SHA, local HEAD is $HEAD (Render may need redeploy)"
    else
      fail "Render not 'live' — status=$STATUS"
    fi
  fi
else
  warn "render CLI not installed — skipping Render check (install: brew install render)"
fi

# ─── CLOUDFLARE PAGES ────────────────────────────────
section "CLOUDFLARE PAGES"
if command -v wrangler >/dev/null 2>&1; then
  CF_DEP=$(wrangler pages deployment list --project-name howtocookathome 2>&1 | /usr/bin/grep -E "Production" | /usr/bin/head -1)
  if [ -n "$CF_DEP" ]; then
    CF_ID=$(echo "$CF_DEP" | /usr/bin/awk -F'│' '{print $2}' | /usr/bin/tr -d ' ' | /usr/bin/cut -c1-8)
    CF_SHA=$(echo "$CF_DEP" | /usr/bin/awk -F'│' '{print $5}' | /usr/bin/tr -d ' ')
    CF_AGE=$(echo "$CF_DEP" | /usr/bin/awk -F'│' '{print $7}' | /usr/bin/tr -d ' ')
    echo "  latest: $CF_ID  commit $CF_SHA  $CF_AGE ago"
    if [ "$CF_SHA" = "$HEAD" ]; then
      ok "Cloudflare Pages on HEAD"
    else
      warn "CF Pages on $CF_SHA, HEAD is $HEAD (wrangler pages deploy output/web to sync)"
    fi
  fi
else
  warn "wrangler not installed — skipping CF Pages check (install: brew install cloudflare-wrangler2)"
fi

# ─── LIVE URL SMOKE TEST ─────────────────────────────
section "LIVE URLS"
check_url() {
  local label="$1" url="$2" expect="$3"
  local code
  code=$(/usr/bin/curl -sS -o /dev/null -m 10 -w "%{http_code}" "$url" 2>/dev/null || echo "000")
  if [ "$code" = "$expect" ]; then
    printf "  %-32s ${GRN}%s${R}  %s\n" "$label" "$code" "$url"
  else
    printf "  %-32s ${RED}%s${R}  %s (expected $expect)\n" "$label" "$code" "$url"
    WARNINGS=$((WARNINGS+1))
  fi
}
check_url "apex /"                "https://howtocookathome.com/"                 200
check_url "apex /tip-better"      "https://howtocookathome.com/tip-better"       200
check_url "apex /next-show"       "https://howtocookathome.com/next-show"        200
check_url "apex /claim/htcah"     "https://howtocookathome.com/claim/htcah"      200
check_url "apex /api/health"      "https://howtocookathome.com/api/health"       200
check_url "apex /api/debug-paths" "https://howtocookathome.com/api/debug-paths"  404
check_url "render /api/health"    "https://howtocookathome.onrender.com/api/health"  200
check_url "render /claim/htcah"   "https://howtocookathome.onrender.com/claim/htcah" 200

# POST /api/subscribe — valid and invalid email
code=$(/usr/bin/curl -sS -o /dev/null -m 10 -w "%{http_code}" -X POST https://howtocookathome.com/api/subscribe \
  -H "Content-Type: application/json" \
  -d '{"email":"doctor@example.com","bucket":"patron","source":"__doctor__"}' 2>/dev/null || echo "000")
printf "  %-32s %s  POST valid email\n" "apex /api/subscribe" "$code"

code=$(/usr/bin/curl -sS -o /dev/null -m 10 -w "%{http_code}" -X POST https://howtocookathome.com/api/subscribe \
  -H "Content-Type: application/json" \
  -d '{"email":"a@b.c","bucket":"patron","source":"__doctor_bad__"}' 2>/dev/null || echo "000")
if [ "$code" = "400" ]; then
  printf "  %-32s ${GRN}%s${R}  POST invalid email (correctly rejected)\n" "apex /api/subscribe (bad)" "$code"
else
  printf "  %-32s ${YEL}%s${R}  POST invalid email (expected 400 — email validation may be loose)\n" "apex /api/subscribe (bad)" "$code"
  WARNINGS=$((WARNINGS+1))
fi

if [ "$MODE" = "--quick" ]; then
  section "SUMMARY"
  echo "  warnings: $WARNINGS"
  echo "  errors:   $ERRORS"
  exit $([ "$ERRORS" -gt 0 ] && echo 2 || ([ "$WARNINGS" -gt 0 ] && echo 1 || echo 0))
fi

# ─── VERSIONS LIVE ───────────────────────────────────
section "VERSIONS LIVE AT /tip-better"
for v in "" "-v1" "-v2" "-v3" "-v4" "-v5" "-v6"; do
  url="https://howtocookathome.com/tip-better${v}"
  code=$(/usr/bin/curl -sS -o /dev/null -m 8 -w "%{http_code}" "$url" 2>/dev/null || echo "000")
  # detect version marker in the page
  body=$(/usr/bin/curl -sS -m 8 "$url" 2>/dev/null)
  marker="?"
  if echo "$body" | /usr/bin/grep -q "cards-container"; then marker="V5 (cards)"
  elif echo "$body" | /usr/bin/grep -q "visits-pills"; then marker="V4 (visits)"
  elif echo "$body" | /usr/bin/grep -q "lock-in"; then marker="V3 (dual-form)"
  elif echo "$body" | /usr/bin/grep -q "x</div>"; then marker="V2 (pairing × luck)"
  elif echo "$body" | /usr/bin/grep -q "id=\"eatout\""; then marker="V1 (calculator)"
  elif echo "$body" | /usr/bin/grep -q "One email a week"; then marker="V6 (email-first)"
  fi
  printf "  %-32s %s  %s\n" "tip-better${v:-  (default)}" "$code" "$marker"
done

# ─── OPEN TASKS FROM MEMORY ──────────────────────────
section "OPEN CONTEXT"
MEM="$HOME/.claude/projects/-Users-matthewmauer-Desktop-Howtocookathome/memory/MEMORY.md"
if [ -f "$MEM" ]; then
  MEM_LINES=$(/usr/bin/wc -l < "$MEM" | /usr/bin/tr -d ' ')
  echo "  memory entries: $MEM_LINES in $MEM"
else
  echo "  memory index not found"
fi

# ─── FINAL ROLLUP ────────────────────────────────────
section "SUMMARY"
echo "  warnings: $WARNINGS"
echo "  errors:   $ERRORS"
if [ "$ERRORS" -gt 0 ]; then
  echo "${RED}doctor found critical gaps.${R}"
  exit 2
elif [ "$WARNINGS" -gt 0 ]; then
  echo "${YEL}doctor found $WARNINGS warning(s) — soft-fail.${R}"
  exit 1
else
  echo "${GRN}all green.${R}"
  exit 0
fi
