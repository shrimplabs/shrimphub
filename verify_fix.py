import os, sys
sys.path.insert(0, '.')
from unittest.mock import patch, MagicMock
import json
os.environ['MINIMAX_API_KEY'] = 'test-key'
import swarm.llm_utils as rt

def al(text):
    return [
        f"data: {json.dumps({'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}})}",
        f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}",
        f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}})}",
        f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}",
        f"data: {json.dumps({'type': 'message_delta', 'delta': {}, 'usage': {'output_tokens': 5}})}",
        f"data: {json.dumps({'type': 'message_stop'})}",
    ]

def ar(text, status=200):
    m = MagicMock(); m.status_code = status; m.text = text
    if status == 200: m.iter_lines.return_value = al(text)
    return m

m = ar("Hello")
with patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}):
    with patch("requests.post", return_value=m):
        text, _, _ = rt.call_llm("sys", [{"role": "user", "content": "hi"}])
assert text == "Hello", text
print("PASS test_anthropic_format_returns_text")
