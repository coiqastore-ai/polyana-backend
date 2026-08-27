import paramiko
import time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Force pull
stdin, stdout, stderr = ssh.exec_command('cd /root/polyana-backend && git fetch origin && git reset --hard origin/feature/editorial-content-mvp')
print("Deploy:", stdout.read().decode())
print(stderr.read().decode())

# Verify
stdin, stdout, stderr = ssh.exec_command('cd /root/polyana-backend && git log --oneline -3')
print("Git log:")
print(stdout.read().decode())

# Restart
stdin, stdout, stderr = ssh.exec_command('systemctl restart polyana')
print("Restart:", stdout.read().decode())

time.sleep(2)

# Health check
stdin, stdout, stderr = ssh.exec_command('curl -s http://127.0.0.1:8100/health')
print("Health:", stdout.read().decode())

ssh.close()
