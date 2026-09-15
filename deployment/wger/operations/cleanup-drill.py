#!/usr/bin/env python3
"""Remove only the labelled disposable restore containers and volumes in receipt."""
import argparse,json,pathlib,re,subprocess
ap=argparse.ArgumentParser();ap.add_argument('receipt');args=ap.parse_args();r=json.loads(pathlib.Path(args.receipt).read_text());name=r['project']
if not re.fullmatch(r'wger-restore-[0-9a-f]{10}',name):raise ValueError('Not a drill project')
present={}
for kind,ids in [('container',r['created_containers']),('volume',r['volumes']),('network',r.get('networks',[r['network']]))]:
 present[kind]=[]
 existing=subprocess.check_output(['docker',kind,'ls']+(['-a'] if kind=='container' else [])+['--format','{{.Names}}' if kind=='container' else '{{.Name}}'],text=True).splitlines()
 for ident in ids:
  expected=[name+'-'+x for x in (['db','web','nginx'] if kind=='container' else ['db','media','static'])] if kind!='network' else [name,name+'-front']
  if ident not in expected:raise ValueError('Unexpected resource')
  if ident not in existing:continue
  present[kind].append(ident)
  obj=json.loads(subprocess.check_output(['docker',kind,'inspect',ident]))[0]
  labels=obj.get('Config',{}).get('Labels',{}) if kind=='container' else obj.get('Labels',{})
  if labels.get('fitness.backup.restore-drill')!=name:raise ValueError('Resource not owned by drill')
for ident in reversed(present['container']):subprocess.run(['docker','rm','-f',ident],check=True,stdout=subprocess.DEVNULL)
for ident in present['volume']:subprocess.run(['docker','volume','rm',ident],check=True,stdout=subprocess.DEVNULL)
for ident in present['network']:subprocess.run(['docker','network','rm',ident],check=True,stdout=subprocess.DEVNULL)
r['state']='drill resources cleaned; original snapshots retained';pathlib.Path(args.receipt).write_text(json.dumps(r,indent=2)+'\n');print(r['state'])
