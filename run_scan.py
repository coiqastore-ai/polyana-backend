import paramiko
import time
import json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Run the actual scan
print("Starting trend scan...")
start_time = time.time()

stdin, stdout, stderr = ssh.exec_command('''cd /root/polyana-backend && source venv/bin/activate && python -c "
import asyncio
import json
import sys
sys.path.insert(0, '/root/polyana-backend')

from trend_scanner.scanner import run_scan, acquire_scan_lock, release_scan_lock
import asyncpg

async def main():
    db = await asyncpg.connect('postgresql://polyana_app:26ecb84075e9361da1ba8d9c41fe25f7@localhost:5432/polyana2')
    
    # Acquire lock
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
"''', timeout=300)

output = stdout.read().decode()
error = stderr.read().decode()
elapsed = time.time() - start_time

print(f"Duration: {elapsed:.1f}s")
print("Output:", output)
if error:
    print("Errors:", error)

ssh.close()
