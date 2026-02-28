"""End-to-end test for the Reachy Bridge API.

Simulates what the OpenClaw extension does:
1. Check bridge status
2. Connect WebSocket for transcript streaming
3. Inject text message
4. Verify robot responds and transcript arrives via WebSocket
5. Test /reachy command equivalents (say, dance)
"""

import asyncio
import json
import sys
import time

import httpx
import websockets

BRIDGE_URL = "http://localhost:8100"
WS_URL = "ws://localhost:8100/bridge/ws"
SECRET = None  # Set if REACHY_BRIDGE_SECRET is configured


def headers():
    h = {"Content-Type": "application/json"}
    if SECRET:
        h["x-bridge-secret"] = SECRET
    return h


async def test_status():
    print("\n=== Test 1: Bridge Status ===")
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BRIDGE_URL}/bridge/status", headers=headers())
        data = r.json()
        print(f"  Status: {data}")
        assert data["connected"] is True, "Bridge not connected!"
        print("  PASS: Bridge is connected")
        return True


async def test_inject_text():
    print("\n=== Test 2: Text Injection ===")
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{BRIDGE_URL}/bridge/inject",
            headers=headers(),
            json={"text": "[Test from OpenClaw] What is 2 + 2?"},
            timeout=15.0,
        )
        data = r.json()
        print(f"  Response: {data}")
        assert data["ok"] is True, f"Injection failed: {data}"
        print("  PASS: Text injected successfully")
        return True


async def test_inject_with_instructions():
    print("\n=== Test 3: Text Injection with Response Instructions ===")
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{BRIDGE_URL}/bridge/inject",
            headers=headers(),
            json={
                "text": "[Command from Telegram] Do a dance!",
                "response_instructions": "Use the dance tool to perform a dance move. Speak your response.",
            },
            timeout=15.0,
        )
        data = r.json()
        print(f"  Response: {data}")
        assert data["ok"] is True, f"Injection failed: {data}"
        print("  PASS: Injection with instructions succeeded")
        return True


async def test_websocket_transcripts():
    print("\n=== Test 4: WebSocket Transcript Streaming ===")
    ws_url = WS_URL
    if SECRET:
        ws_url += f"?secret={SECRET}"

    transcripts = []

    async def listen_ws():
        async with websockets.connect(ws_url) as ws:
            print("  WebSocket connected, waiting for transcripts...")
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
                    data = json.loads(msg)
                    print(f"  Transcript: role={data.get('role')} content={data.get('content', '')[:100]}")
                    transcripts.append(data)
                    if len(transcripts) >= 2:
                        return
            except asyncio.TimeoutError:
                print("  Timeout waiting for transcripts")

    async def inject_after_delay():
        await asyncio.sleep(2)
        print("  Injecting test message...")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BRIDGE_URL}/bridge/inject",
                headers=headers(),
                json={"text": "[WebSocket test] Tell me a one-sentence fun fact about robots."},
                timeout=15.0,
            )

    await asyncio.gather(listen_ws(), inject_after_delay())

    if transcripts:
        print(f"  PASS: Received {len(transcripts)} transcript(s) via WebSocket")
        return True
    else:
        print("  WARN: No transcripts received (robot may not have responded in time)")
        return False


async def test_validation():
    print("\n=== Test 5: Validation & Error Handling ===")
    async with httpx.AsyncClient() as client:
        # Empty payload
        r = await client.post(
            f"{BRIDGE_URL}/bridge/inject",
            headers=headers(),
            json={},
            timeout=10.0,
        )
        assert r.status_code == 400, f"Expected 400, got {r.status_code}"
        print("  PASS: Empty payload returns 400")

        # Auth test (only if secret is set)
        if SECRET:
            r = await client.post(
                f"{BRIDGE_URL}/bridge/inject",
                json={"text": "unauthorized"},
                timeout=10.0,
            )
            assert r.status_code == 401, f"Expected 401, got {r.status_code}"
            print("  PASS: Missing secret returns 401")
        else:
            print("  SKIP: Auth test (no secret configured)")

        return True


async def main():
    print("=" * 60)
    print("Reachy Bridge End-to-End Test")
    print("=" * 60)
    print(f"Bridge URL: {BRIDGE_URL}")
    print(f"Auth: {'configured' if SECRET else 'open (no secret)'}")

    results = {}
    tests = [
        ("status", test_status),
        ("inject_text", test_inject_text),
        ("inject_instructions", test_inject_with_instructions),
        ("websocket", test_websocket_transcripts),
        ("validation", test_validation),
    ]

    for name, test_fn in tests:
        try:
            results[name] = await test_fn()
        except Exception as e:
            print(f"  FAIL: {e}")
            results[name] = False

    print("\n" + "=" * 60)
    print("Results:")
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {status}: {name}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n{passed}/{total} tests passed")

    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
