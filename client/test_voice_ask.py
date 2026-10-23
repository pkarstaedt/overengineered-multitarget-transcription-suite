import collections
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import overmultiasrsuite as app


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class VoiceAskTests(unittest.TestCase):
    def test_model_override_falls_back_to_selected_post_edit_provider_model(self):
        cfg = app.DEFAULT_CONFIG.copy()
        cfg.update({
            "ask_model": "",
            "openai_model": "hosted-base",
            "openai_compatible_post_edit_model": "compatible-base",
            "external_post_edit_model": "external-base",
        })
        self.assertEqual(app._ask_model_for_provider(cfg, "openai"), "hosted-base")
        self.assertEqual(app._ask_model_for_provider(cfg, "openai_compatible"), "compatible-base")
        self.assertEqual(app._ask_model_for_provider(cfg, "external"), "external-base")
        cfg["ask_model"] = "ask-override"
        self.assertEqual(app._ask_model_for_provider(cfg, "external"), "ask-override")

    @mock.patch.object(app.requests, "get")
    def test_external_model_query_uses_sibling_models_endpoint(self, get):
        get.return_value = _FakeResponse({
            "data": [
                {"id": "qwen3:1.7b"},
                {"id": "gemma3:4b"},
            ],
        })
        models = app._fetch_external_models("http://localhost:8010/external_postedit")
        self.assertEqual(models, ["gemma3:4b", "qwen3:1.7b"])
        self.assertEqual(get.call_args.args[0], "http://localhost:8010/models")

    @mock.patch.object(app.requests, "get")
    def test_hosted_model_query_uses_configured_openai_key(self, get):
        get.return_value = _FakeResponse({"data": [{"id": "model-b"}, {"id": "model-a"}]})
        models = app._fetch_openai_models("secret-test-key")
        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(get.call_args.args[0], "https://api.openai.com/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer secret-test-key")

    def test_model_catalog_accepts_ollama_tags_shape(self):
        self.assertEqual(
            app._model_ids_from_catalog({"models": [{"name": "alpha"}, {"model": "beta"}]}),
            ["alpha", "beta"],
        )

    def test_ask_prompt_substitutes_transcript(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt_path = Path(temp_dir) / "ask.md"
            prompt_path.write_text(
                "# Voice Ask Prompt\n\n## System\nSystem\n\n## Developer\nDeveloper\n\n## User\nQuestion: {transcript}\n",
                encoding="utf-8",
            )
            cfg = app.DEFAULT_CONFIG.copy()
            cfg["ask_prompt_markdown_file"] = str(prompt_path)
            messages = app._render_ask_messages("Why?", cfg)
        self.assertEqual(messages[-1], {"role": "user", "content": "Question: Why?"})

    def test_ask_routes_through_post_edit_provider_selection(self):
        cfg = app.DEFAULT_CONFIG.copy()
        cfg["post_edit_provider"] = "external"
        with mock.patch.object(
            app,
            "_ask_text_external",
            return_value=("Answer", "", True, "model"),
        ) as external, mock.patch.object(app, "_ask_text_openai") as hosted:
            answer, error, success, provider, model = app._ask_text("Question", cfg)
        external.assert_called_once()
        hosted.assert_not_called()
        self.assertEqual((answer, error, success, provider, model), ("Answer", "", True, "external", "model"))

    def test_compatible_ask_does_not_add_post_edit_json_contract(self):
        cfg = app.DEFAULT_CONFIG.copy()
        messages = app._openai_compatible_chat_messages(
            [{"role": "developer", "content": "Answer directly."}, {"role": "user", "content": "Hello"}],
            cfg,
            require_post_edit_json=False,
        )
        combined = "\n".join(message["content"] for message in messages)
        self.assertNotIn("edited_text", combined)
        self.assertIn("Answer directly.", combined)

    @mock.patch.object(app.requests, "post")
    def test_external_ask_uses_configured_post_edit_url(self, post):
        post.return_value = _FakeResponse({"answer": "External answer"})
        cfg = app.DEFAULT_CONFIG.copy()
        cfg.update({
            "external_post_edit_url": "http://example.invalid/external_postedit",
            "external_post_edit_model": "local-model",
            "ask_model": "",
        })
        answer, error, success, model = app._ask_text_external(
            "Question",
            cfg,
            [{"role": "user", "content": "Question"}],
        )
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(answer, "External answer")
        self.assertEqual(model, "local-model")
        called_url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertEqual(called_url, cfg["external_post_edit_url"])
        self.assertEqual(payload["mode"], "ask")
        self.assertNotIn("num_predict", payload["options"])

    @mock.patch.object(app.requests, "post")
    def test_external_ask_maps_output_limit_to_ollama_num_predict(self, post):
        post.return_value = _FakeResponse({"answer": "External answer"})
        cfg = app.DEFAULT_CONFIG.copy()
        cfg.update({
            "external_post_edit_url": "http://example.invalid/external_postedit",
            "external_post_edit_model": "local-model",
            "ask_max_output_tokens": 321,
        })
        app._ask_text_external("Question", cfg, [{"role": "user", "content": "Question"}])
        self.assertEqual(post.call_args.kwargs["json"]["options"]["num_predict"], 321)

    @mock.patch.object(app.requests, "post")
    def test_compatible_ask_parses_free_form_answer(self, post):
        post.return_value = _FakeResponse({"choices": [{"message": {"content": "Compatible answer"}}]})
        cfg = app.DEFAULT_CONFIG.copy()
        cfg.update({
            "openai_compatible_post_edit_url": "http://example.invalid/v1/chat/completions",
            "openai_compatible_post_edit_model": "compatible-model",
        })
        answer, error, success, model = app._ask_text_openai_compatible(
            "Question",
            cfg,
            [{"role": "user", "content": "Question"}],
        )
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(answer, "Compatible answer")
        self.assertEqual(model, "compatible-model")

    def test_ask_history_never_becomes_last_insertable_result(self):
        previous_history = app._history
        try:
            app._history = collections.deque([
                {"kind": "dictation", "text": "Insertable transcription"},
                {"kind": "ask", "text": "Do not insert this answer"},
            ], maxlen=20)
            self.assertEqual(app._latest_history_text(), "Insertable transcription")
        finally:
            app._history = previous_history

    def test_dismissed_ask_request_cannot_reopen(self):
        request_id = app._next_ask_request_id()
        app._show_preview_overlay(
            "Thinking",
            "Thinking",
            "main",
            ask=True,
            dismissible=True,
            request_id=request_id,
        )
        app._dismiss_ask_preview()
        app._show_preview_overlay(
            "Late answer",
            "Answer ready",
            "main",
            ask=True,
            dismissible=True,
            request_id=request_id,
        )
        with app._preview_overlay_lock:
            self.assertFalse(app._preview_overlay_state["visible"])


if __name__ == "__main__":
    unittest.main()
