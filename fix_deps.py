import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Install feedparser
stdin, stdout, stderr = ssh.exec_command('cd /root/polyana-backend && source venv/bin/activate && pip install feedparser -q')
print("Install feedparser:", stdout.read().decode(), stderr.read().decode())

# Check llm.env (redacted)
stdin, stdout, stderr = ssh.exec_command('cat /etc/polyana/llm.env | sed "s/=.*/=REDACTED/"')
print("llm.env contents:")
print(stdout.read().decode())

# Test OpenRouter
stdin, stdout, stderr = ssh.exec_command('''cd /root/polyana-backend && source venv/bin/activate && python -c "
import httpx
import os

# Read API key
api_key = None
with open('/etc/polyana/env') as f:
    for line in f:
        if line.startswith('OPENROUTER_API_KEY='):
            api_key = line.strip().split('=', 1)[1]
            break

if api_key:
    r = httpx.get('https://openrouter.ai/api/v1/models', headers={'Authorization': f'Bearer {api_key}'}, timeout=10)
    print(f'OpenRouter status: {r.status_code}')
    if r.status_code == 200:
        models = r.json().get('data', [])
        print(f'Available models: {len(models)}')
else:
    print('No API key found')
"''')
print("OpenRouter test:", stdout.read().decode())
print(stderr.read().decode())

ssh.close()
