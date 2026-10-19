"""Conversation titling keeps the native route scoped and leaves main history byte-identical."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def test_conversation_title_wire_context_and_profile_route(tmp_path):
    from agent.title_generator import generate_conversation_title, MAX_TITLE_INPUT_CHARS
    from agent.auxiliary_client import shutdown_cached_clients
    wire = []
    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            wire.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"id":"title", "object":"chat.completion", "created":1,
                "model":wire[-1]["model"], "choices":[{"index":0,"message":{"role":"assistant",
                "content":'{"title":"Repair connection pool exhaustion"}'},"finish_reason":"stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1",0),Endpoint)
    thread = threading.Thread(target=server.serve_forever,kwargs={"poll_interval":0.01},daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    homes = [tmp_path/"a",tmp_path/"b"]
    for home, route in zip(homes,[{"provider":"custom","model":"profile-a","base_url":endpoint,"api_key":"test-key"}, {"provider":"auto"}]):
        home.mkdir()
        (home/"config.yaml").write_text(json.dumps({"auxiliary":{"title_generation":route}}),encoding="utf-8")
    history = [{"role":"system","content":"PRIVATE SYSTEM"},
        {"role":"user","content":"[Runtime note: PRIVATE RUNTIME]"},
        {"role":"assistant","content":"PRIVATE TOOL REQUEST","tool_calls":[{"id":"call"}]},
        {"role":"tool","content":"PRIVATE TOOL RESULT","tool_call_id":"call"},
        {"role":"user","content":"Investigate connection pool exhaustion"},
        {"role":"assistant","content":"The pool leaks connections after exceptions"}]
    original = deepcopy(history)
    try:
        shutdown_cached_clients()
        for home, expected in [(homes[0],"profile-a"),(homes[1],"session-override"),(homes[0],"profile-a")]:
            token=set_hermes_home_override(home)
            try:
                runtime={"provider":"custom","model":"session-override","api_key":"test-key","base_url":endpoint,"api_mode":"chat_completions"}
                assert generate_conversation_title(history,main_runtime=runtime) == "Repair connection pool exhaustion"
                assert wire[-1]["model"] == expected
                context=wire[-1]["messages"][-1]["content"]
                assert "connection pool" in context and "PRIVATE" not in context
                assert len(context) <= MAX_TITLE_INPUT_CHARS
                assert history == original
            finally:
                reset_hermes_home_override(token)
    finally:
        shutdown_cached_clients()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
