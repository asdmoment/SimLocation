import asyncio,json,os,socket,subprocess,time
from pathlib import Path
from pymobiledevice3.remote.tunnel_service import RemotePairingTunnelService
import pymobiledevice3.remote.userspace_tunnel as ut
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
udid=os.environ['SIMLOCATION_UDID']
children=[];start=time.monotonic();stage='startup'
def note(text):print(f'[{time.monotonic()-start:.2f}s] {text}',flush=True)
async def forward(remote_port):
 with socket.socket() as sock:
  sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 p=subprocess.Popen(['ssh','-N','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ExitOnForwardFailure=yes','-o','ControlMaster=no','-o','ControlPath=none','-L',f'127.0.0.1:{port}:10.77.77.2:{remote_port}',os.environ.get('SIMLOCATION_SSH_HOST','vps')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 children.append(p)
 for _ in range(30):
  await asyncio.sleep(.1)
  if p.poll() is not None:raise RuntimeError('SSH forward failed')
  ready=subprocess.run(['lsof','-nP','-a','-p',str(p.pid),f'-iTCP:{port}','-sTCP:LISTEN'],capture_output=True)
  if ready.returncode==0:return port
 raise TimeoutError('SSH listener not ready')
class Provider(RemotePairingTunnelService):
 async def create_tcp_listener(self):
  global stage
  stage='create_data_listener'
  parameters=await super().create_tcp_listener()
  remote_port=parameters['port'];note('Device TCP tunnel listener allocated')
  parameters['port']=await forward(remote_port);note('Data port forward ready (no extra TCP probe)')
  stage='tunnel_tls_and_rsd';return parameters
async def provider(serial,autopair,remotepairing_fallback=True):
 global stage
 stage='pair_verify';port=await forward(49152)
 p=Provider(udid,'127.0.0.1',port)
 try:await p.connect(autopair=False)
 except BaseException:await p.close();raise
 note('PAIR_VERIFIED');return p,None
async def main():
 global stage
 original=ut._create_no_root_tunnel_provider;ut._create_no_root_tunnel_provider=provider
 try:
  async with asyncio.timeout(40):
   async with ut.UserspaceRsdTunnel(serial=udid,autopair=False) as rsd:
    note('RSD_CONNECTED; service_count='+str(len(rsd.peer_info.get('Services',{}))))
    stage='dvt_connect'
    async with DvtProvider(rsd) as dvt:
     note('DVT_CONNECTED')
     stage='location_channel'
     async with LocationSimulation(dvt):note('LOCATION_CHANNEL_OPEN; no coordinates sent')
    stage='cleanup'
  note('VERIFICATION_COMPLETE; location unchanged')
 except Exception as e:note('FAILED stage='+stage+' exception='+type(e).__name__)
 finally:ut._create_no_root_tunnel_provider=original
original_open=asyncio.open_connection
async def traced_open(*args,**kwargs):
 global stage
 if kwargs.get('ssl') is not None:
  stage='tls_psk';note('TLS_PSK_START')
  result=await asyncio.wait_for(original_open(*args,**kwargs),10)
  stage='rsd_tunnel';note('TLS_PSK_CONNECTED');return result
 return await original_open(*args,**kwargs)
asyncio.open_connection=traced_open
from pymobiledevice3.remote.tunnel_service import RemotePairingTcpTunnel
original_establish=RemotePairingTcpTunnel.request_tunnel_establish
async def traced_establish(self):
 global stage
 stage='tunnel_parameters';note('TUNNEL_PARAMETERS_REQUEST')
 result=await asyncio.wait_for(original_establish(self),10)
 stage='rsd_connect';note('TUNNEL_PARAMETERS_RECEIVED');return result
RemotePairingTcpTunnel.request_tunnel_establish=traced_establish
try:asyncio.run(main())
finally:
 for p in children:
  if p.poll() is None:p.terminate()
 for p in children:
  try:p.wait(timeout=3)
  except subprocess.TimeoutExpired:p.kill();p.wait()
 note('Temporary SSH forwards closed')
