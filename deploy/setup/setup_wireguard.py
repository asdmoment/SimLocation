"""Create a new, isolated phone-to-server WireGuard interface; never print keys."""
import ipaddress,json,os,subprocess
from pathlib import Path

def run(*args,**kwargs):
    return subprocess.run(args,check=True,text=True,capture_output=True,**kwargs).stdout.strip()

def private_write(path,text):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:f.write(text)

interface='wg-simloc'
server_path=Path('/etc/wireguard')/(interface+'.conf')
phone_path=Path('/etc/simlocation/wireguard-phone.conf')
if server_path.exists() or phone_path.exists():raise SystemExit('Existing SimLocation WireGuard files found; no changes made')
if run('ss','-Hlun','sport = :51820'):raise SystemExit('UDP port 51820 is in use; no changes made')
routes=json.loads(run('ip','-j','-4','route','show','table','all'))
networks=[ipaddress.ip_network(r['dst'],strict=False) for r in routes if r.get('dst') and r['dst']!='default']
subnet=next((ipaddress.ip_network(n) for n in ('10.0.0.1/30','10.0.0.2/30','10.0.0.3/30') if not any(ipaddress.ip_network(n).overlaps(r) for r in networks)),None)
if subnet is None:raise SystemExit('Candidate subnets overlap existing routes; no changes made')
server,phone=list(subnet.hosts())
server_private=run('wg','genkey');phone_private=run('wg','genkey')
server_public=run('wg','pubkey',input=server_private+'\n');phone_public=run('wg','pubkey',input=phone_private+'\n')
for directory in (server_path.parent,phone_path.parent):directory.mkdir(mode=0o700,parents=True,exist_ok=True)
created=[]
try:
    private_write(server_path,f'[Interface]\nAddress = {server}/30\nListenPort = 51820\nPrivateKey = {server_private}\nMTU = 1280\n\n[Peer]\nPublicKey = {phone_public}\nAllowedIPs = {phone}/32\n');created.append(server_path)
    private_write(phone_path,f'[Interface]\nPrivateKey = {phone_private}\nAddress = {phone}/32\nMTU = 1280\n\n[Peer]\nPublicKey = {server_public}\nEndpoint = {os.environ.get("SIMLOCATION_WG_ENDPOINT","SERVER_PUBLIC_IP:51820")}\nAllowedIPs = {server}/32\nPersistentKeepalive = 25\n');created.append(phone_path)
    run('systemctl','start','wg-quick@'+interface)
    print('SERVICE:',run('systemctl','is-active','wg-quick@'+interface))
    print('INTERFACE:',run('ip','-br','address','show',interface))
    print('PHONE:',phone,'SERVER:',server,'PORT: UDP 51820')
    print('ROUTES:',run('wg','show',interface,'allowed-ips'))
    print('No default routes, DNS settings, NAT, or existing services changed.')
    print('Autostart not enabled yet; await phone handshake verification.')
except BaseException:
    subprocess.run(['systemctl','stop','wg-quick@'+interface],capture_output=True)
    for path in created:path.unlink(missing_ok=True)
    raise
