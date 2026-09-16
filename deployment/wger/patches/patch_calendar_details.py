"""Show plain activity notes in the existing calendar session disclosure.

Input is pinned wger 2.7 main.js after patch_muscle_diagram.py. The anchors
are Entries.tsx (tOt), not the shared WorkoutSession.textRepresentation:
ordinary routine summaries and exercise logs keep their existing rendering.
Generic notes are React text children, never HTML or parsed metadata.
"""
import hashlib
from pathlib import Path
import sys

source = Path(sys.argv[1])
target = Path(sys.argv[2])
raw = source.read_bytes()
if hashlib.sha256(raw).hexdigest() != '0e687e50c669cf29b2d48a2a3fa49b90bd62e9d2235eba027def1425c9bd99d4':
    raise SystemExit('Pinned muscle-patched React source changed; review calendar patch before deployment')
text = raw.decode()
old_summary = 'primary:n(`routines.workoutSession`),secondary:e.textRepresentation,sx:'
new_summary = 'primary:n(`routines.workoutSession`),secondary:e.routineId===null&&e.notes?e.notes.split(/\\r?\\n/,1)[0]:e.textRepresentation,sx:'
old_details = 'Z(uu,{in:s===e.id,timeout:`auto`,unmountOnExit:!0,children:Z(yh,{sx:{pl:4,pt:0},children:e.logs.map(e=>Z(Sh,{dense:!0,children:Z(Dh,{primary:e.exerciseObj?.getTranslation().name,secondary:`${e.repetitions} × ${e.weight} `})},e.id))})})'
new_details = 'Z(uu,{in:s===e.id,timeout:`auto`,unmountOnExit:!0,children:Q(yh,{sx:{pl:4,pt:0},children:[e.routineId===null&&e.notes&&Z(Sh,{children:Z(Xd,{component:`div`,sx:{whiteSpace:`pre-wrap`,overflowWrap:`anywhere`},children:e.notes})}),e.logs.map(e=>Z(Sh,{dense:!0,children:Z(Dh,{primary:e.exerciseObj?.getTranslation().name,secondary:`${e.repetitions} × ${e.weight} `})},e.id))]})})'
if text.count(old_summary) != 1 or text.count(old_details) != 1:
    raise SystemExit('Expected calendar session summary and disclosure were not identified uniquely')
text = text.replace(old_summary, new_summary).replace(old_details, new_details)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(text)
print('Routine-null calendar sessions show a first-line summary and expandable plain-text notes')
