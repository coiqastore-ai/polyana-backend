import paramiko
import time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Kill any stuck scans
stdin, stdout, stderr = ssh.exec_command('pkill -f "trend_scanner"')
time.sleep(1)

# Run scan with timeout
stdin, stdout, stderr = ssh.exec_command(
    'cd /root/polyana-backend && source venv/bin/activate && timeout 60 python run_real_scan.py',
    timeout=90
)

output = stdout.read().decode()
error = stderr.read().decode()

print("Output:", output[-2000:] if len(output) > 2000 else output)
print("Error:", error[-1000:] if len(error) > 1000 else error)

ssh.close()
