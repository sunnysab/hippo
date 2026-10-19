"""The reading-assistant, daily-report and LLM-provider contracts.

These pin the shapes the /docs document and the runtime must agree on: the
request bodies are typed, the streaming answer is a server-sent-events body
rather than JSON, the report preview is HTML, a partial PATCH leaves the fields
it did not mention alone, and a provider's stored API key never leaves the
process in clear.
"""

from __future__ import annotations

import importlib
import pkgutil
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from _openapi import document as _document
from _openapi import resolve as _resolve
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from hippo.api import deps
from hippo.api import schemas as schemas_pkg
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import chat as chat_api
from hippo.api.routers import llm as llm_api
from hippo.api.routers import report as report_api
from hippo.api.schemas.chat import MessageRequest, SessionCreateRequest, SessionRenameRequest
from hippo.api.schemas.llm import ProviderCreateRequest, ProviderUpdateRequest
from hippo.api.schemas.report import ReportSettingUpdateRequest

HTTP_METHODS = ('get', 'post', 'put', 'patch', 'delete')


def _success_schema(spec: dict[str, Any], path: str, method: str, code: str) -> dict[str, Any]:
    content = spec['paths'][path][method]['responses'][code]['content']
    return next(iter(content.values()))['schema']


class _NullTransaction:
    async def __aenter__(self) -> _NullTransaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _client(storage: SimpleNamespace, user: SimpleNamespace) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(chat_api.router, prefix='/api')
    app.include_router(report_api.router, prefix='/api')
    app.include_router(llm_api.router, prefix='/api')
    app.dependency_overrides[deps.get_storage] = lambda: storage
    app.dependency_overrides[deps.current_user] = lambda: user
    app.dependency_overrides[deps.require_admin] = lambda: user
    return TestClient(app)


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username='alice', role='user', timezone='UTC', email=None)


def _session() -> dict[str, Any]:
    return {
        'id': 1,
        'article_id': None,
        'provider_id': None,
        'title': '新对话',
        'created_at': None,
        'updated_at': None,
    }


def _setting(**overrides: Any) -> dict[str, Any]:
    base = {'enabled': True, 'send_hour': 8, 'recipients': [], 'group_ids': [], 'include_read': True}
    base.update(overrides)
    return base


class RequestContractTest(unittest.TestCase):
    def test_a_chat_session_can_be_opened_with_no_fields(self) -> None:
        body = SessionCreateRequest()

        self.assertIsNone(body.article_id)
        self.assertIsNone(body.title)

    def test_a_non_integer_article_id_is_a_validation_failure(self) -> None:
        with self.assertRaises(ValidationError):
            SessionCreateRequest.model_validate({'article_id': 'not-a-number'})

    def test_renaming_needs_a_title(self) -> None:
        with self.assertRaises(ValidationError):
            SessionRenameRequest.model_validate({})
        self.assertEqual('x', SessionRenameRequest.model_validate({'title': 'x'}).title)

    def test_an_unknown_preset_is_rejected_by_the_body(self) -> None:
        with self.assertRaises(ValidationError):
            MessageRequest.model_validate({'preset': 'translate-to-klingon'})
        self.assertEqual('summary', MessageRequest.model_validate({'preset': 'summary'}).preset)

    def test_creating_a_provider_needs_the_four_core_fields(self) -> None:
        body = ProviderCreateRequest(name='p', base_url='http://x', api_key='k', model='m')

        self.assertFalse(body.is_default)
        self.assertTrue(body.enabled)
        with self.assertRaises(ValidationError):
            ProviderCreateRequest.model_validate({'name': 'p', 'base_url': 'http://x', 'model': 'm'})

    def test_a_provider_patch_distinguishes_unset_from_a_value(self) -> None:
        body = ProviderUpdateRequest(name='renamed')

        # Only the field the caller mentioned is in the set: the rest keep their
        # stored value rather than reverting to a default.
        self.assertEqual({'name'}, body.model_fields_set)
        self.assertIsNone(body.api_key)

    def test_a_report_patch_only_names_the_keys_that_were_sent(self) -> None:
        body = ReportSettingUpdateRequest.model_validate({'enabled': False})

        self.assertEqual({'enabled'}, body.model_fields_set)
        with self.assertRaises(ValidationError):
            ReportSettingUpdateRequest.model_validate({'send_hour': 'soon'})


