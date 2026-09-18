#!/usr/bin/env python3
"""Remove only labelled disposable restore resources named by a receipt."""
import argparse, json, pathlib, re, subprocess
DOCKER=['docker','-H',f'unix://{pathlib.Path.home()}/.colima/docker.sock']

parser=argparse.ArgumentParser();parser.add_argument('receipt');args=parser.parse_args()
receipt_path=pathlib.Path(args.receipt);receipt=json.loads(receipt_path.read_text());name=receipt['project']
if not re.fullmatch(r'wger-restore-[0-9a-f]{10}',name):raise ValueError('Not a drill project')
expected={
    'container':{name+'-db',name+'-web',name+'-nginx'},
    'volume':{name+'-db',name+'-media',name+'-static'},
    'network':{name,name+'-front'},
}
resources={
    'container':receipt['created_containers'],
    'volume':receipt['volumes'],
    'network':receipt.get('networks',[receipt['network']]),
}
present={kind:[] for kind in resources}
for kind,identifiers in resources.items():
    for ident in identifiers:
        if ident not in expected[kind]:raise ValueError('Unexpected resource')
        inspected=subprocess.run([*DOCKER,kind,'inspect',ident],text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        if inspected.returncode:continue
        obj=json.loads(inspected.stdout)[0]
        labels=obj.get('Config',{}).get('Labels',{}) if kind=='container' else obj.get('Labels',{})
        if labels.get('fitness.backup.restore-drill')!=name:raise ValueError('Resource not owned by drill')
        present[kind].append(ident)
for ident in reversed(present['container']):subprocess.run([*DOCKER,'rm','-f',ident],check=True,stdout=subprocess.DEVNULL)
for ident in present['volume']:subprocess.run([*DOCKER,'volume','rm',ident],check=True,stdout=subprocess.DEVNULL)
for ident in present['network']:subprocess.run([*DOCKER,'network','rm',ident],check=True,stdout=subprocess.DEVNULL)
receipt['state']='drill resources cleaned; original snapshots retained';receipt_path.write_text(json.dumps(receipt,indent=2)+'\n');print(receipt['state'])
