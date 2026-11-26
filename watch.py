#!/usr/bin/env python3
"""
Hielo & Aventura - Safari Azul (22/12/2026 09:00) availability watcher.
Modelled after ChearinL/cascanueces-watch (Playwright + ntfy.sh + GitHub Actions).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

BUY_URL = "https://hieloyaventura.com/excursiones/comprar/safari-azul"
PRODUCT_URL = "https://hieloyaventura.com/excursiones/safari-azul"

TARGET_DAY = 22
TARGET_MONTH = 12
TARGET_YEAR = 2026
TARGET_DATE_STR = f"{TARGET_DAY:02d}/{TARGET_MONTH:02d}/{TARGET_YEAR}"
TARGET_TIME_RE = re.compile(r"\b0?9:00\b")

INTERVAL_SEC = 180
JITTER_SEC = 30
MAX_MINUTES_DEFAULT = 55
STATE_FILE_DEFAULT = "state.json"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"Warning: failed to read {path}: {exc}")
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def send_ntfy(title: str, message: str, priority: str = "urgent", tags: str = "ticket") -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log("NTFY_TOPIC not set; skipping ntfy.sh push notification.")
        return
    url = f"https://ntfy.sh/{topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Click": BUY_URL,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"ntfy.sh sent ({resp.status}): {title}")
    except urllib.error.URLError as exc:
        log(f"Warning: failed to send ntfy.sh alert: {exc}")


def send_macos_alert(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"',
            ],
            check=False,
        )
        subprocess.run(["say", "Safari Azul 9 AM tickets available"], check=False)
    except Exception as exc:
        log(f"Warning: macOS alert failed: {exc}")


def notify_available(found_modes: list[dict[str, Any]]) -> None:
    lines = [f"Safari Azul {TARGET_DATE_STR} 09:00 자리 오픈!"]
    for item in found_modes:
        mode_label = "이동 포함(Con traslado)" if item["transfer"] else "이동 미포함(Sin traslado)"
        opts = ", ".join(item["options"])
        spots = item.get("spots") or ""
        lines.append(f"- {mode_label}: {opts} {spots}".strip())
    lines.append(f"예약 링크: {BUY_URL}")
    msg = "\n".join(lines)
    title = f"Safari Azul {TARGET_DATE_STR} 09:00 Available!"
    send_ntfy(title=title, message=msg, priority="urgent", tags="ticket,rotating_light")
    send_macos_alert(title=title, message=msg)


def _click_fecha_and_select_date(page: Page) -> None:
    fecha_candidates = [
        page.get_by_label(re.compile(r"fecha", re.I)),
        page.locator("xpath=//label[contains(translate(., 'FECHA', 'fecha'), 'fecha')]/following::input[1]"),
        page.locator("xpath=//*[contains(translate(text(), 'FECHA', 'fecha'), 'fecha')]/following::input[1]"),
    ]
    fecha_input = None
    for cand in fecha_candidates:
        if cand.count() > 0 and cand.first.is_visible():
            fecha_input = cand.first
            break

    if fecha_input is None:
        raise RuntimeError("Could not locate 'Fecha' date input on booking page.")

    current_val = (fecha_input.input_value() or "").strip()
    if current_val == TARGET_DATE_STR:
        return

    # Open Vuetify datepicker menu if not already expanded
    if fecha_input.get_attribute("aria-expanded") != "true":
        fecha_input.click()
        page.wait_for_timeout(600)

    # Wait for visible Vuetify 2/3 date picker container
    picker = page.locator(
        ".v-date-picker:visible, .v-picker:visible, "
        ".v-menu__content.menuable__content__active:visible, .v-overlay__content:visible"
    ).filter(has=page.locator("button")).last
    picker.wait_for(state="visible", timeout=10000)

    # Navigate calendar header to December 2026 (works even if month and year are on separate lines)
    month_re = re.compile(r"(diciembre|december|\bdic\b)", re.I)
    for _ in range(14):
        picker_text = picker.inner_text()
        if month_re.search(picker_text) and str(TARGET_YEAR) in picker_text:
            break

        next_btns = [
            picker.locator("button[aria-label*='next' i], button[aria-label*='siguiente' i]").first,
            picker.locator("button:has(.mdi-chevron-right), button:has(.fa-chevron-right)").first,
            picker.locator(".v-date-picker-controls button:not([disabled]), .v-date-picker-header button:not([disabled])").last,
        ]
        clicked = False
        for nb in next_btns:
            if nb.count() > 0 and nb.is_visible() and nb.is_enabled():
                nb.click()
                page.wait_for_timeout(400)
                clicked = True
                break
        if not clicked:
            break

    # Click day 22 regardless of element tag (<button>, <td>, <div>, <span>)
    day_locs = picker.get_by_text(re.compile(rf"^{TARGET_DAY}$"))
    for i in range(day_locs.count()):
        el = day_locs.nth(i)
        if not el.is_visible():
            continue
        cls = el.get_attribute("class") or ""
        parent_cls = el.locator("xpath=..").get_attribute("class") or ""
        if "disabled" in cls or "disabled" in parent_cls:
            continue
        if el.get_attribute("disabled") is not None:
            continue
        el.click()
        page.wait_for_timeout(1200)
        return

    snippet = " | ".join(picker.inner_text().splitlines())
    raise RuntimeError(f"Could not find enabled day '{TARGET_DAY}' in datepicker. Full text: {snippet[:500]}")


def _set_transfer_toggle(page: Page, include_transfer: bool) -> None:
    toggle_candidates = [
        page.get_by_role("switch", name=re.compile(r"traslado", re.I)),
        page.get_by_role("checkbox", name=re.compile(r"traslado", re.I)),
        page.locator("xpath=//*[contains(translate(., 'TRASLADO', 'traslado'), 'traslado')]//input[@type='checkbox']"),
        page.locator("xpath=//*[contains(translate(., 'TRASLADO', 'traslado'), 'traslado')]/following::input[@type='checkbox'][1]"),
        page.locator("xpath=//*[contains(translate(., 'TRASLADO', 'traslado'), 'traslado')]/preceding::input[@type='checkbox'][1]"),
        page.locator(".v-input--switch input[type='checkbox']").first,
    ]
    for cand in toggle_candidates:
        if cand.count() > 0:
            el = cand.first
            try:
                checked = el.is_checked()
            except Exception:
                checked = el.get_attribute("aria-checked") == "true"
            if checked != include_transfer:
                el.click(force=True)
                page.wait_for_timeout(1000)
            return
    log("Warning: could not explicitly verify transfer toggle state; continuing.")


def _read_hora_options(page: Page) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []

    # Case 1: Vuetify v-select / combobox near Hora
    trigger_candidates = [
        page.locator("xpath=//*[contains(translate(text(), 'HORA', 'hora'), 'hora')]/ancestor::*[contains(@class, 'v-input')][1]"),
        page.locator("xpath=//label[contains(translate(., 'HORA', 'hora'), 'hora')]/ancestor::*[contains(@class, 'v-select') or contains(@class, 'v-input')][1]"),
        page.get_by_role("combobox", name=re.compile(r"hora", re.I)),
        page.locator(".v-select").filter(has_text=re.compile(r"hora", re.I)).first,
    ]
    for trig in trigger_candidates:
        if trig.count() > 0 and trig.first.is_visible():
            trig.first.click()
            page.wait_for_timeout(600)
            menu_items = page.locator(
                ".v-overlay__content:visible .v-list-item, .v-menu__content.menuable__content__active .v-list-item, "
                ".v-select-list .v-list-item, "
                "[role='listbox'] [role='option'], "
                "[role='option']"
            )
            for i in range(menu_items.count()):
                item = menu_items.nth(i)
                if not item.is_visible():
                    continue
                text = (item.inner_text() or "").strip()
                if re.search(r"\b\d{1,2}:\d{2}\b", text):
                    classes = item.get_attribute("class") or ""
                    aria_dis = item.get_attribute("aria-disabled") == "true"
                    disabled = (
                        aria_dis
                        or "v-list-item--disabled" in classes
                        or bool(re.search(r"agotad|sin cupo|no disponible", text, re.I))
                    )
                    options.append({"text": text, "disabled": disabled})
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            if options:
                return options

    # Case 2: Fallback native <select>
    select_candidates = [
        page.get_by_label(re.compile(r"hora", re.I)),
        page.locator("select[name*='hora' i], select[id*='hora' i]"),
    ]
    for cand in select_candidates:
        if cand.count() > 0 and cand.first.is_visible():
            opt_locs = cand.first.locator("option")
            for i in range(opt_locs.count()):
                o = opt_locs.nth(i)
                text = (o.inner_text() or "").strip()
                if not text or re.search(r"seleccion|eleg|hora", text, re.I):
                    continue
                disabled = o.is_disabled() or bool(re.search(r"agotad|sin cupo|no disponible", text, re.I))
                options.append({"text": text, "disabled": disabled})
            return options

    return options


def check_once(transfer_mode: str = "with") -> dict[str, Any]:
    modes_to_check = [True, False] if transfer_mode == "both" else ([True] if transfer_mode == "with" else [False])
    results: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="es-AR", viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_default_timeout(20000)

        try:
            page.goto(BUY_URL, wait_until="networkidle", timeout=35000)
        except PlaywrightTimeoutError:
            page.goto(BUY_URL, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(1500)

        if "safari-azul" not in page.url:
            page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

        for include_transfer in modes_to_check:
            _set_transfer_toggle(page, include_transfer)
            _click_fecha_and_select_date(page)
            opts = _read_hora_options(page)
            available_opts = [o["text"] for o in opts if not o["disabled"]]
            target_hit = any(TARGET_TIME_RE.search(o["text"]) and not o["disabled"] for o in opts)

            spots_match = re.search(r"\d+\s+lugares\s+disponibles", page.locator("body").inner_text(), re.I)
            spots_text = spots_match.group(0) if spots_match else ""

            results.append(
                {
                    "transfer": include_transfer,
                    "key": "with_transfer" if include_transfer else "without_transfer",
                    "options": available_opts,
                    "all_options": opts,
                    "target_available": target_hit,
                    "spots": spots_text,
                }
            )

        browser.close()

    return {"date": TARGET_DATE_STR, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Safari Azul 22/12/2026 09:00 availability")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--max-minutes", type=int, default=MAX_MINUTES_DEFAULT, help="Max run duration in minutes")
    parser.add_argument("--interval", type=int, default=INTERVAL_SEC, help="Seconds between checks")
    parser.add_argument("--jitter", type=int, default=JITTER_SEC, help="Max random jitter seconds")
    parser.add_argument("--state-file", type=str, default=STATE_FILE_DEFAULT, help="Path to state.json")
    parser.add_argument(
        "--transfer-mode",
        choices=["both", "with", "without"],
        default=os.environ.get("TRANSFER_MODE", "with"),
        help="Check with transfer, without transfer, or both",
    )
    args = parser.parse_args()

    state_path = Path(args.state_file)
    state = load_state(state_path)
    start_ts = time.monotonic()
    consecutive_not_found = 0
    consecutive_errors = 0

    while True:
        try:
            data = check_once(transfer_mode=args.transfer_mode)
            consecutive_errors = 0

            if not any(r["all_options"] for r in data["results"]):
                consecutive_not_found += 1
                log(f"Warning: no Hora options parsed for {TARGET_DATE_STR} (streak={consecutive_not_found}).")
                if args.once or consecutive_not_found >= 3:
                    return 2
            else:
                consecutive_not_found = 0

            newly_available = []
            for r in data["results"]:
                key = r["key"]
                prev_status = state.get(key, "UNAVAILABLE")
                curr_status = "AVAILABLE" if r["target_available"] else "UNAVAILABLE"
                log(
                    f"[{key}] {TARGET_DATE_STR} -> options={r['options']} | "
                    f"09:00={curr_status} (prev={prev_status})"
                )
                state[key] = curr_status
                if curr_status == "AVAILABLE" and prev_status != "AVAILABLE":
                    newly_available.append(r)

            save_state(state_path, state)

            if newly_available:
                notify_available(newly_available)
                return 10

        except Exception as exc:
            consecutive_errors += 1
            log(f"Error during check (streak={consecutive_errors}): {exc}")
            if args.once:
                return 3
            if consecutive_errors >= 5:
                send_ntfy(
                    title="Safari Azul watcher error",
                    message=f"5 consecutive failures checking {TARGET_DATE_STR} 09:00: {exc}",
                    priority="default",
                    tags="warning",
                )
                return 3

        if args.once:
            return 0

        elapsed_min = (time.monotonic() - start_ts) / 60.0
        if elapsed_min >= args.max_minutes:
            log(f"Reached max-minutes ({args.max_minutes}); exiting cleanly.")
            return 0

        sleep_sec = args.interval + random.randint(0, max(0, args.jitter))
        log(f"Sleeping {sleep_sec}s...")
        time.sleep(sleep_sec)


if __name__ == "__main__":
    sys.exit(main())
