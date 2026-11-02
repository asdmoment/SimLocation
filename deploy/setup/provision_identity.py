import base64,json,plistlib,subprocess
from pathlib import Path
from pymobiledevice3.remote.tunnel_service import RemotePairingTunnelService
udid=json.loads(Path('/Users/user/SimLocation/var/devices.json').read_text())['aliases']['myphone']
record=plistlib.loads((Path.home()/'.pymobiledevice3'/f'remote_{udid}.plist').read_bytes())
record.setdefault('host_identifier',RemotePairingTunnelService(udid,'10.77.77.2',49152).identifier)
payload=json.dumps({'udid':udid,'record':base64.b64encode(plistlib.dumps(record,fmt=plistlib.FMT_BINARY)).decode()}).encode()
remote=r'''
import base64,json,os,pwd,secrets,subprocess,sys
from pathlib import Path
data=json.load(sys.stdin)
def private_write(path,content):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as f:f.write(content)
home=Path('/var/lib/simlocation-tunnel');home.mkdir(mode=0o700,exist_ok=True)
pair=home/'pairing';pair.mkdir(mode=0o700,exist_ok=True)
record=pair/'device.plist';private_write(record,base64.b64decode(data['record']))
config={'udid':data['udid'],'host':'10.77.77.2','port':49152,'pair_record':str(record)}
private_write('/etc/simlocation/network.json',json.dumps(config).encode())
try: user=pwd.getpwnam('simlocation')
except KeyError:
 subprocess.run(['useradd','--system','--home-dir','/var/lib/simlocation','--no-create-home','--shell','/usr/sbin/nologin','simlocation'],check=True)
 user=pwd.getpwnam('simlocation')
state=Path('/var/lib/simlocation');state.mkdir(mode=0o700,exist_ok=True);os.chown(state,user.pw_uid,user.pw_gid)
devices=state/'devices.json';private_write(devices,json.dumps({'aliases':{'myphone':data['udid']},'default':data['udid']}).encode());os.chown(devices,user.pw_uid,user.pw_gid)
token=secrets.token_urlsafe(32)
private_write('/etc/simlocation/web.env',('SIMLOCATION_MAP_TOKEN='+token+'\n').encode())
print('Private trust record installed; original host identity preserved')
print('Separate unprivileged web user and token configured; credential values suppressed')
'''
cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=12','-o','ControlMaster=no','-o','ControlPath=none','vps','python3 -c '+__import__('shlex').quote(remote)]
r=subprocess.run(cmd,input=payload,capture_output=True,timeout=30)
print(r.stdout.decode())
if r.returncode:print('Provisioning failed; exit code',r.returncode);raise SystemExit(1)
