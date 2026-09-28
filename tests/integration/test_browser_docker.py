"""Real native browser tools on a disposable public-only Docker network."""

import os
from uuid import uuid4

import docker
import pytest
from docker.errors import NotFound

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TALOS_TEST_DOCKER") != "1", reason="Set TALOS_TEST_DOCKER=1"
    ),
]


def test_native_browsers_use_public_only_egress():
    import json
    import time

    from worker.runtime import NATIVE_IMAGES, launch_options, native_config, prepare_volumes

    docker_client = docker.from_env(timeout=150)
    prefix = "talos-browser-test-" + uuid4().hex[:12]
    labels = {"io.talos.test": prefix}
    network = docker_client.networks.create(prefix, internal=True)
    containers, volumes = [], []
    try:
        proxy = docker_client.containers.run(
            "talos-egress:local",
            name=prefix + "-proxy",
            labels=labels,
            detach=True,
        )
        containers.append(proxy)
        network.connect(proxy, aliases=["talos-egress"])
        for kind in ("openclaw", "hermes"):
            state, config = prefix + "-" + kind + "-state", prefix + "-" + kind + "-config"
            volumes.extend((state, config))
            prepare_volumes(
                docker_client,
                state,
                config,
                native_config(kind, "synthetic-hash"),
                labels,
                native=True,
                runtime_kind=kind,
            )
            image = os.environ.get("TALOS_TEST_" + kind.upper() + "_IMAGE", NATIVE_IMAGES[kind])
            container = docker_client.containers.run(
                **launch_options(
                    prefix + "-" + kind,
                    state,
                    network.name,
                    config,
                    labels,
                    native_image=image,
                    runtime_kind=kind,
                    control_token="test-browser-token",
                    control_origin="http://localhost",
                )
            )
            containers.append(container)
            assert container.attrs["HostConfig"]["ReadonlyRootfs"]
            assert docker_client.networks.get(network.id).attrs["Internal"]

            def execute(command, container=container):
                result = container.exec_run(["timeout", "90s", *command], demux=True)
                stdout, stderr = result.output
                assert result.exit_code == 0, (stdout or b"") + (stderr or b"")
                return (stdout or b"").decode()

            if kind == "openclaw":
                execute(
                    [
                        "node",
                        "--input-type=module",
                        "-e",
                        "import assert from 'node:assert/strict';"
                        "import {N as check} from '/app/dist/chrome-BfgKoTT4.mjs';"
                        "let calls=0;const opts={url:'https://example.com',lookupFn:async()=>{"
                        "calls++;return [{address:'93.184.216.34',family:4}]}};"
                        "await check({...opts,browserProxyMode:'explicit-browser-proxy',"
                        "ssrfPolicy:{dangerouslyAllowPrivateNetwork:true}});assert.equal(calls,0);"
                        "await check(opts);assert.equal(calls,1);"
                        "await assert.rejects(check({...opts,"
                        "browserProxyMode:'explicit-browser-proxy',"
                        "ssrfPolicy:{dangerouslyAllowPrivateNetwork:false}}));"
                        "await assert.rejects(check({...opts,url:'file:///etc/passwd'}));",
                    ]
                )
                deadline = time.monotonic() + 45
                while True:
                    probe = container.exec_run(
                        [
                            "node",
                            "-e",
                            "fetch('http://127.0.0.1:18789/health')"
                            ".then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                        ]
                    )
                    if probe.exit_code == 0:
                        break
                    assert time.monotonic() < deadline, "Gateway readiness timeout"
                    time.sleep(0.5)

            def browse(url, kind=kind, execute=execute):
                if kind == "hermes":
                    code = f"new_tab({url!r}); print(page_info())"
                    output = execute(
                        [
                            "python",
                            "-c",
                            "from tools.browser_use_cli import browser_exec; "
                            f'print(browser_exec({code!r},session="proof",timeout_s=60))',
                        ]
                    )
                    result = json.loads(output.splitlines()[-1])
                    if "error" in result:
                        return result["error"]
                    assert result["success"], result
                    return result["output"]
                execute(["node", "openclaw.mjs", "browser", "open", url, "--json"])
                return execute(["node", "openclaw.mjs", "browser", "snapshot", "--json"])

            expected = "Example Domain" if kind == "hermes" else "documentation examples"
            assert expected in browse("https://example.com")
            for url in ("http://127.0.0.1", "http://169.254.169.254"):
                rejection = browse(url)
                assert (
                    "requested URL could not be retrieved" in rejection
                    or "Blocked: URL targets a cloud metadata endpoint" in rejection
                ), (kind, rejection)
            # Redirects also resolve and enforce policy at the public-only proxy.
            assert expected in browse(
                "https://httpbingo.org/redirect-to?url=https%3A%2F%2Fexample.com"
            )
            assert "requested URL could not be retrieved" in browse(
                "https://httpbin.org/redirect-to?url=http%3A%2F%2F127.0.0.1"
            )
            if kind == "openclaw":
                execute(["node", "openclaw.mjs", "browser", "stop", "--json"])
            else:
                execute(["env", "BU_NAME=proof", "browser-use", "--reload"])
                execute(["agent-browser", "--session", "bu-named-proof", "close"])
            # A new browser must still work after shutdown and policy rejection.
            assert expected in browse("https://example.com")
    finally:
        for container in reversed(containers):
            container.remove(force=True)
        network.remove()
        for name in volumes:
            try:
                docker_client.volumes.get(name).remove()
            except NotFound:
                pass
        docker_client.close()
