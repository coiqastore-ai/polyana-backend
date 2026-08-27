import paramiko
import json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('155.212.140.121', username='root', password='pb5*EFJ46x12', timeout=30, banner_timeout=30)

# Get all candidates from this run
script = '''
import asyncio
import asyncpg
import json

async def main():
    db = await asyncpg.connect('postgresql://polyana_app:26ecb84075e9361da1ba8d9c41fe25f7@localhost:5432/polyana2')
    
    rows = await db.fetch("""
        SELECT 
            id, title, source_platform, source_url,
            content_type, canonical_dish_name,
            trend_score, trend_confidence,
            freshness_score, engagement_score, cross_source_score,
            published_at, raw_engagement
        FROM trend_candidates 
        WHERE discovered_at > NOW() - INTERVAL '1 hour'
        ORDER BY trend_score DESC NULLS LAST
        LIMIT 30
    """)
    
    for r in rows:
        d = dict(r)
        if d.get('published_at'):
            d['published_at'] = d['published_at'].isoformat()
        if d.get('raw_engagement'):
            d['raw_engagement'] = json.loads(d['raw_engagement']) if isinstance(d['raw_engagement'], str) else d['raw_engagement']
        print(json.dumps(d, default=str))

asyncio.run(main())
'''

stdin, stdout, stderr = ssh.exec_command(f'cd /root/polyana-backend && source venv/bin/activate && python -c "{script}"')
print(stdout.read().decode())
print(stderr.read().decode())

ssh.close()
