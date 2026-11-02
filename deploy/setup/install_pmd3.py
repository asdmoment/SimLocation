import subprocess,time
ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=12','-o','ControlMaster=no','-o','ControlPath=none']
check=subprocess.run(ssh+['vps','ss -Hlt "sport = :18152"'],capture_output=True,text=True,timeout=20)
if check.returncode or check.stdout.strip():raise SystemExit('Reverse proxy port unavailable; no changes made')
proxy=subprocess.Popen(ssh+['-o','ExitOnForwardFailure=yes','-N','-R','127.0.0.1:18152:127.0.0.1:6152','vps'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
try:
 time.sleep(1)
 if proxy.poll() is not None:raise SystemExit('Temporary reverse proxy failed')
 command='HTTPS_PROXY=http://127.0.0.1:18152 HTTP_PROXY=http://127.0.0.1:18152 /opt/simlocation-venv/bin/python -m pip install --disable-pip-version-check --timeout 20 --retries 1 "pymobiledevice3==11.49.2" requests'
 result=subprocess.run(ssh+['vps',command],timeout=240)
 if result.returncode:raise SystemExit(result.returncode)
finally:
 proxy.terminate()
 try:proxy.wait(timeout=5)
 except subprocess.TimeoutExpired:proxy.kill();proxy.wait()
 print('Temporary installation proxy closed',flush=True)
