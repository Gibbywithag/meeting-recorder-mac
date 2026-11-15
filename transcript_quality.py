"""Content validation and safe context for meeting analysis segments."""
import json


def validate_content(result: dict, seconds: float) -> None:
    """Reject malformed results and long repeated passages; never silently delete speech."""
    fields = {'transcript': ('speaker', 'text'), 'screen_refs': ('said', 'on_screen'),
              'action_items': ('what', 'owner', 'due')}
    for field, required in fields.items():
        items = result.get(field)
        if not isinstance(items, list):
            raise ValueError(f'{field} must be a list')
        for item in items:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key].strip() for key in required
            ):
                raise ValueError(f'{field} entries require nonempty strings: {required}')
    if not isinstance(result.get('summary'), str):
        raise ValueError('summary must be a string')
    for field in ('decisions', 'proposals', 'open_questions'):
        items = result.get(field, [])
        if not isinstance(items, list) or any(not isinstance(x, str) or not x.strip() for x in items):
            raise ValueError(f'{field} must be a list of nonempty strings')
    seen = set()
    for turn in result['transcript']:
        text = ' '.join(turn['text'].casefold().split())
        key = (turn['speaker'].casefold(), text)
        if len(text) >= 240 and key in seen:
            raise ValueError('Long repeated transcript passage; verify against this clip only')
        seen.add(key)


def segment_context(result: dict) -> str:
    # Passing quoted turns with old timestamps can induce replay or absolute timestamps.
    labels = list(dict.fromkeys(turn['speaker'] for turn in result.get('transcript', [])))
    return ('\nPrevious segment speaker labels (identity hints only, not speech to transcribe): '
            + json.dumps(labels, ensure_ascii=False)
            + '\nMatch these labels only when the voices support the identity. Do not infer a role from a label. '
              'Transcribe ONLY the attached clip; do not reconstruct prior discussion. '
              'Timestamps must start at zero for this segment, never at the meeting offset.')
