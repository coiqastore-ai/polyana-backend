import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Check journal for all recent logs
stdin, stdout, stderr = ssh.exec_command('journalctl -u polyana --since "5 minutes ago" | tail -50')
print("Journal:")
print(stdout.read().decode())

ssh.close()