class StreamingContractTest(unittest.TestCase):
    def _storage(self, **chat: Any) -> SimpleNamespace:
        return SimpleNamespace(transaction=_NullTransaction, chat=SimpleNamespace(**chat))

    def test_a_cached_preset_answers_as_server_sent_events(self) -> None:
        storage = self._storage(
            get_session=AsyncMock(return_value=_session()),
            find_preset=AsyncMock(return_value={'id': 7, 'content': '[preset:summary]\n摘要内容', 'created_at': None}),
        )

        response = _client(storage, _user()).post('/api/chat/session/1/message', json={'preset': 'summary'})

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers['content-type'].startswith('text/event-stream'))
        # An SSE body: records separated by a blank line, each one a data line.
        self.assertIn('data: {', response.text)
        self.assertIn('摘要内容', response.text)
        self.assertNotIn('application/json', response.headers['content-type'])

    def test_a_missing_session_is_a_json_error_before_the_stream_starts(self) -> None:
        storage = self._storage(get_session=AsyncMock(return_value=None))

        response = _client(storage, _user()).post('/api/chat/session/1/message', json={'content': 'hi'})

        self.assertEqual(404, response.status_code)
        self.assertEqual('http_404', response.json()['code'])

    def test_the_document_promises_events_and_not_json(self) -> None:
        spec = _document()
        message = spec['paths']['/api/chat/session/{session_id}/message']['post']

        self.assertEqual(['text/event-stream'], list(message['responses']['200']['content']))
        request = message['requestBody']['content']['application/json']['schema']
        self.assertEqual('MessageRequest', request['$ref'].rpartition('/')[-1])

    def test_opening_a_session_defaults_the_title_and_returns_the_session(self) -> None:
        created = _session()
        storage = SimpleNamespace(
            transaction=_NullTransaction,
            chat=SimpleNamespace(create_session=AsyncMock(return_value=created)),
        )

        response = _client(storage, _user()).post('/api/chat/session', json={})

        self.assertEqual(201, response.status_code)
        self.assertEqual('新对话', response.json()['title'])
        self.assertEqual('新对话', storage.chat.create_session.await_args.kwargs['title'])

    def test_a_malformed_article_id_is_a_validation_envelope(self) -> None:
        storage = SimpleNamespace(transaction=_NullTransaction, chat=SimpleNamespace())

        response = _client(storage, _user()).post('/api/chat/session', json={'article_id': 'x'})

        self.assertEqual(422, response.status_code)
        self.assertEqual('request.invalid', response.json()['code'])


class ReportContractTest(unittest.TestCase):
    def test_a_partial_patch_changes_only_what_it_names(self) -> None:
        reports = SimpleNamespace(
            get_setting=AsyncMock(return_value=_setting()),
            save_setting=AsyncMock(return_value=_setting(enabled=False)),
        )
        storage = SimpleNamespace(transaction=_NullTransaction, reports=reports)

        response = _client(storage, _user()).patch('/api/report/setting', json={'enabled': False})

        self.assertEqual(200, response.status_code)
        self.assertEqual({'enabled': False}, reports.save_setting.await_args.args[1])
        self.assertFalse(response.json()['enabled'])

    def test_an_empty_patch_is_refused(self) -> None:
        reports = SimpleNamespace(get_setting=AsyncMock(), save_setting=AsyncMock())
        storage = SimpleNamespace(transaction=_NullTransaction, reports=reports)

        response = _client(storage, _user()).patch('/api/report/setting', json={})

        self.assertEqual(400, response.status_code)
        reports.save_setting.assert_not_awaited()

    def test_a_send_hour_outside_the_day_is_refused(self) -> None:
        reports = SimpleNamespace(get_setting=AsyncMock(), save_setting=AsyncMock())
        storage = SimpleNamespace(transaction=_NullTransaction, reports=reports)

        response = _client(storage, _user()).patch('/api/report/setting', json={'send_hour': 25})

        self.assertEqual(400, response.status_code)
        reports.save_setting.assert_not_awaited()

    def test_reading_the_setting_returns_the_documented_shape(self) -> None:
        reports = SimpleNamespace(get_setting=AsyncMock(return_value=_setting(send_hour=7)))
        storage = SimpleNamespace(transaction=_NullTransaction, reports=reports)

        payload = _client(storage, _user()).get('/api/report/setting').json()

        self.assertEqual(7, payload['send_hour'])
        self.assertEqual(
            {'enabled', 'send_hour', 'recipients', 'group_ids', 'include_read'},
            set(payload),
        )

    def test_the_preview_is_html_not_json(self) -> None:
        context = {'site_name': 'Hippo', 'report_date': '2026-03-01'}
        storage = SimpleNamespace(transaction=_NullTransaction, reports=SimpleNamespace())
        with (
            patch.object(report_api, '_collect', AsyncMock(return_value=(_setting(), [], context))),
            patch.object(report_api, 'render_report', lambda _: '<html><body>日报</body></html>'),
        ):
            response = _client(storage, _user()).get('/api/report/2026-03-01/html')

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers['content-type'].startswith('text/html'))
        self.assertIn('日报', response.text)

    def test_the_document_types_the_preview_and_the_send_result(self) -> None:
        spec = _document()

        preview = spec['paths']['/api/report/{date}/html']['get']['responses']['200']['content']
        self.assertEqual(['text/html'], list(preview))
        self.assertEqual(
            'ReportOut', _success_schema(spec, '/api/report/{date}', 'get', '200')['$ref'].rpartition('/')[-1]
        )
        send = _success_schema(spec, '/api/report/{date}/send', 'post', '202')
        self.assertEqual('ReportSendOut', send['$ref'].rpartition('/')[-1])


