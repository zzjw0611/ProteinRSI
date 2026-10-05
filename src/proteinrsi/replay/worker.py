# SPDX-License-Identifier: MIT
"""Fresh interpreter: no inherited oracle memory or keys, no direct DB/file/model authority."""
import json
from pathlib import Path
import sys


def rpc(operation, **arguments):
    print(json.dumps({"rpc": operation, **arguments}), flush=True)
    line = sys.stdin.readline(32*1024*1024)
    if not line:
        raise RuntimeError("Controller disconnected")
    response = json.loads(line)
    if "error" in response:
        raise RuntimeError("Controller rejected operation: " + response["error"])
    return response["result"]


class RemoteStore:
    def __init__(self, root):
        self.root = Path(root)
    def get(self, namespace, key, default=None):
        return rpc("get", namespace=namespace, key=key, default=default)
    def all(self, namespace):
        return rpc("all", namespace=namespace)
    def put(self, namespace, key, value, *, immutable=False):
        return rpc("put", namespace=namespace, key=key, value=value, immutable=immutable)
    def event(self, kind, payload):
        return rpc("event", kind=kind, payload=payload)
    def remaining(self, resource):
        return rpc("remaining", resource=resource)
    def usage(self):
        return rpc("usage")


class RemoteLLM:
    def __init__(self, store, identity):
        self.store = store
        self.model = identity["model"]
        self.base_url = identity["base_url"]
        self.cache_settings = identity["cache_settings"]
    def complete(self, role, instructions, context, schema):
        return rpc("llm", role=role, instructions=instructions, context=context, schema=schema)


def main():
    start = json.loads(sys.stdin.readline(32*1024*1024))
    # Restrict BEFORE importing numpy, tools or agent code; no arbitrary actions before enforcement.
    from proteinrsi.replay.sandbox import restrict
    security = restrict(start["read_roots"], start["work"])
    from proteinrsi.contracts import TaskView
    from proteinrsi.agents import Team, MetaAgent
    from proteinrsi.tools import ToolGateway, ToolSpec

    class RemoteGateway(ToolGateway):
        remote_context_tools = True
        def fork(self):
            clone = RemoteGateway(self.store, allow_egress=self.allow_egress)
            clone._tools = dict(self._tools)
            return clone
        def catalog(self, task, allowed):
            # The controller already checked all configured workflow bindings and
            # sent only the subset compatible with this task and egress policy.
            return super().catalog(task, [name for name in allowed if name in self._tools])
        def call(self, call, task, *, allowed, context_key):
            if call.name not in allowed:
                raise PermissionError("Tool not allowed in this role")
            return rpc("tool", call=call.model_dump(mode="json"), context_key=context_key)

    store = RemoteStore(start["work"])
    llm = RemoteLLM(store, start["llm"]) if start["llm"] else None
    gateway = RemoteGateway(store, allow_egress=start["allow_egress"])
    for spec in start["tools"]:
        gateway.register(ToolSpec.model_validate(spec), lambda _: None)
    team = Team(store, llm, gateway, register_tools=False)
    view = TaskView.model_validate(start["view"])
    if start["operation"] == "team":
        output = [c.model_dump(mode="json") for c in team.run(view)]
    elif start["operation"] == "feedback":
        output = team.analyst.feedback(view).model_dump(mode="json")
    elif start["operation"] == "meta":
        output = MetaAgent(llm, store).propose(view, start["last_patch_round"]).model_dump(mode="json")
    else:
        raise ValueError("Unknown worker operation")
    print(json.dumps({"done": output, "sandbox": security}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"failed": type(exc).__name__, "message": str(exc)[:600]}), flush=True)
        raise SystemExit(2)
