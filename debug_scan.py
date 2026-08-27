import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Check journal for scan errors
stdin, stdout, stderr = ssh.exec_command('journalctl -u polyana --since "10 minutes ago" | grep -i "trend\|scan\|llm\|error" | tail -30')
print("Journal:")
print(stdout.read().decode())

# Check if yt-dlp returns upload dates
stdin, stdout, stderr = ssh.exec_command('''/opt/trend-scanner/venv/bin/yt-dlp --flat-playlist --print "%(id)s\\t%(title)s\\t%(upload_date)s" "ytsearch1:viral recipe" 2>/dev/null''')
print("yt-dlp test:")
print(stdout.read().decode())

ssh.close()
