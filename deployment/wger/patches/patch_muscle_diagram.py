"""Patch the pinned shared React muscle renderer, never crop page overflow.

Apply to the original main.js source before collectstatic. The read-only Compose
mount keeps the same responsive emitter across service restarts and both views.
"""
import hashlib
from pathlib import Path
import sys

ORIGINAL_SHA256 = '38af8aa81a5fb7368a15f51234ff75e7b88893e857b50bac28a590f3276c5af7'
WAVE_ONE_SHA256 = '763fcf8ab715e5ee4f7c4be7ad999c06a465ea09afaf4fb26e67fe77eb30cc04'
WAVE_ONE_REVIEW_REPAIR_SHA256 = 'fa79075075c16f43cabb118d244de81dbf63b4d749a0a4920d35587032ae05f1'
WAVE_TWO_CARDIO_SHA256 = '5c3a2b01a43f0f4d0dfc8c21486d5226a029ffe881adf5f84431ad4b3d7840fc'
WAVE_THREE_PHONE_SHA256 = 'd17d8954046843114f04325b7ac1ea0310e064965ec7a976831a3510e2317ced'
SET_PROGRESSION_SHA256 = 'dd679bcdb4ea6e884821f6d06057a3523bd67033ab764ef54cfc77b9e0a646fd'
CHART_RANGE_SHA256 = '5776e03fc88f9dfed8694a16d65bac863a2ee40101c9b6209ee12683bbe22781'
AUSTRALIAN_DATES_SHA256 = '1cd83f8aa69416c989decd7ba6700bd409d3d64b29e5f3593692b4c2c5c876ba'
APPROVED_SHA256 = frozenset({ORIGINAL_SHA256, WAVE_ONE_SHA256, WAVE_ONE_REVIEW_REPAIR_SHA256, WAVE_TWO_CARDIO_SHA256, WAVE_THREE_PHONE_SHA256, SET_PROGRESSION_SHA256, CHART_RANGE_SHA256, AUSTRALIAN_DATES_SHA256})


def patch_bundle(source, target, approved_sha256=APPROVED_SHA256):
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest not in approved_sha256:
        raise SystemExit(f'Pinned React source changed ({digest}); review the component patch before deployment')
    text = raw.decode()
    old = 'height:`400px`,width:`200px`,backgroundImage:'
    new = 'height:`auto`,width:`200px`,maxWidth:`100%`,aspectRatio:`1 / 2`,backgroundSize:`contain`,backgroundImage:'
    if text.count(old) != 1 or text.count('muscular_system_back.svg') != 1:
        raise SystemExit('Expected shared muscle-diagram emitter was not identified uniquely')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace(old, new))
    print('Shared front/back muscle diagrams now fit their column with unchanged proportions')


if __name__ == '__main__':
    patch_bundle(Path(sys.argv[1]), Path(sys.argv[2]))

