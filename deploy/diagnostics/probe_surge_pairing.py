import asyncio,json,time,struct,plistlib
from pathlib import Path
from pymobiledevice3.remote.tunnel_service import RemotePairingTunnelService,RemotePairingManualPairingService
udid=json.loads(Path('/Users/user/SimLocation/var/devices.json').read_text())['aliases']['myphone']
original_open=asyncio.open_connection
async def through_proxy(host,port,*a,**kw):
 if host not in ('100.64.0.10','fd00::11'): return await original_open(host,port,*a,**kw)
 r,w=await original_open('127.0.0.1',6152)
 target=f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
 w.write(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode());await w.drain()
 header=await r.readuntil(b'\r\n\r\n')
 if header.split(b' ')[1]!=b'200':
  w.close(); await w.wait_closed(); raise RuntimeError('CONNECT '+header.split(b'\r\n')[0].decode())
 return r,w
async def probe(base,host):
 class Trace(base):
  async def send_request(self,data):
   self.phase='send_handshake' if not self.sent else 'send_pairing';self.sent+=1
   await super().send_request(data);self.phase='await_reply'
  async def receive_response(self):
   result=await super().receive_response();self.received+=1;return result
 s=Trace(udid,host,49152);s.phase='connect';s.sent=0;s.received=0;t=time.monotonic()
 try:
  async with asyncio.timeout(8):await s.connect(autopair=False)
  result='VERIFIED'
 except Exception as e:result=type(e).__name__
 finally:await s.close()
 print(json.dumps({'host':host,'entry':base.__name__,'result':result,'stage':s.phase,'sent':s.sent,'received':s.received,'seconds':round(time.monotonic()-t,2)}),flush=True)
async def lockdown(host):
 w=None;t=time.monotonic()
 try:
  async with asyncio.timeout(8):
   r,w=await through_proxy(host,62078)
   p=plistlib.dumps({'Label':'SimLocationReadOnlyProbe','Request':'QueryType'})
   w.write(struct.pack('>I',len(p))+p);await w.drain()
   size=struct.unpack('>I',await r.readexactly(4))[0]
   if size>65536:raise ValueError('Unexpected length')
   d=plistlib.loads(await r.readexactly(size));result=d.get('Type',d.get('Error','unexpected_response'))
 except Exception as e:result=type(e).__name__
 finally:
  if w:w.close()
 print(json.dumps({'host':host,'port':62078,'result':result,'seconds':round(time.monotonic()-t,2)}),flush=True)
async def main():
 asyncio.open_connection=through_proxy
 try:
  for host in ('100.64.0.10','fd00::11'):
   for cls in (RemotePairingTunnelService,RemotePairingManualPairingService):await probe(cls,host)
   await lockdown(host)
 finally:asyncio.open_connection=original_open
asyncio.run(main())
