import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Check if changes are deployed
stdin, stdout, stderr = ssh.exec_command('cd /root/polyana-backend && git log --oneline -3')
print("Git log:")
print(stdout.read().decode())

# Check scanner.py for the fix
stdin, stdout, stderr = ssh.exec_command('grep -A5 "No qualified candidates" /root/polyana-backend/trend_scanner/scanner.py')
print("Scanner fix:")
print(stdout.read().decode())

ssh.close()
