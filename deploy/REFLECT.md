# Foreground reflect relay

> **Legacy since 2026-11-12.** The phones now simulate location on their own with the
> SimLocation swiplet in Swipt; see [`swiplet/README.md`](../swiplet/README.md). This relay is
> kept for reference and is no longer how the phones are used. Marking it legacy changed
> nothing on the hosts: a relay deployed there stays as it was until `fleet.py relay-stop`.

## What is connected

The existing CLI and password-protected web console remain the control surface.
`reflect_tunneld.py` replaces only the network backend for an explicitly enabled
session:

```
SimLocation web/CLI -> local tunneld API -> kernel simloc-rsd TUN
  -> paired TLS-PSK tunnel -> private Unix relay socket
  -> mutually authenticated TLS -> a-Shell -> Surge reflect -> iOS
```

The phone holds only the experiment's mTLS client credential. Device pairing
records stay on the VPS. The relay permits the control port 49152 and at most one
negotiated high data port at a time, on the fixed address `10.7.0.1`. A new data
port can replace an old grant only after the previous data channels close.

## Current evidence and limits

- Wi-Fi: existing pairing identity, TLS-PSK tunnel, RSD, DVT and opening the
  location channel passed. Seven read-only queries on one DVT session succeeded
  over 60 seconds. No location was set or cleared in that test.
- Cellular: the outer mTLS transport connected, but the phone's new connection
  to `10.7.0.1:49152` was refused. This backend cannot make that listener appear.
- Switching network while a device channel stays open is distinct from starting
  another channel. Seamless handoff is not established; the outer transport is
  TCP and can break when the network changes.
- a-Shell must remain foreground. Use the console from a second device for now;
  opening Safari on this phone backgrounds a-Shell. Background/lock-screen
  reliability is not established.
- A connected outer relay is not a ready device. Only a successfully established
  device tunnel is published through the existing tunneld API.
- TLS credentials must be unexpired. These tools reuse an existing private
  certificate package; they do not silently renew credentials or change trust.

## Prepare the phone

For a code-only upgrade, replace just `reflect_relay.py` in the existing phone
folder. Keep all three certificate/key files and `phone.json` unchanged. Run:

```sh
python3 reflect_relay.py phone --config phone.json --reconnect --max-seconds 3600
```

This does not copy credentials or extend their expiry. A code-only update ZIP
should contain only the script and its README. Building the full private bundle
below copies the client private key and requires the owner's explicit approval.

From the repository on the Mac, build a new private bundle using the earlier
phone package as the credential source. Choose an unused output directory:

```sh
python3 deploy/prepare_reflect_phone.py \
  --source /ABS/PATH/TO/Reflect-Relay-Phone \
  --output /ABS/PATH/TO/Reflect-Relay-Phone-v2 \
  --seconds 3600
```

The output ZIP includes a private client key. Transfer it only to your phone,
unzip it, run `pickFolder` in a-Shell, and select the extracted folder. Do not
install its CA as an iOS certificate or as a Surge MITM CA.

Keep Surge running with the SimLocation Reflect module enabled. Its source is
`scripts/SimLocation-Reflect.sgmodule` in the Surge repo (`~/path/to/surge-repo`), `tools/push-scripts.sh`
publishes it on the tailnet, and the phone installs it from
`http://100.64.0.11:9101/script/SimLocation-Reflect.sgmodule`. It replaces the hand-made
"RemotePairing Reflect Test" module (2026-11-05), with one rule added that pins the
address to DIRECT:

```ini
[General]
tun-included-routes = %INSERT% 10.7.0.1/32

[Rule]
IP-CIDR,10.7.0.1/32,DIRECT,no-resolve

[IP Rewrite]
10.7.0.1 = reflect
```

A module lives outside the profile, so a whole-profile install does not bring it back;
after reinstalling Surge, install it again from that address.

Connect Wi-Fi, then run:

```sh
python3 reflect_relay.py phone --config phone.json
```

`RELAY_READY` reports outer transport only. The new package retries a broken
transport every five seconds within its configured session duration, and stops
on certificate verification failure. An old package can still connect, but does
not have the new reconnect and data-grant replacement behavior.

## Server activation and rollback

