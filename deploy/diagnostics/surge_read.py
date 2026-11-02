import asyncio,json,sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
 c=json.loads(Path('/Users/user/.claude.json').read_text())['mcpServers'][sys.argv[1]]
 async with stdio_client(StdioServerParameters(command=c['command'],args=c['args'])) as (r,w):
  async with ClientSession(r,w) as s:
   await s.initialize()
   for name,params in json.loads(sys.argv[2]):
    if not name.startswith(('get_','list_')):raise ValueError('Read-only tools required')
    v=await s.call_tool(name,params)
    print('===',name,'===',flush=True)
    for b in v.content:
     if b.type=='text':print(b.text,flush=True)
asyncio.run(asyncio.wait_for(main(),70))
