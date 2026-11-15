"""Check the deterministic simulation's AI result against its known script."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gemini_pass as gp

folder = Path(sys.argv[1])
expected = json.loads((folder / 'expected.json').read_text())
actual = gp.parse_json((folder / 'gemini_raw_meeting_for_ai.json').read_text())
gp.validate_timestamps(actual, gp.duration_s(folder / 'meeting_for_ai.mp4'))
turns = actual['transcript']
assert len(turns) == len(expected), 'Unexpected turn count; review manually'
for want, got in zip(expected, turns):
    labels = {'You', 'Sam'} if want['speaker'] == 'You' else {'Client', 'Jamie'}
    assert got['speaker'] in labels, (want, got)
    assert abs(got['t'] - want['t']) < 3, ('Timestamp mismatch', want['t'], got['t'])
decisions = ' '.join(actual['decisions']).lower()
for key in ['navy', 'white', '48', 'banner', '20%', 'testimonial', '49']:
    assert key in decisions, ('Missing decision', key)
actions = actual['action_items']
assert any(a['owner'] in {'Client', 'Jamie'} and 'logo' in a['what'].lower() and '15' in a['due'] for a in actions)
assert any(a['owner'] in {'You', 'Sam'} and 'preview' in a['what'].lower() and '16' in a['due'] for a in actions)
refs = ' '.join(r['on_screen'] for r in actual['screen_refs']).lower()
for key in ['button', 'northstar', 'banner', 'testimonial', 'pricing']:
    assert key in refs, ('Missing screen object', key)
report = ('PASS: all nine speaker turns attributed correctly.\n'
          'PASS: timestamps within duration and within three seconds of the scripted turn starts.\n'
          'PASS: requested changes and unchanged price captured.\n'
          'PASS: logo and preview assigned to correct people with correct deadlines.\n'
          'PASS: screen references identify the relevant objects across both presenters.\n'
          'Scope: synthetic media and AI pipeline; not a live FaceTime call or overlapping-speech test.\n')
(folder / 'evaluation.txt').write_text(report)
print(report)
