#!/bin/bash
# run.sh <expect: pass|fail> <label> [plusargs...]
#
# Shared by the hostio4 benches. VCS always exits 0, so the verdict is decided
# by whether $PASSMARK appears in the run log.
#
# A case declared `fail` MUST actually fail. If it passes, that is reported as
# an error in its own right -- a check that cannot fail proves nothing.
#
# Per-bench overrides (environment):
#   SIMV      path to the simulator binary   (default ./simv)
#   PASSMARK  string that means success      (default "LINK PASS")
#   FAILMARK  string that prefixes a reason  (default "LINK FAIL")
set -u

SIMV="${SIMV:-./simv}"
PASSMARK="${PASSMARK:-LINK PASS}"
FAILMARK="${FAILMARK:-LINK FAIL}"

expect="$1"; shift
label="$1";  shift
log="run_${label}.log"

timeout 900 "$SIMV" -l "$log" "$@" > /dev/null 2>&1
rc=$?

reason_of() { grep -oP "${FAILMARK}\s+\K.*" "$log" | head -1; }

if [ $rc -eq 124 ]; then
  # A wall-clock timeout still counts as a failure, but flag it: it means the
  # in-sim stall watchdog did not fire, which we would rather fix than rely on.
  printf '  %-24s %-8s (expected %s)  *** wall-clock timeout: in-sim watchdog never fired ***\n' \
         "$label" "TIMEOUT" "$expect"
  [ "$expect" = "fail" ] && exit 0 || exit 1
fi

if grep -q "$PASSMARK" "$log"; then verdict="pass"; else verdict="fail"; fi

if [ "$verdict" = "$expect" ]; then
  if [ "$expect" = "pass" ]; then
    t=$(grep -oP "${PASSMARK}.*?, \K[0-9]+" "$log" | head -1)
    printf '  %-24s PASS    (%s ps)\n' "$label" "${t:-?}"
  else
    printf '  %-24s fails as required  -- %s\n' "$label" "$(reason_of)"
  fi
  exit 0
fi

if [ "$expect" = "fail" ]; then
  printf '  %-24s *** PASSED BUT MUST FAIL -- the bench is blind here ***\n' "$label"
else
  printf '  %-24s *** FAIL ***  %s\n' "$label" "$(reason_of)"
  sed -n '/policy:\|clocks /,/PASS\|FAIL/p' "$log" | sed 's/^/      /'
fi
exit 1
