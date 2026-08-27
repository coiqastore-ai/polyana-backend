import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Test Qwen API
stdin, stdout, stderr = ssh.exec_command('''cd /root/polyana-backend && source venv/bin/activate && python -c "
import httpx

# Read from llm.env
api_key = None
base_url = None
model = None

with open('/etc/polyana/llm.env') as f:
    for line in f:
        line = line.strip()
        if line.startswith('QWEN_API_KEY='):
            api_key = line.split('=', 1)[1]
        elif line.startswith('QWEN_BASE_URL='):
            base_url = line.split('=', 1)[1]
        elif line.startswith('QWEN_TEXT_MODEL='):
            model = line.split('=', 1)[1]

print(f'API key found: {bool(api_key)}')
print(f'Base URL: {base_url}')
print(f'Model: {model}')

if api_key and base_url and model:
    r = httpx.post(
        f'{base_url}/chat/completions',
        headers={'Authorization': f'Bearer {api_key}'},
        json={
            'model': model,
            'messages': [{'role': 'user', 'content': 'Say hello'}],
            'max_tokens': 10,
        },
        timeout=30,
    )
    print(f'Qwen status: {r.status_code}')
    if r.status_code == 200:
        print('Qwen API works!')
    else:
        print(f'Error: {r.text[:200]}')
"''')
print("Qwen test:", stdout.read().decode())
print(stderr.read().decode())

# Check if Jina is blocked and try alternative
stdin, stdout, stderr = ssh.exec_command('curl -s -o /dev/null -w "%{http_code}" "https://www.google.com"')
print("Google accessible:", stdout.read().decode().strip())

ssh.close()
