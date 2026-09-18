"""Give the shared locale control its natural width, allowing row reflow."""
import hashlib
from pathlib import Path
import sys

source=Path(sys.argv[1]);target=Path(sys.argv[2]);raw=source.read_bytes()
if hashlib.sha256(raw).hexdigest()!='d35c810427b1f71aace03edd97dbee690ac67b57deb733118d449453942ef830':
    raise SystemExit('Pinned wger template changed; review footer patch before deployment')
text=raw.decode();old='<div class="col-md-2">';new='''<div class="col-12 col-md-auto">
                <a href="https://github.com/Cubatica/wger/tree/135d8569a3eb27c9f0f74e865d56372421a61294" class="text-muted small">Server source</a>
                <span class="text-muted small"> · </span>
                <a href="https://github.com/Cubatica/react/tree/3066f7693ac00632ad14ea0ef025371156f91d0d" class="text-muted small">UI source</a>'''
if text.count(old)!=1:
    raise SystemExit('Locale column was not identified uniquely')
target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text.replace(old,new))
print('Shared locale control keeps its full label and wraps as a column when needed')
