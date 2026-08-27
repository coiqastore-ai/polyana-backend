import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Check all env vars related to AI
stdin, stdout, stderr = ssh.exec_command('grep -E "(API_KEY|MODEL|BASE_URL)" /etc/polyana/env | sed "s/=.*/=REDACTED/"')
print("AI-related env vars:")
print(stdout.read().decode())

# Check if feedparser is installed
stdin, stdout, stderr = ssh.exec_command('cd /root/polyana-backend && source venv/bin/activate && pip list | grep -i feed')
print("feedparser installed:", stdout.read().decode())

# Check llm.env if exists
stdin, stdout, stderr = ssh.exec_command('ls -la /etc/polyana/llm.env 2>/dev/null || echo "llm.env not found"')
print("llm.env:", stdout.read().decode())

# Check OpenRouter
stdin, stdout, stderr = ssh.exec_command('grep OPENROUTER /etc/polyana/env | sed "s/=.*/=REDACTED/"')
print("OpenRouter config:", stdout.read().decode())

# Test Jina with different approach
stdin, stdout, stderr = ssh.exec_command('curl -s -H "Accept: application/json" "https://s.jina.ai/viral+recipe" | head -c 500')
print("Jina test (first 500 chars):", stdout.read().decode())

ssh.close()
