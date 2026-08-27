import paramiko
import time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Pull latest
stdin, stdout, stderr = ssh.exec_command('cd /root/polyana-backend && git pull origin feature/editorial-content-mvp')
print("Pull:", stdout.read().decode())

# Restart service
stdin, stdout, stderr = ssh.exec_command('systemctl restart polyana')
print("Restart:", stdout.read().decode())

time.sleep(2)

# Run scan
print("Starting scan...")
start = time.time()

script = """
import asyncio
import json
import sys
sys.path.insert(0, '/root/polyana-backend')

from trend_scanner.scanner import run_scan, acquire_scan_lock, release_scan_lock
import asyncpg

async def main():
    db = await asyncpg.connect('postgresql://polyana_app:26ecb84075e9361da1ba8d9c41fe25f7@localhost:5432/polyana2')
    
    if not await acquire_scan_lock(db):
        print(json.dumps({'error': 'Lock already held'}))
        return
    
    try:
        result = await run_scan(db=db, dry_run=False)
        print(json.dumps(result, indent=2, default=str))
    finally:
        await release_scan_lock(db)
        await db.close()

asyncio.run(main())
"""

stdin, stdout, stderr = ssh.exec_command(f'cd /root/polyana-backend && source venv/bin/activate && python -c "{script}"', timeout=300)
output = stdout.read().decode()
error = stderr.read().decode()
elapsed = time.time() - start

print(f"Duration: {elapsed:.1f}s")
print("Output:", output)
if error:
    print("Errors:", error[-500:])

ssh.close()