Install the repository's `reflect_relay.py`, `reflect_tunneld.py` and
`reflect_session.py` alongside the existing `network_tunneld.py` in
`/opt/simlocation/deploy`. Normal fleet bundles now include the backend, relay,
session manager and this guide. No credentials belong in the code bundle.

The session manager runs as root on the selected VPS. It uses existing private
`ca.pem`, `server.pem`, `server-key.pem` and `expiry.json` from the specified
credential directory. `expiry.json` contains `not_after_utc` in ISO-8601 form.

```sh
python3 /opt/simlocation/deploy/reflect_session.py enable \
  --credentials /var/lib/simloc-reflect-test \
  --public-ip PUBLIC_IPV4 \
  --seconds 3600

python3 /opt/simlocation/deploy/reflect_session.py status
python3 /opt/simlocation/deploy/reflect_session.py disable
```

Activation refuses live location-hold workers. It switches the existing network
unit to the relay backend using an owned drop-in, reusing port 49151, the
`simloc-rsd` interface and existing UID/firewall restrictions. It does not expose
tunneld or the web console on the public Internet. The console retains its
existing private WireGuard address, login and CSRF checks.

A second owned drop-in forces manual operation (`SIMLOCATION_WEB_AUTO_SET=0`)
for the session. This is deliberate: a transport reconnect must not count as a
new instruction to set the default coordinates. Restarting the web console may
require logging in again.

An independent expiry timer removes the relay's public TCP 8446 exception,
removes the session's drop-ins and restores the prior backend/service state.
`disable` is also the manual rollback. The expiry timer is transient and applies
to the current server boot: run `disable` before a planned reboot, or immediately
after an unexpected reboot, because the timer disappears but the drop-ins and UFW
rule persist. This is a bounded integration session, not a reboot-resilient daemon
installation. Neither rollback nor closing a-Shell is a location-clear command.
Clear location explicitly before ending normal use.

## End-to-end operation

1. Enable the server session, import the current phone bundle, and connect on
   Wi-Fi with a-Shell foreground.
2. Confirm `DEVICE_TUNNEL_READY` in `simlocation-network.service` logs and the
   device online in the existing console. The outer `RELAY_READY` alone does not
   satisfy this step.
3. Run the existing `deploy/verify_device.py` as the existing `simlocation`
   account with its normal HOME to verify RSD, DVT and the location channel
   without setting or clearing coordinates. No developer-image auto-mount is
   performed by that verifier.
4. In the console, choose the intended coordinate and explicitly set it. A
   server-side `ready` session indicates the command was applied; confirm the
   displayed position in a location-aware application on the phone separately.
5. Use the existing route controls if needed. Existing reconnect behavior still
   needs a usable device tunnel; pure cellular cannot rebuild it in the current
   test configuration.
6. Explicitly restore real location in the console, then stop the phone relay
   and disable the server session. A relay disconnect alone must not be shown as
   proof that simulated location has been cleared.

## Troubleshooting

| Signal | Meaning / next action |
| --- | --- |
| `RELAY_READY` | mTLS transport connected; wait for a device tunnel. |
| `phone_not_connected` | No authenticated phone session; start the phone relay. |
| `phone_port_refused` | The fixed reflected phone port refused TCP; restore Wi-Fi and check the module. |
| `phone_port_timeout` | Phone-side dial timed out; check phone network/foreground state. |
| `phone_dial_failed` | An older phone script reported failure without an error code. |
| `relay_closed_before_reply` | Local relay closed without a control response; inspect the relay log. |
| `existing_identity_rejected` | Existing pairing verification failed; this backend never auto-pairs. |
| `SESSION_LIMIT_REACHED` | The bounded phone session ended; explicitly start a new session. |

Diagnostic commands contain no credentials:

```sh
systemctl status simloc-reflect.service simlocation-network.service
journalctl -u simloc-reflect.service -u simlocation-network.service -n 60 --no-pager
python3 /opt/simlocation/deploy/reflect_session.py status
```

## Regression tests

```sh
python3 -m unittest discover -s deploy -p 'test_*.py' -v
```

The relay tests use local socket fixtures. Backend tests mock the paired device
and assert registration, cleanup, explicit error propagation and rejection of
new pairing. Session-manager tests mock systemd/firewall operations; they never
alter a real host.
