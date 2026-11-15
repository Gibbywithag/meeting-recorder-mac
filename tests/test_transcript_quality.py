import pytest
from transcript_quality import validate_content, segment_context


def result(turns):
    return {'transcript': turns, 'screen_refs': [], 'summary': 'Discussed layout.',
            'decisions': [], 'action_items': [], 'proposals': [], 'open_questions': []}


def test_rejects_missing_transcript_instead_of_publishing_empty_success():
    with pytest.raises(ValueError, match='transcript'):
        validate_content({'summary': 'Missing speech'}, 60)


def test_rejects_missing_speaker_and_empty_speech():
    for turn in ({'t': 0, 'text': 'Hi'}, {'t': 0, 'speaker': 'Speaker 1', 'text': ''}):
        with pytest.raises(ValueError):
            validate_content(result([turn]), 60)


def test_rejects_large_repeated_passage_but_keeps_normal_replies():
    long = 'Please move the calendar into the sidebar and keep the mobile menu visible. ' * 4
    with pytest.raises(ValueError, match='repeated'):
        validate_content(result([{'t': t, 'speaker': 'Speaker 1', 'text': long} for t in (0, 30)]), 60)
    validate_content(result([{'t': t, 'speaker': 'Speaker 2', 'text': 'Yeah.'} for t in (0, 30)]), 60)


def test_context_carries_identity_without_replaying_speech_or_timestamps():
    context = segment_context(result([{'t': 42, 'speaker': 'Speaker 1', 'text': 'Secret quoted conversation'}]))
    assert 'Speaker 1' in context
    assert 'Secret quoted conversation' not in context
    assert '42' not in context
    assert 'Timestamps must start at zero' in context


def test_validates_proposals_actions_and_screen_references():
    good = result([{'t': 0, 'speaker': 'Unknown', 'text': '[inaudible]'}])
    validate_content(good, 60)
    for key, value in [('proposals', 'Not a list'), ('action_items', [{'what': 'Ship'}]),
                       ('screen_refs', [{'t': 10, 'said': 'This'}])]:
        bad = {**good, key: value}
        with pytest.raises(ValueError):
            validate_content(bad, 60)