class LlmContractTest(unittest.TestCase):
    def _row(self, **extra: Any) -> dict[str, Any]:
        return {
            'id': 1,
            'name': 'p',
            'base_url': 'http://llm.local/v1',
            'api_key_masked': '••••cdef',
            'model': 'm',
            'is_default': True,
            'enabled': True,
            'api_key': 'sk-secret-value',
            **extra,
        }

    def test_a_provider_list_never_echoes_the_stored_key(self) -> None:
        storage = SimpleNamespace(llm=SimpleNamespace(list_all=AsyncMock(return_value=[self._row()])))

        response = _client(storage, _user()).get('/api/llm/provider')

        self.assertEqual(200, response.status_code)
        self.assertNotIn('sk-secret-value', response.text)
        self.assertEqual('••••cdef', response.json()['items'][0]['api_key_masked'])

    def test_the_provider_document_names_no_key_field(self) -> None:
        spec = _document()
        listing = _resolve(spec, _success_schema(spec, '/api/llm/provider', 'get', '200'))
        provider = _resolve(spec, listing['properties']['items']['items'])
        probe = _resolve(spec, _success_schema(spec, '/api/llm/provider/{provider_id}/test', 'post', '200'))

        self.assertNotIn('api_key', provider['properties'])
        self.assertIn('api_key_masked', provider['properties'])
        self.assertNotIn('api_key', probe['properties'])

    def test_creating_a_provider_documents_the_typed_body(self) -> None:
        spec = _document()
        request = _resolve(
            spec, spec['paths']['/api/llm/provider']['post']['requestBody']['content']['application/json']['schema']
        )

        self.assertEqual(
            {'name', 'base_url', 'api_key', 'model', 'is_default', 'enabled'},
            set(request['properties']),
        )
        self.assertEqual({'name', 'base_url', 'api_key', 'model'}, set(request['required']))


class SchemaNameTest(unittest.TestCase):
    """FastAPI names a component after its class, so one name must have one owner.

    Two models with the same name make the generated ``$ref`` fall back to a
    module-qualified name, and which of the two wins is iteration order: the
    document would change between runs. The chat and auth session models are the
    pair this exists to keep apart.
    """

    def test_no_two_schema_modules_define_the_same_class_name(self) -> None:
        owners: dict[str, str] = {}
        for info in pkgutil.iter_modules(schemas_pkg.__path__):
            module = importlib.import_module(f'{schemas_pkg.__name__}.{info.name}')
            for name, value in vars(module).items():
                if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == module.__name__:
                    owner = owners.setdefault(name, module.__name__)
                    self.assertEqual(owner, module.__name__, f'{name} is defined by both {owner} and {module.__name__}')


class DocumentReferenceTest(unittest.TestCase):
    """The operations reference the schemas above rather than describing objects."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _document()

    def test_every_assistant_operation_names_its_response_model(self) -> None:
        expected = {
            ('/api/chat/session', 'get'): 'ChatSessionListOut',
            ('/api/chat/session/{session_id}', 'get'): 'ChatSessionDetailOut',
            ('/api/report/setting', 'get'): 'ReportSettingOut',
        }
        for (path, method), name in expected.items():
            schema = _success_schema(self.spec, path, method, '200')
            self.assertEqual(name, schema['$ref'].rpartition('/')[-1], f'{method.upper()} {path}')

    def test_the_html_preview_is_not_wrapped_in_json(self) -> None:
        content = self.spec['paths']['/api/report/{date}/html']['get']['responses']['200']['content']
        self.assertNotIn('application/json', content)


if __name__ == '__main__':
    unittest.main()
