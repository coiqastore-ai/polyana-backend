import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Grant permissions
tables = ['trend_scan_runs', 'trend_source_runs', 'trend_candidates']
for table in tables:
    stdin, stdout, stderr = ssh.exec_command(f'sudo -u postgres psql -d polyana2 -c "GRANT ALL PRIVILEGES ON TABLE {table} TO polyana_app;"')
    print(f"Grant {table}:", stdout.read().decode().strip(), stderr.read().decode().strip())

# Also grant sequence permissions
stdin, stdout, stderr = ssh.exec_command('sudo -u postgres psql -d polyana2 -c "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO polyana_app;"')
print("Grant sequences:", stdout.read().decode().strip(), stderr.read().decode().strip())

ssh.close()
