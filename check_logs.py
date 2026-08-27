import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Check journal for scan logs
stdin, stdout, stderr = ssh.exec_command('journalctl -u polyana --since "2 minutes ago" | grep -i "trend\|scan\|llm\|qualified" | tail -20')
print("Journal:")
print(stdout.read().decode())

ssh.close()
