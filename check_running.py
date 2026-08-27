import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Check if scan is still running
stdin, stdout, stderr = ssh.exec_command('ps aux | grep python | grep -v grep')
print("Python processes:")
print(stdout.read().decode())

# Check journal
stdin, stdout, stderr = ssh.exec_command('journalctl -u polyana --since "5 minutes ago" | tail -30')
print("Journal:")
print(stdout.read().decode())

ssh.close()
