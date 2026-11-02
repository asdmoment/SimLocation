import os, pathlib, shlex, subprocess
import qrcode
remote="from pathlib import Path; print(next(x.split('=',1)[1] for x in Path('/etc/simlocation/web.env').read_text().splitlines() if x.startswith('SIMLOCATION_MAP_TOKEN=')))"
r=subprocess.run(['ssh','-o','ConnectTimeout=12','-o','BatchMode=yes','-o','ControlMaster=no','-o','ControlPath=none','vps','python3 -c '+shlex.quote(remote)],capture_output=True,check=True,timeout=20)
token=r.stdout.decode().strip()
assert len(token)==43 and all(c.isalnum() or c in '_-' for c in token)
url='http://10.77.77.1:8765/?t='+token
root=pathlib.Path('/Users/user/path/to/workdir/outputs')
os.umask(0o077)
p=root/'SimLocation-Console.txt'; p.write_text('手机开启 SimLocation WireGuard 后，在 Safari 打开：\n'+url+'\n\n此地址含访问令牌，请勿转发。手机连接尚待恢复验证。\n'); p.chmod(0o600)
p=root/'SimLocation-Console-QR.png'; qrcode.make(url).save(p); p.chmod(0o600)
print('Private console URL and QR saved, both mode 600; token not displayed')
