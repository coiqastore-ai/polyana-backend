import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Release lock
stdin, stdout, stderr = ssh.exec_command('''cd /root/polyana-backend && source venv/bin/activate && python -c "
import asyncio
import asyncpg

async def release():
    db = await asyncpg.connect('postgresql://polyana_app:26ecb84075e9361da1ba8d9c41fe25f7@localhost:5432/polyana2')
    lock_id = 1234567890
    result = await db.fetchval('SELECT pg_advisory_unlock(\$1)', lock_id)
    print(f'Lock released: {result}')
    await db.close()

asyncio.run(release())
"''')
print(stdout.read().decode())
print(stderr.read().decode())

ssh.close()
