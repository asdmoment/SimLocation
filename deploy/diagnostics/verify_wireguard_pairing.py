import asyncio,json,socket,subprocess,time
from pathlib import Path
from pymobiledevice3.remote.tunnel_service import RemotePairingTunnelService,RemotePairingManualPairingService
udid=json.loads(Path('/Users/user/SimLocation/var/devices.json').read_text())['aliases']['myphone']
with socket.socket() as sock:
 sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
proc=subprocess.Popen(['ssh','-N','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ExitOnForwardFailure=yes','-o','ControlMaster=no','-o','ControlPath=none','-L',f'127.0.0.1:{port}:10.77.77.2:49152','vps'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
async def run():
 await asyncio.sleep(1)
 if proc.poll() is not None:raise RuntimeError('SSH forwarding exited')
 for cls in (RemotePairingTunnelService,RemotePairingManualPairingService):
  class Trace(cls):
   async def receive_response(self):
    d=await super().receive_response();self.responses+=1;return d
  svc=Trace(udid,'127.0.0.1',port);svc.responses=0;t=time.monotonic()
  try:
   async with asyncio.timeout(8):await svc.connect(autopair=False)
   result='PAIR_VERIFIED'
  except Exception as e:result=type(e).__name__
  finally:await svc.close()
  print(json.dumps({'entry':cls.__name__,'result':result,'responses':svc.responses,'seconds':round(time.monotonic()-t,2)}),flush=True)
try:asyncio.run(run())
finally:
 proc.terminate()
 try:proc.wait(timeout=5)
 except subprocess.TimeoutExpired:proc.kill();proc.wait()
 print('Temporary SSH forwarding closed')
