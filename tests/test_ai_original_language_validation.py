from copy import deepcopy

import pytest

from erp.mercadolibre_translation import title_language_issue
from scripts.repair_ai_original_language_validation import ERROR, repaired_snapshot


@pytest.mark.parametrize('title,language', [
    ('Fantasia Cosplay Viúva Negra Menina Halloween Festa', 'pt-BR'),
    ('Peluca cosplay Naruto Hinata Hyuga negro azulado lisa con', 'es'),
    ('Máscara de Freira Demoniaca Halloween, Fantasia Assustadora', 'pt-BR'),
])
def test_mixed_brand_titles_are_not_unsupported_languages(title, language):
    assert title_language_issue(title, language) == ''


@pytest.mark.parametrize('language', ['es', 'pt-BR'])
def test_real_wrong_language_remains_blocked(language):
    assert '英文' in title_language_issue('Black Panther Costume For Girls Halloween Party', language)
    assert '中文' in title_language_issue('Cosplay 黑豹 costume', language)


def test_repair_revalidates_without_changing_copy_or_review():
    draft = {'status': 'completed', 'title_es': 'Traje para fiesta',
             'title_pt': 'Fantasia Cosplay Viúva Negra Menina Halloween Festa',
             'description_es': 'Descripción', 'main_image_url': '/image.jpg',
             'validation': {'errors': ['葡语' + ERROR], 'warnings': []}}
    snapshot = {'ai_original': draft, 'marketplace_listings': {'mercadolibre': deepcopy(draft)},
                'review_status': 'unreviewed'}
    before = deepcopy(snapshot)
    repaired = repaired_snapshot(snapshot)
    assert snapshot == before
    assert repaired['ai_original']['validation']['errors'] == []
    assert repaired['marketplace_listings']['mercadolibre']['validation']['errors'] == []
    assert repaired['review_status'] == 'unreviewed'
    assert repaired['ai_original']['title_pt'] == draft['title_pt']
    assert repaired_snapshot(repaired) is None
    snapshot['ai_original']['title_es'] = 'Black Panther Costume For Girls Halloween Party'
    assert any('英文' in e for e in repaired_snapshot(snapshot)['ai_original']['validation']['errors'])


def test_repair_skips_active_generation():
    assert repaired_snapshot({'ai_original': {'status': 'processing',
        'validation': {'errors': [ERROR]}}}) is None
