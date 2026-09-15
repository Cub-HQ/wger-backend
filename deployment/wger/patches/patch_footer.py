"""Give the shared locale control its natural width, allowing row reflow."""
import hashlib
from pathlib import Path
import sys

source=Path(sys.argv[1]);target=Path(sys.argv[2]);raw=source.read_bytes()
if hashlib.sha256(raw).hexdigest()!='d35c810427b1f71aace03edd97dbee690ac67b57deb733118d449453942ef830':
    raise SystemExit('Pinned wger template changed; review footer patch before deployment')
text=raw.decode();old='<div class="col-md-2">';new='<div class="col-12 col-md-auto">'
if text.count(old)!=1:
    raise SystemExit('Locale column was not identified uniquely')
target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text.replace(old,new))
print('Shared locale control keeps its full label and wraps as a column when needed')
