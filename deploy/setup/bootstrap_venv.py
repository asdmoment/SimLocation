import hashlib,subprocess
from pathlib import Path
root=Path('work/venv-debs');root.mkdir(parents=True,exist_ok=True)
packages=[
('pool/universe/p/python-pip/python3-pip-whl_25.1.1+dfsg-1ubuntu2_all.deb','ee08bb36dcd86a0e0aa96611d4da8b87a3253c8220b535def261e91631bb47bd'),
('pool/universe/s/setuptools/python3-setuptools-whl_78.1.1-0.1build1_all.deb','e945108f14dab1ed9ac2ebe813286cca2073c962097bcc8a796bd141c8d15fcb'),
('pool/universe/p/python3.14/python3.14-venv_3.14.4-1ubuntu0.2_amd64.deb','cad2456bf3cb57cb18d2c82b48b549e5b4ac6eee9b3e14e65334209387e3d0ae'),
('pool/universe/p/python3-defaults/python3-venv_3.14.3-0ubuntu2_amd64.deb','17a2d766fd1d46b7ab2a8b0f7bb576f3956ea001f9ab58ec79ea8983cd85c13f')]
for rel,expected in packages:
 p=root/Path(rel).name
 subprocess.run(['curl','-fsSL','--connect-timeout','8','--max-time','45','https://archive.ubuntu.com/ubuntu/'+rel,'-o',str(p)],check=True)
 if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:raise SystemExit('SHA256 mismatch: '+p.name)
 print('Verified',p.name,flush=True)
(root/'SHA256SUMS').write_text(''.join(f'{sha}  {Path(rel).name}\n' for rel,sha in packages))
