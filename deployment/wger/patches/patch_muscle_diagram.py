"""Patch the pinned shared React muscle renderer, never crop page overflow.

Apply to the original main.js source before collectstatic. The read-only Compose
mount keeps the same responsive emitter across service restarts and both views.
"""
import hashlib
from pathlib import Path
import sys

ORIGINAL_SHA256='38af8aa81a5fb7368a15f51234ff75e7b88893e857b50bac28a590f3276c5af7'
source=Path(sys.argv[1]); target=Path(sys.argv[2])
raw=source.read_bytes()
if hashlib.sha256(raw).hexdigest()!=ORIGINAL_SHA256:
    raise SystemExit('Pinned React source changed; review the component patch before deployment')
text=raw.decode()
old='height:`400px`,width:`200px`,backgroundImage:'
new='height:`auto`,width:`200px`,maxWidth:`100%`,aspectRatio:`1 / 2`,backgroundSize:`contain`,backgroundImage:'
if text.count(old)!=1 or text.count('muscular_system_back.svg')!=1:
    raise SystemExit('Expected shared muscle-diagram emitter was not identified uniquely')
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(text.replace(old,new))
print('Shared front/back muscle diagrams now fit their column with unchanged proportions')

