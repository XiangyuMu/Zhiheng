"""HTTP evidence-backed answers while a personal fact remains unresolved."""
from pathlib import Path

from fastapi.testclient import TestClient

from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_personal_updates import _update
from tests.integration.test_relation_publication_history import _worker_pass


def _publish(client: TestClient, csrf: str, key: str, claim: str, premise: str = "") -> None:
    source = client.post('/v1/conclusions/sources', json={'text': claim},
                         headers=_headers(csrf, key + '-source'))
    assert source.status_code == 200, source.text
    draft = client.post('/v1/conclusions', json={
        'source_id': source.json()['id'], 'title': key, 'claim': claim,
        'domain_id': 'career_work_practice', 'excerpt': claim,
        'premises': [{'text': premise, 'confirmed': False}] if premise else [],
    }, headers=_headers(csrf, key + '-draft'))
    assert draft.status_code == 200, draft.text
    item = draft.json()
    approved = client.post(f"/v1/conclusions/{item['id']}/approve", json={},
                           headers=_headers(csrf, key + '-approve', item['etag']))
    assert approved.status_code == 200, approved.text
    for index in range(3):
        _worker_pass(client, f'{key}-worker-{index}')


def _conflict(client: TestClient, csrf: str) -> None:
    for index, city in enumerate(('北京', '上海')):
        response = client.post('/v1/personal-updates', json=_update(city),
                               headers=_headers(csrf, f'city-{index}'))
        assert response.status_code == 200, response.text


def test_conflict_answer_preserves_explicit_conditional_alternatives(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    _conflict(client, csrf)
    _publish(client, csrf, 'beijing', '通勤可以选择北京地铁', '居住在北京')
    _publish(client, csrf, 'shanghai', '通勤可以选择上海地铁', '居住在上海')
    response = client.post('/v1/answers', json={'query': '居住 通勤'},
                           headers=_headers(csrf, 'conditional-answer'))
    assert response.status_code == 200, response.text
    body = response.json()
    assert '如果居住在北京，则通勤可以选择北京地铁' in body['answer']
    assert '如果居住在上海，则通勤可以选择上海地铁' in body['answer']
    assert len(body['citations']) == 2
    assert any(p['kind'] == 'conflict' for p in body['context_prompts'])
    assert body['personalization_refs'] == []


def test_conflict_defers_personal_part_and_unrelated_answer_continues(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    _conflict(client, csrf)
    _publish(client, csrf, 'commute', '无论居住在哪里，通勤出行应遵守交通规则')
    response = client.post('/v1/answers', json={'query': '居住 通勤'},
                           headers=_headers(csrf, 'partial-answer'))
    assert response.status_code == 200, response.text
    body = response.json()
    assert '通勤出行应遵守交通规则' in body['answer']
    assert body['citations']
    assert body['personalization_refs'] == []
    assert any('北京' in item and '上海' in item and '暂缓' in item
               for item in body['insufficiencies'])
    unrelated = client.post('/v1/answers', json={'query': '通勤'},
                            headers=_headers(csrf, 'unrelated-answer'))
    assert unrelated.status_code == 200, unrelated.text
    assert unrelated.json()['context_prompts'] == []
    assert '通勤出行应遵守交通规则' in unrelated.json()['answer']
