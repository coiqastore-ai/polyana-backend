import paramiko
import time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Kill stuck scan
stdin, stdout, stderr = ssh.exec_command('pkill -f "trend_scanner.scanner"')
print("Kill:", stdout.read().decode(), stderr.read().decode())

time.sleep(1)

# Write scan script to VPS
sftp = ssh.open_sftp()
with sftp.open('/root/polyana-backend/run_real_scan.py', 'w') as f:
    f.write('''
import asyncio
import json
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

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
''')
sftp.close()

print("Script written. Run manually on VPS with:")
print("  cd /root/polyana-backend && source venv/bin/activate && python run_real_scan.py")

ssh.close()
