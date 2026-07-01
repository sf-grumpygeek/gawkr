#!/usr/bin/env bash
# Pre-deploy connectivity check. Run this ON THE PORTAINER HOST before adding the
# stack — it confirms the host can reach Protect and both model servers.
# Override addresses via env vars, e.g.:
#   PROTECT=PROTECT_IP VISION=VISION_HOST:8081 ./scripts/check.sh
set -u

PROTECT="${PROTECT:-PROTECT_IP}"
VISION="${VISION:-VISION_HOST:8081}"
WHISPER="${WHISPER:-VISION_HOST:8082}"

probe() {
  local name="$1" url="$2" extra="${3:-}"
  local code
  code=$(curl $extra -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)
  if [ -z "$code" ] || [ "$code" = "000" ]; then
    printf '  %-8s UNREACHABLE   %s\n' "$name" "$url"
    return 1
  fi
  printf '  %-8s ok (http %s)  %s\n' "$name" "$code" "$url"
}

echo "gawkr pre-deploy check"
rc=0
probe protect "https://$PROTECT"       "-k" || rc=1
probe vision  "http://$VISION/v1/models"     || rc=1
probe whisper "http://$WHISPER/"             || rc=1
echo
if [ "$rc" -eq 0 ]; then
  echo "All reachable — safe to deploy the stack."
else
  echo "Something is unreachable. Fix routing/firewall before deploying."
  echo "Protect on 192.168.250.x is the usual culprit: the Portainer host needs"
  echo "a route / firewall-allow to reach it on 443 across the VLAN boundary."
fi
exit $rc
