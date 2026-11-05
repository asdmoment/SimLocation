# Reflect integration handoff

## Current state

Local implementation is complete and tested. After renewed exact-command
approval, upload and activation succeeded at 2026-11-04T12:23:55Z. Relay, network
backend and Web are running. The expiry timer is scheduled for 13:23:55Z.
At 12:25Z, login page and tunneld returned HTTP 200, tunneld listed zero devices,
and the actual Web process environment contained SIMLOCATION_WEB_AUTO_SET=0.
The phone reconnected at 12:29:08Z, and the integrated backend published its tunnel
at 12:29:33Z. verify_device.py run as simlocation with its normal HOME completed
RSD_CONNECTED, DVT_CONNECTED, LOCATION_CHANNEL_OPEN and
READ_ONLY_VERIFICATION_COMPLETE. The real CLI device list/status recognized the
default iPhone and showed no active location session. CLI invocations require
SIMLOCATION_PMD3=/opt/simlocation-venv/bin/pymobiledevice3, as the Web unit uses.
A subsequent HTTP check confirmed login=200, unauthenticated /api/state=401 and
one tunnel still registered. No set or clear was sent. Next: user chooses a
coordinate or explicitly operates the authenticated console; verify actual
location-hold and clear behavior after that instruction. Do not stop the relay
or tunnel merely because the read-only check has completed.

The phone code-only update is at:
`/Users/user/vps-a/reflect-tunnel.XXXXXX/Reflect-Relay-Update.zip`

It contains only reflect_relay.py and README.txt and reuses existing TLS files.
The separately approved full package build also succeeded on the later retry:
`/Users/user/vps-a/reflect-tunnel.XXXXXX/Reflect-Relay-Phone-v2.zip`.
That full package contains the existing short-lived client private key. Keep it
private; no certificate was renewed and the original expiry is unchanged.

## Evidence

- Old isolated Wi-Fi probe: pairing, TLS-PSK, RSD and DVT passed, location channel
  opened without coordinates. Seven reads over 60 seconds completed successfully.
- Cellular outer mTLS connected, but PHONE_SOCKET_FAILED 49152
  ConnectionRefusedError occurred. No RSD/DVT on that attempt.
- New local deploy suite: 94 tests run, 93 passed, one Linux-only test skipped on
  macOS. Existing application suite: 168 passed.
- Local network_tunneld.py, simlocation.py and simlocation_web.py hashes match the
  deployed baseline on vps-a.
- Last server check: network/web active, no location state files. Activation
  additionally checks live --_hold-session processes twice and refuses to switch
  while any exists.
- Existing server/phone certificate expiry: 2026-11-04T15:58:08Z. A new one-hour
  session requires more than 65 minutes of remaining validity; recheck at execution.

## Deployment command already executed successfully

Do not repeat enable while this session is active. The approved command was:

```sh
scp -o BatchMode=yes -o ConnectTimeout=15 /Users/user/SimLocation/deploy/reflect_relay.py /Users/user/SimLocation/deploy/reflect_tunneld.py /Users/user/SimLocation/deploy/reflect_session.py /Users/user/SimLocation/deploy/REFLECT.md vps-a:/opt/simlocation/deploy/ && ssh -o BatchMode=yes -o ConnectTimeout=15 vps-a '/usr/bin/python3 -I /opt/simlocation/deploy/reflect_session.py enable --credentials /var/lib/simloc-reflect-test --public-ip 203.0.113.10 --seconds 3600'
```

Then inspect service logs, expiry timer, listener 8446, local API 49151 and the
existing login page at 10.77.77.21:8765. Do not disable auth or publish the console.
Ask the user to connect Wi-Fi and run the updated phone script:

```sh
python3 reflect_relay.py phone --config phone.json --reconnect --max-seconds 3600
```

When the new backend publishes the device, run the existing verify_device.py as
simlocation with its normal HOME. That script opens RSD/DVT/location channel but
sets/clears nothing. Confirm the console detects the device. To test real set/
clear, obtain the user's desired coordinate or have them explicitly operate the
existing authenticated console. Keep a-Shell foreground; operating the console
from a second device avoids backgrounding it.

## Operational boundaries

- reflect_session enable uses only owned drop-ins, disables auto-set through the
  final EnvironmentFile, stops Web before switching network and checks workers
  again. It does not alter pairing, original network.json, original units, or
  other proxy services.
- disable restores the prior backend/service states and removes its own UFW rule.
  It does not clear the phone's simulated location.
- Expiry is a transient timer for this boot. Before a planned reboot, disable;
  after an unexpected reboot, manually disable to remove surviving drop-ins/rule.
- Full cellular handoff/background survival and actual location changes remain
  unverified. Do not describe them as working.
- No git commits or pushes were performed.
