import paramiko
import time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Check HEAD
stdin, stdout, stderr = ssh.exec_command('cd /root/polyana-backend && git rev-parse HEAD')
print("HEAD:", stdout.read().decode().strip())

# Check service status
stdin, stdout, stderr = ssh.exec_command('systemctl is-active polyana')
print("Service:", stdout.read().decode().strip())

# Check health
stdin, stdout, stderr = ssh.exec_command('curl -s http://127.0.0.1:8100/health')
print("Health:", stdout.read().decode())

# Check /trendscan command registration
stdin, stdout, stderr = ssh.exec_command('grep -n "trendscan" /root/polyana-backend/main.py | head -5')
print("trendscan in main.py:", stdout.read().decode())

# Check ADMIN_CHAT_ID
stdin, stdout, stderr = ssh.exec_command('grep ADMIN_CHAT_ID /etc/polyana/env')
print("ADMIN_CHAT_ID configured:", "yes" if stdout.read().decode().strip() else "no")

# Check Jina search
stdin, stdout, stderr = ssh.exec_command('curl -s -o /dev/null -w "%{http_code}" "https://s.jina.ai/test"')
print("Jina search HTTP:", stdout.read().decode().strip())

# Check yt-dlp
stdin, stdout, stderr = ssh.exec_command('which yt-dlp || ls /opt/trend-scanner/venv/bin/yt-dlp 2>/dev/null')
print("yt-dlp:", stdout.read().decode().strip() or "not found")

# Check RSS (feedparser)
stdin, stdout, stderr = ssh.exec_command('cd /root/polyana-backend && source venv/bin/activate && python -c "import feedparser; print(feedparser.__version__)"')
print("feedparser:", stdout.read().decode().strip())

# Check AI provider
stdin, stdout, stderr = ssh.exec_command('grep QWEN_API_KEY /etc/polyana/env | head -1 | cut -d= -f1')
print("QWEN_API_KEY:", stdout.read().decode().strip() or "not configured")

stdin, stdout, stderr = ssh.exec_command('grep QWEN_TEXT_MODEL /etc/polyana/env')
print("QWEN_TEXT_MODEL:", stdout.read().decode().strip() or "not configured")

ssh.close()
