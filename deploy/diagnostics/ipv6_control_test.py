import asyncio,json,subprocess
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
rules=[f'AND,((SRC-IP,fd00::10),(IP-CIDR6,::1/128,no-resolve),(DEST-PORT,{port})),DIRECT' for port in (49152,62078)]
async def main():
 c=json.loads(Path('/Users/user/.claude.json').read_text())['mcpServers']['surge-18p']
 async with stdio_client(StdioServerParameters(command=c['command'],args=c['args'])) as(r,w):
  async with ClientSession(r,w) as s:
   await s.initialize();added=[]
   try:
    for rule in rules:
     v=await s.call_tool('edit_temporary_rules',{'action':'add','rule':rule})
     print('ADD',v.model_dump_json(),flush=True)
     if v.isError:raise RuntimeError('Temporary rule rejected')
     added.append(rule)
    proc=await asyncio.create_subprocess_exec('/opt/homebrew/Caskroom/miniforge/base/bin/python3','work/probe_surge_pairing.py',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    out,_=await asyncio.wait_for(proc.communicate(),60);print(out.decode(),flush=True)
    for port in ('49152','62078'):
     v=await s.call_tool('list_requests',{'filter':port,'limit':4,'detail':True})
     for b in v.content:
      if b.type!='text':continue
      try:d=json.loads(b.text)
      except ValueError:print(b.text);continue
      for r in d.get('requests',[]):print(json.dumps({k:r.get(k) for k in ('id','sourceAddress','remoteHost','rule','failed','remark','outBytes','inBytes','notes')},ensure_ascii=False),flush=True)
   finally:
    for rule in reversed(added):
     v=await s.call_tool('edit_temporary_rules',{'action':'remove','rule':rule});print('REMOVE',v.model_dump_json(),flush=True)
asyncio.run(asyncio.wait_for(main(),110))
