"""Headless page inspector: collect diagnostics, output structured text.

Usage:
    python inspect_page.py --url http://localhost:8000 [--wait-ms 3000]

Outputs a structured text report suitable for LLM consumption.
No multimodal model needed — all data is text.
"""
from __future__ import annotations

import argparse
import sys


def _collect_diagnostics(url: str, wait_ms: int) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "ERROR: playwright is not installed. Run: pip install playwright && python -m playwright install chromium --with-deps"

    sections: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        console_messages: list[dict[str, str]] = []
        network_failures: list[dict[str, str]] = []
        ws_connected = False
        ws_message_count = 0
        ws_url = ""

        def _on_console(msg):
            console_messages.append({"level": msg.type, "text": msg.text})

        def _on_response(response):
            if response.status >= 400:
                network_failures.append({
                    "url": response.url,
                    "status": str(response.status),
                    "method": response.request.method,
                })

        page.on("console", _on_console)
        page.on("response", _on_response)

        # Inject WS interceptor before navigation
        page.add_init_script("""
            window.__ryobot_ws = { connected: false, messages: 0, url: "" };
            const OrigWS = window.WebSocket;
            window.WebSocket = function(...args) {
                window.__ryobot_ws.url = args[0] || "";
                const ws = new OrigWS(...args);
                ws.addEventListener("open", () => { window.__ryobot_ws.connected = true; });
                ws.addEventListener("message", () => { window.__ryobot_ws.messages++; });
                return ws;
            };
        """)

        # Navigate
        try:
            page.goto(url, wait_until="networkidle", timeout=15000)
        except Exception as exc:
            sections.append("=== Page State ===")
            sections.append(f"  url: {url}")
            sections.append(f"  ERROR: Failed to load page — {exc}")
            browser.close()
            return "\n\n".join(sections)

        # Wait for dynamic content
        page.wait_for_timeout(wait_ms)

        # Collect WS state
        try:
            ws_state = page.evaluate("window.__ryobot_ws")
            ws_connected = bool(ws_state.get("connected"))
            ws_message_count = int(ws_state.get("messages", 0))
            ws_url = str(ws_state.get("url", ""))
        except Exception:
            ws_connected = False

        # Page state
        title = page.title()
        current_url = page.url
        sections.append("=== Page State ===")
        sections.append(f'  title: "{title}"')
        sections.append(f"  url: {current_url}")

        # Console logs
        sections.append(f"\n=== Console Logs ({len(console_messages)} entries) ===")
        if console_messages:
            for msg in console_messages:
                level = msg["level"].upper()
                sections.append(f"  [{level}] {msg['text']}")
        else:
            sections.append("  (no console output)")

        # WebSocket
        sections.append("\n=== WebSocket ===")
        sections.append(f"  connected: {ws_connected}")
        sections.append(f"  messages received: {ws_message_count}")
        sections.append(f"  endpoint: {ws_url or '(not detected)'}")

        # DOM summary
        sections.append("\n=== DOM Summary ===")
        try:
            leaflet_container = page.evaluate("""
                (() => {
                    const mapEl = document.querySelector('.leaflet-container');
                    if (!mapEl) return { found: false };
                    const markers = document.querySelectorAll('.leaflet-marker-icon');
                    const popups = document.querySelectorAll('.leaflet-popup');
                    const statusBar = document.getElementById('status');
                    return {
                        found: true,
                        size: mapEl.clientWidth + 'x' + mapEl.clientHeight,
                        markerCount: markers.length,
                        popupCount: popups.length,
                        statusText: statusBar ? statusBar.textContent : '',
                        visibleText: document.body.innerText.substring(0, 500),
                    };
                })()
            """)
            if leaflet_container.get("found"):
                sections.append(f"  Leaflet map container: found ({leaflet_container.get('size', 'unknown')})")
                sections.append(f"  Station markers rendered: {leaflet_container.get('markerCount', 0)}")
                sections.append(f"  Popups bound: {leaflet_container.get('popupCount', 0)}")
                status_text = leaflet_container.get("statusText", "")
                sections.append(f'  Status bar: "{status_text}"')
                visible = leaflet_container.get("visibleText", "")
                if visible:
                    sections.append(f"  Visible text preview: {visible[:300]}")
            else:
                sections.append("  Leaflet map container: NOT FOUND — map may not have initialized")
        except Exception as exc:
            sections.append(f"  ERROR collecting DOM: {exc}")

        # Network
        sections.append(f"\n=== Network ({len(network_failures)} failures) ===")
        if network_failures:
            for nf in network_failures:
                sections.append(f"  {nf['method']} {nf['url']} → {nf['status']}")
        else:
            sections.append("  (no request failures detected)")

        # JS errors
        js_errors = [m for m in console_messages if m["level"] == "error"]
        sections.append(f"\n=== JS Errors ({len(js_errors)}) ===")
        if js_errors:
            for i, err in enumerate(js_errors, 1):
                sections.append(f"  {i}. {err['text']}")
        else:
            sections.append("  (none)")

        browser.close()

    # Overall verdict
    has_errors = bool(js_errors) or bool(network_failures)
    sections.insert(0, f"=== Diagnostic Report for {url} ===")
    sections.append(f"\n=== Verdict ===\n  {'ISSUES FOUND' if has_errors else 'All checks passed'}")
    if has_errors:
        sections[-1] += f" — {len(js_errors)} JS error(s), {len(network_failures)} network failure(s)"

    return "\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless page diagnostics")
    parser.add_argument("--url", default="http://localhost:8000", help="Target URL")
    parser.add_argument("--wait-ms", type=int, default=3000, help="Extra wait after load (ms)")
    args = parser.parse_args()
    report = _collect_diagnostics(args.url, args.wait_ms)
    print(report)
    # Exit non-zero if issues found so the skill can detect problems
    if "ISSUES FOUND" in report:
        sys.exit(2)


if __name__ == "__main__":
    main()
