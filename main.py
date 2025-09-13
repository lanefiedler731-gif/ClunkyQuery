#!/usr/bin/env python3
# agent_browser.py
# Minimal web agent: LLM plans actions in JSON, Selenium executes them in a headed browser.

import os
import urllib.parse
import re
import json
import time
import argparse
import threading
import socket
from datetime import datetime
import textwrap
from typing import Any, Dict, List, Optional

import requests
import sys
import shutil
import subprocess

# Selenium
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Auto-manage driver (avoids version hell)
try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAVE_WDM = True
except Exception:
    HAVE_WDM = False

# OpenAI-compatible provider endpoints (used if --endpoint not provided)
PROVIDER_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "together": "https://api.together.xyz/v1",
}

# Model and API key may be provided via environment variables, but no hardcoded secrets.
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
DEFAULT_API_KEY = os.environ.get("LLM_API_KEY")  # No hardcoded fallback

SYSTEM_TOOLING = """You are a web automation planner. Plan and execute ONE action at a time based on the latest observation. After each action, you will receive the newest page text or any error, plus auto-detected links and visit progress. Prefer to leverage multiple searches and visit several promising results to gather evidence before finishing. Output ONLY a JSON object with this schema:

{
  "actions": [
    // Exactly one next step to perform (or just {"type":"done"})
    // Supported actions:
    // 1) {"type":"open_url","url":"https://..."}
    // 2) {"type":"type","selector":"css selector","text":"query","submit":true|false}
    // 3) {"type":"click","selector":"css selector or link text"}
    // 4) {"type":"wait_for","selector":"css selector","timeout":10}
    // 5) {"type":"scroll","px":1200}
    // 6) {"type":"scrape","selector":"css selector or 'body'","max_chars":2000}
    // 7) {"type":"extract_links","selector":"a","limit":10}
    // 8) {"type":"screenshot","path":"screenshot.png"}
    // 10) {"type":"back"}
    // 9) {"type":"done"}
  ],
  "notes": "brief planning notes if needed"
}

Rules:
- Return exactly one action per turn. If the goal is satisfied, return {"type":"done"}.
- Default search engine: DuckDuckGo (https://duckduckgo.com). Avoid Google unless explicitly requested due to rate limits.
 - Prefer generic selectors. For DuckDuckGo: input[name='q'] to search; then click result titles (e.g., 'h2 a'). If 'h2 a' fails, try 'a[data-testid="result-title-a"]'.
- Click result titles, not random containers.
- Use 'scrape' for main content (main, article, #content). For a quick what's-visible snapshot, use selector "viewport".
- Stay strictly on-topic. Avoid logins, sign-ups, and ads.
- On search pages, prefer 'extract_links' and then click the top unseen results across subsequent turns. Use 'back' to return to results and follow additional unseen links.
- If results look weak, try another search (refine keywords, use site: filters) with 'open_url' to the search engine and 'type' your query, then continue exploring.
- If a selector fails, propose a different one next turn rather than guessing wildly.
- Never propose two 'scrape' actions in a row. If a scrape returns little/empty content, next try a small 'wait_for' on a stable selector, 'extract_links', or a 'screenshot'.
- When the user goal is satisfied, end with {"type":"done"}.

Anti-spam constraints:
- Do not repeat the same failed action (same type+selector/url) more than twice. If an action fails twice, switch strategy (e.g., use 'extract_links', 'wait_for', or a different selector/XPath).
- For typing into editors, if 'input' fails, consider 'textarea' or contenteditable editors; avoid re-trying the exact same selector repeatedly.
- Avoid login/sign-up flows unless explicitly asked by the user.
"""

SUMMARY_SYSTEM = """
You are a concise news analyst. Produce a helpful, on-topic roundup of findings relevant to the user's goal.

Requirements:
- Focus on substantive content, not process/errors.
- 5–8 crisp bullets summarizing key developments, with sources.
- Include 2–5 direct links with short labels (Source — URL).
- If data is sparse, infer themes from available links/text and suggest 2–3 follow-up queries.
- Do NOT mention automation steps, retries, or duplicates.
"""

def strip_code_fences(s: str) -> str:
    # Remove ```json ... ``` wrappers if model adds them
    fence = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
    return fence.sub("", s).strip()

# Relevance filtering helpers
STOPWORDS = {
    'the','a','an','and','or','but','for','to','of','on','in','at','by','with','as',
    'is','are','was','were','be','been','from','that','this','it','its','you','your',
    'we','our','they','their','about','over','into','out','more','most','can','will',
    'may','might','should','would','could','if','than','then','so','such','up','down',
}

def extract_keywords(text: str) -> List[str]:
    words = re.findall(r"[a-zA-Z0-9_+\-]{3,}", (text or "").lower())
    kws = [w for w in words if w not in STOPWORDS]
    seen = set()
    out: List[str] = []
    for w in kws:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:30]

def filter_text_by_keywords(text: str, keywords: List[str], mode: str = 'loose', max_lines: int = 80) -> str:
    if mode == 'off' or not keywords or not text:
        return text
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    kept: List[str] = []
    need = 2 if mode == 'strict' else 1
    kw_set = set(keywords)
    for ln in lines:
        ln_low = ln.lower()
        # Always keep headings
        if ln.startswith('== ') or ln.startswith('# '):
            kept.append(ln)
            continue
        hits = sum(1 for k in kw_set if k in ln_low)
        if hits >= need:
            kept.append(ln)
        if len(kept) >= max_lines:
            break
    if not kept:
        kept = lines[: min(10, len(lines))]
    return "\n".join(kept)

def filter_links_by_keywords(links: List[Dict[str,str]], keywords: List[str], mode: str = 'loose', max_keep: int = 10) -> List[Dict[str,str]]:
    if mode == 'off' or not keywords or not links:
        return links[:max_keep]
    need = 2 if mode == 'strict' else 1
    kw_set = set(keywords)
    matched: List[Dict[str,str]] = []
    for lk in links:
        text = (lk.get('text') or '').lower()
        href = (lk.get('href') or '').lower()
        hits = sum(1 for k in kw_set if k in text or k in href)
        if hits >= need:
            matched.append(lk)
        if len(matched) >= max_keep:
            break
    if not matched:
        return links[:max_keep]
    return matched[:max_keep]

class LLMClient:
    def __init__(self, api_key: str, model: str, endpoint: str):
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.url = f"{self.endpoint}/chat/completions"

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        resp = requests.post(self.url, headers=headers, json=payload, timeout=120)
        if not resp.ok:
            # Provide clearer diagnostics for common misconfigurations
            detail = ""
            try:
                err = resp.json()
                detail = json.dumps(err)
            except Exception:
                detail = resp.text
            raise RuntimeError(
                "LLM request failed. "
                f"status={resp.status_code} endpoint={self.endpoint} model={self.model} detail={detail}"
            )
        data = resp.json()
        return data["choices"][0]["message"]["content"]

def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


class BrowserAgent:
    def __init__(self, headless: bool = False, binary: Optional[str] = None, detach: bool = True, nav_stop_seconds: float = 2.0, debug_port: Optional[int] = None):
        self.nav_stop_seconds = float(nav_stop_seconds) if nav_stop_seconds is not None else 0.0
        self.clicked_hrefs: set[str] = set()
        self.visited_urls: set[str] = set()
        def pick_binary(explicit: Optional[str]) -> Optional[str]:
            if explicit and os.path.exists(explicit):
                return explicit
            # Try common names/paths
            candidates = [
                explicit,
                shutil.which("google-chrome-stable"),
                shutil.which("google-chrome"),
                shutil.which("chromium-browser"),
                shutil.which("chromium"),
                "/usr/bin/google-chrome-stable",
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
            ]
            for path in candidates:
                if path and os.path.exists(path):
                    return path
            return None

        # If no GUI available and headed requested, fallback to headless.
        # Only apply DISPLAY/WAYLAND checks on Linux; Windows/macOS don't set these in normal GUI sessions.
        if sys.platform.startswith("linux"):
            no_display = not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
        else:
            no_display = False
        requested_headless = bool(headless)
        effective_headless = requested_headless or no_display

        opts = Options()
        # If we plan to stop loads manually, don't wait for full loads
        if self.nav_stop_seconds and self.nav_stop_seconds > 0:
            try:
                opts.set_capability('pageLoadStrategy', 'none')
            except Exception:
                pass
        chosen_binary = pick_binary(binary)
        if chosen_binary:
            opts.binary_location = chosen_binary
        if effective_headless:
            opts.add_argument("--headless=new")
            # Set a reasonable viewport for headless
            opts.add_argument("--window-size=1280,900")
        else:
            opts.add_argument("--start-maximized")
        # Stability flags for containers/CI
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # Help avoid DevToolsActivePort issues; ensure per-instance unique port
        try:
            port = int(debug_port) if debug_port else _find_free_port()
            opts.add_argument(f"--remote-debugging-port={port}")
        except Exception:
            # If port selection fails, proceed without setting it
            pass

        # Resolve a matching ChromeDriver. Prefer webdriver_manager with exact major match; otherwise Selenium Manager.
        service = None
        if HAVE_WDM:
            try:
                # Determine browser type and major version for precise matching
                try:
                    from webdriver_manager.core.utils import ChromeType
                except Exception:
                    ChromeType = None  # type: ignore

                browser_major = None
                if chosen_binary and os.path.exists(chosen_binary):
                    try:
                        out = subprocess.check_output([chosen_binary, "--version"], text=True).strip()
                        m = re.search(r"(\d+)\.", out)
                        if m:
                            browser_major = int(m.group(1))
                    except Exception:
                        pass

                ctype = None
                if ChromeType is not None:
                    ctype = ChromeType.CHROMIUM if (chosen_binary and "chromium" in os.path.basename(chosen_binary)) else ChromeType.GOOGLE

                if browser_major and ctype is not None:
                    driver_path = ChromeDriverManager(version=str(browser_major), chrome_type=ctype).install()
                    service = Service(driver_path)
                elif ctype is not None:
                    driver_path = ChromeDriverManager(chrome_type=ctype).install()
                    service = Service(driver_path)
                else:
                    # Fallback: let Selenium Manager handle it
                    service = None
            except Exception as e:
                print(f"[WARN] webdriver_manager failed to resolve driver ({e}). Using Selenium Manager.", flush=True)
                service = None

        def log_start(mode: str, extra: str = ""):
            info = [f"mode={mode}"]
            if chosen_binary:
                info.append(f"binary={chosen_binary}")
            if extra:
                info.append(extra)
            print("[INFO] Starting Chrome(" + ", ".join(info) + ")", flush=True)

        # Try to start; if headed fails, retry headless as fallback
        try:
            log_start("headless" if effective_headless else "headed")
            if service is not None:
                self.driver = webdriver.Chrome(service=service, options=opts)
            else:
                # Let Selenium Manager auto-resolve a matching driver
                self.driver = webdriver.Chrome(options=opts)
        except Exception as first_err:
            if not effective_headless:
                # Retry in headless mode
                try:
                    print(f"[WARN] Headed Chrome failed ({first_err}). Retrying headless...", flush=True)
                    opts.add_argument("--headless=new")
                    opts.add_argument("--window-size=1280,900")
                    log_start("headless", "fallback=from_headed")
                    if service is not None:
                        self.driver = webdriver.Chrome(service=service, options=opts)
                    else:
                        self.driver = webdriver.Chrome(options=opts)
                except Exception as second_err:
                    raise RuntimeError(
                        "Failed to start Chrome in both headed and headless modes. "
                        f"First error: {first_err}; Second error: {second_err}. "
                        "If Chrome/Chromium is not installed, set CHROME_BINARY to its path or install it."
                    )
            else:
                raise

        if detach:
            try:
                # Keep the window open after script unless explicitly quit
                self.driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "allow"})
            except Exception:
                pass
        self.wait = WebDriverWait(self.driver, 15)

    def quit(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    # Helpers

    def _normalize_url(self, url: str) -> str:
        try:
            # Normalize scheme/host lowercase, drop fragment, clean trailing slash
            p = urllib.parse.urlsplit(url)
            scheme = (p.scheme or '').lower()
            netloc = (p.netloc or '').lower()
            path = p.path or '/'
            if path != '/' and path.endswith('/'):
                path = path[:-1]
            query = (('?' + p.query) if p.query else '')
            return f"{scheme}://{netloc}{path}{query}"
        except Exception:
            return url

    def _is_dup_exempt(self, url: str) -> bool:
        """Return True if this URL should never be considered a duplicate.
        Exempts all DuckDuckGo hosts (e.g., duckduckgo.com, lite.duckduckgo.com).
        """
        try:
            p = urllib.parse.urlsplit(url)
            host = (p.netloc or '').lower()
            return host.endswith('duckduckgo.com')
        except Exception:
            return False

    def _xpath_literal(self, s: str) -> str:
        # Build a safe XPath string literal for any content
        if "'" not in s:
            return f"'{s}'"
        if '"' not in s:
            return f'"{s}"'
        # String contains both single and double quotes: use concat('..', "'", '..')
        parts = s.split("'")
        out = ["concat("]
        for idx, part in enumerate(parts):
            if idx > 0:
                out.append(',"\'",')
            out.append(f"'{part}'")
        out.append(")")
        return "".join(out)

    def _resolve_locator(self, selector: str):
        sel = (selector or '').strip()
        if not sel:
            raise NoSuchElementException("Empty selector")

        # Explicit XPath syntax
        if sel.startswith('xpath='):
            return (By.XPATH, sel[len('xpath='):])
        if sel.startswith('//') or sel.startswith('.//'):
            return (By.XPATH, sel)

        # Playwright-style :has-text()
        m = re.search(r"^([a-zA-Z0-9_*\-]+)?\s*:\s*has-text\((['\"])\s*(.*?)\s*\2\)\s*$", sel)
        if m:
            tag = (m.group(1) or '*').strip()
            text_val = m.group(3)
            lit = self._xpath_literal(text_val)
            xp = f"//{tag}[contains(normalize-space(.), {lit})]"
            return (By.XPATH, xp)

        # Plain visible text selector (e.g., "Continue with Google")
        # If it doesn't look like a CSS selector, treat as a text match on clickable elements
        if not re.search(r"[\.#\[:]", sel):
            lit = self._xpath_literal(sel)
            xp = (
                "//button[contains(normalize-space(.), {lit})] | "
                "//a[contains(normalize-space(.), {lit})] | "
                "//*[@role='button' and contains(normalize-space(.), {lit})] | "
                "//input[((@type='button' or @type='submit') and contains(@value, {lit}))]"
            ).format(lit=lit)
            return (By.XPATH, xp)

        # Default: CSS selector
        return (By.CSS_SELECTOR, sel)

    def _by_selector(self, selector: str):
        by, val = self._resolve_locator(selector)
        # Wait for presence and visibility
        try:
            el = self.wait.until(EC.presence_of_element_located((by, val)))
            el = self.wait.until(EC.visibility_of_element_located((by, val)))
            return el
        except Exception:
            # As a fallback, try partial link text for anchors if the selector looks like plain text
            try:
                return self.driver.find_element(By.PARTIAL_LINK_TEXT, selector)
            except Exception:
                pass
        raise NoSuchElementException(f"Selector not found: {selector}")

    def _maybe_stop_loading(self):
        if self.nav_stop_seconds and self.nav_stop_seconds > 0:
            try:
                time.sleep(self.nav_stop_seconds)
                self.driver.execute_script("window.stop();")
            except Exception:
                pass

    def open_url(self, url: str):
        # Prevent revisiting the same absolute URL
        norm = self._normalize_url(url)
        exempt = self._is_dup_exempt(norm)
        # Local agent-level block (skip if exempt)
        if (not exempt) and (norm in self.visited_urls):
            raise ValueError(f"URL already visited: {norm}")
        # Team-wide block (skip if exempt)
        try:
            with GLOBAL_VISITED_LOCK:
                if (not exempt) and (norm in GLOBAL_VISITED_URLS):
                    raise ValueError(f"URL already visited by another agent: {norm}")
        except NameError:
            # Globals may not exist in some contexts; ignore
            pass
        self.driver.get(url)
        # Optionally stop loading after a short delay
        self._maybe_stop_loading()
        # Try to auto-accept common consent dialogs (e.g., Google)
        try:
            cur = self.driver.current_url
            if "google." in cur or "google." in url:
                candidates = [
                    "button#L2AGLb",  # EU consent "I agree"
                    "button[aria-label='Accept all']",
                    "button[aria-label='I agree']",
                    "form[action][method] button[type='submit']",
                ]
                for sel in candidates:
                    try:
                        el = self.driver.find_element(By.CSS_SELECTOR, sel)
                        if el.is_displayed() and el.is_enabled():
                            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                            time.sleep(0.1)
                            el.click()
                            break
                    except Exception:
                        continue
        except Exception:
            pass
        # Record visited URL
        try:
            cur = self.driver.current_url
            if cur:
                cur_n = self._normalize_url(cur)
                self.visited_urls.add(cur_n)
                try:
                    with GLOBAL_VISITED_LOCK:
                        GLOBAL_VISITED_URLS.add(cur_n)
                except NameError:
                    pass
        except Exception:
            pass

    def type(self, selector: str, text: str, submit: bool = False):
        # Wait for element to be interactable, then type. If not found, try common fallbacks.
        try:
            by, val = self._resolve_locator(selector)
            el = self.wait.until(EC.element_to_be_clickable((by, val)))
            try:
                el.clear()
            except Exception:
                pass
            el.send_keys(text)
            if submit:
                el.send_keys(Keys.ENTER)
            return
        except Exception:
            pass

        # Fallbacks: try visible inputs/textareas/contenteditable fields
        candidates: List[Any] = []
        try:
            candidates.extend(self.driver.find_elements(By.CSS_SELECTOR, "input[type='search'], input[type='text'], textarea"))
        except Exception:
            pass
        try:
            candidates.extend(self.driver.find_elements(By.XPATH, "//div[@contenteditable='true']"))
        except Exception:
            pass
        # Filter visible and enabled
        visible = [e for e in candidates if getattr(e, 'is_displayed', lambda: False)() and getattr(e, 'is_enabled', lambda: False)()]
        if not visible:
            raise NoSuchElementException(f"Type target not found: {selector}")
        target = visible[0]
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
        except Exception:
            pass
        try:
            target.click()
        except Exception:
            pass
        try:
            target.clear()
        except Exception:
            pass
        target.send_keys(text)
        if submit:
            target.send_keys(Keys.ENTER)

    def click(self, selector: str):
        by, val = self._resolve_locator(selector)
        el = self.wait.until(EC.element_to_be_clickable((by, val)))
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass
        time.sleep(0.1)
        # Record link href if available (element or closest anchor)
        try:
            href = el.get_attribute('href')
            if not href:
                href = self.driver.execute_script(
                    "return arguments[0].closest && arguments[0].closest('a') ? arguments[0].closest('a').href : null;",
                    el,
                )
            if href:
                norm = self._normalize_url(href)
                # Disallow clicking a link we've already clicked or visited (unless exempt)
                exempt = self._is_dup_exempt(norm)
                blocked = (not exempt) and ((href in self.clicked_hrefs) or (norm in self.visited_urls))
                # Team-wide block
                try:
                    with GLOBAL_VISITED_LOCK:
                        blocked = blocked or ((not exempt) and (norm in GLOBAL_VISITED_URLS))
                except NameError:
                    pass
                if blocked:
                    raise ValueError(f"Link already visited: {norm}")
                self.clicked_hrefs.add(href)
        except Exception:
            pass
        el.click()
        # If navigation likely started, optionally stop loading quickly
        self._maybe_stop_loading()
        # Record visited URL post-click
        try:
            cur = self.driver.current_url
            if cur:
                cur_n = self._normalize_url(cur)
                self.visited_urls.add(cur_n)
                try:
                    with GLOBAL_VISITED_LOCK:
                        GLOBAL_VISITED_URLS.add(cur_n)
                except NameError:
                    pass
        except Exception:
            pass

    def back(self):
        self.driver.back()
        # brief settle time
        time.sleep(0.2)
        self._maybe_stop_loading()

    def wait_for(self, selector: str, timeout: int = 15):
        by, val = self._resolve_locator(selector)
        WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located((by, val)))

    def scroll(self, px: int = 1200):
        self.driver.execute_script(f"window.scrollBy(0, {px});")

    def scrape(self, selector: str = "body", max_chars: int = 2000) -> str:
        sel = (selector or "").lower()
        if sel in ("body", "document", "page", "viewport", "visible"):
            return self.scrape_visible(max_chars=max_chars)
        try:
            el = self._by_selector(selector)
            txt = el.text
        except Exception:
            # Fallback to viewport-visible content to avoid offscreen noise
            txt = self.scrape_visible(max_chars=max_chars)
        txt = re.sub(r"\s+\n", "\n", txt)
        txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
        if len(txt) > max_chars:
            txt = txt[:max_chars] + "…"
        return txt

    def scrape_visible(self, max_chars: int = 2000) -> str:
        items = self.driver.execute_script(
            r"""
            const WH = window.innerHeight, WW = window.innerWidth;
            function visible(el){
              const cs = getComputedStyle(el);
              if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity||'1') === 0) return false;
              const r = el.getBoundingClientRect();
              if (r.bottom <= 0 || r.top >= WH || r.right <= 0 || r.left >= WW) return false;
              const vert = Math.min(WH, r.bottom) - Math.max(0, r.top);
              const horiz = Math.min(WW, r.right) - Math.max(0, r.left);
              return vert >= 20 && horiz >= 20;
            }
            function norm(s){ return (s||'').replace(/\s+/g,' ').trim(); }
            const selectors = 'h1,h2,h3,h4,h5,h6,main,article,section,p,li,a,button,[role=button],[role=link]';
            const nodes = Array.from(document.querySelectorAll(selectors));
            const seen = new Set();
            const items = [];
            for (const el of nodes){
              if (!visible(el)) continue;
              let txt = norm(el.innerText || el.textContent || '');
              if (!txt || txt.length < 2) continue;
              const r = el.getBoundingClientRect();
              const tag = el.tagName.toLowerCase();
              const href = (tag === 'a' && el.href) ? el.href : null;
              const key = tag+':'+txt.slice(0,120)+':' + (href||'');
              if (seen.has(key)) continue;
              seen.add(key);
              items.push({tag, txt, href, top:r.top, left:r.left});
            }
            items.sort((a,b)=> a.top - b.top || a.left - b.left);
            return items;
            """
        )
        lines: List[str] = []
        def add_line(s: str):
            if s:
                lines.append(s)
        for it in items:
            tag = it.get("tag", "")
            txt = (it.get("txt") or "").strip()
            href = it.get("href")
            if not txt:
                continue
            if tag == "h1":
                add_line(f"== {txt} ==")
            elif tag == "h2":
                add_line(f"# {txt}")
            elif tag == "h3":
                add_line(f"## {txt}")
            elif tag in ("a",):
                if href:
                    add_line(f"• {txt} — {href}")
                else:
                    add_line(f"• {txt}")
            elif tag in ("button",):
                add_line(f"[button] {txt}")
            else:
                add_line(txt)
            if sum(len(x) + 1 for x in lines) >= max_chars:
                break
        out = "\n".join(lines)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        if len(out) > max_chars:
            out = out[:max_chars] + "…"
        return out

    def extract_links(self, selector: str = "a", limit: int = 10) -> List[Dict[str, str]]:
        items = self.driver.execute_script(
            r"""
            const WH = window.innerHeight, WW = window.innerWidth;
            function visible(el){
              const cs = getComputedStyle(el);
              if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity||'1') === 0) return false;
              const r = el.getBoundingClientRect();
              if (r.bottom <= 0 || r.top >= WH || r.right <= 0 || r.left >= WW) return false;
              const vert = Math.min(WH, r.bottom) - Math.max(0, r.top);
              const horiz = Math.min(WW, r.right) - Math.max(0, r.left);
              return vert >= 12 && horiz >= 12;
            }
            function norm(s){ return (s||'').replace(/\s+/g,' ').trim(); }
            const nodes = Array.from(document.querySelectorAll('a[href^="http"]'));
            const items = [];
            for (const el of nodes){
              if (!visible(el)) continue;
              const txt = norm(el.innerText || el.textContent || '');
              const href = el.href;
              if (!href) continue;
              const r = el.getBoundingClientRect();
              items.push({text: txt, href: href, top: r.top, left: r.left});
            }
            items.sort((a,b)=> a.top - b.top || a.left - b.left);
            return items;
            """
        )
        links: List[Dict[str, str]] = []
        for it in items:
            href = it.get("href")
            text = (it.get("text") or "").strip()
            if href and href.startswith("http"):
                norm = self._normalize_url(href)
                if self._is_dup_exempt(norm):
                    clicked = False
                else:
                    clicked_local = href in getattr(self, 'clicked_hrefs', set())
                    visited_local = norm in getattr(self, 'visited_urls', set())
                    visited_global = False
                    try:
                        with GLOBAL_VISITED_LOCK:
                            visited_global = norm in GLOBAL_VISITED_URLS
                    except NameError:
                        pass
                    clicked = clicked_local or visited_local or visited_global
                # We keep only 'text' and 'href' public, but include 'clicked' for display and filtering
                links.append({"text": text[:120], "href": href, "clicked": clicked})
            if len(links) >= limit:
                break
        return links

    def screenshot(self, path: str = "screenshot.png") -> str:
        self.driver.save_screenshot(path)
        return path

def _extract_balanced_json_object(s: str) -> Optional[str]:
    s = s.strip()
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        else:
            if ch in ('"', "'"):
                in_str = True
                quote = ch
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return s[start:i+1]
    return None


def _strip_js_comments(s: str) -> str:
    # Remove // line comments and /* */ block comments safely
    s = re.sub(r"(^|\s)//.*$", "", s, flags=re.MULTILINE)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    return s


def _parse_model_json_loose(raw: str) -> Dict[str, Any]:
    """Parse model output into a JSON object with resiliency.

    Tries strict JSON first. If that fails, removes JS comments and extracts
    the first balanced JSON object. As a last resort, attempts Python literal
    eval after mapping true/false/null to True/False/None.
    """
    cleaned = strip_code_fences(raw)
    # Fast path: strict JSON
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Remove comments and try again on the first balanced object
    no_comments = _strip_js_comments(cleaned)
    candidate = _extract_balanced_json_object(no_comments) or no_comments.strip()
    try:
        return json.loads(candidate)
    except Exception:
        pass

    # Last resort: tolerant Python-literal parsing
    try:
        import ast
        py_like = re.sub(r"(?<![A-Za-z0-9_])true(?![A-Za-z0-9_])", "True", candidate)
        py_like = re.sub(r"(?<![A-Za-z0-9_])false(?![A-Za-z0-9_])", "False", py_like)
        py_like = re.sub(r"(?<![A-Za-z0-9_])null(?![A-Za-z0-9_])", "None", py_like)
        obj = ast.literal_eval(py_like)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    raise ValueError(f"Model did not return valid JSON.\n{raw}")


def plan_actions(llm: LLMClient, user_goal: str, context_snippets: List[str]) -> Dict[str, Any]:
    msgs = [
        {"role": "system", "content": SYSTEM_TOOLING},
        {"role": "user", "content": user_goal},
    ]
    if context_snippets:
        msgs.append({
            "role": "user",
            "content": "Recent page snippets:\n" + "\n---\n".join(context_snippets[-3:])
        })
    raw = llm.chat(msgs, temperature=0.2)
    plan = _parse_model_json_loose(raw)
    if "actions" not in plan or not isinstance(plan["actions"], list):
        raise ValueError(f"Missing 'actions' list in model output.\n{plan}")
    return plan

def summarize_findings(llm: LLMClient, user_goal: str, context_snippets: List[str]) -> str:
    # Use a slightly longer window to capture useful content
    snippets = context_snippets[-12:] if context_snippets else []
    obs = "\n---\n".join(snippets) if snippets else "(no observations)"
    msgs = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": f"User goal: {user_goal}"},
        {"role": "user", "content": "Observations collected:\n" + obs},
    ]
    try:
        return llm.chat(msgs, temperature=0.2)
    except Exception as e:
        return f"Summary unavailable due to error: {e}"

def execute_actions(agent: BrowserAgent, actions: List[Dict[str, Any]], label: str = "") -> Dict[str, Any]:
    last_scrape = ""
    last_links: List[Dict[str, str]] = []
    for i, act in enumerate(actions, start=1):
        t = act.get("type", "").lower()
        try:
            prefix = (label + " ") if label else ""
            print(f"{prefix}[DO] Step {i}: {t} {json.dumps(act, ensure_ascii=False)}", flush=True)
            if t == "open_url":
                agent.open_url(act["url"])
                # Auto-scrape every visited page
                last_scrape = agent.scrape(act.get("selector", "viewport"), int(act.get("max_chars", 2000)))
                print(f"\n{prefix}[SCRAPE]\n" + last_scrape + "\n", flush=True)
                # Also extract links to seed next steps
                last_links = agent.extract_links(act.get("link_selector", "a"), int(act.get("limit", 10)))
                print(f"\n{prefix}[LINKS]", flush=True)
                for idx, lk in enumerate(last_links, 1):
                    mark = " [clicked]" if lk.get("clicked") else ""
                    print(f"{prefix}{idx}. {mark} {lk['text'] or '(no text)'} — {lk['href']}", flush=True)
                print()
            elif t == "type":
                agent.type(act["selector"], act.get("text", ""), bool(act.get("submit", False)))
            elif t == "click":
                try:
                    agent.click(act["selector"])
                except Exception as e_click:
                    # Fallback: if 'text' provided, try clicking link by visible text
                    txt = (act.get("text") or "").strip()
                    if txt:
                        try:
                            # Build a robust XPath using contains() on normalized text
                            lit = agent._xpath_literal(txt)
                            by = By.XPATH
                            xp = f"//a[contains(normalize-space(.), {lit})]"
                            el = agent.wait.until(EC.element_to_be_clickable((by, xp)))
                            try:
                                agent.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                            except Exception:
                                pass
                            time.sleep(0.1)
                            href = None
                            try:
                                href = el.get_attribute('href')
                            except Exception:
                                pass
                            el.click()
                            if href:
                                try:
                                    agent.clicked_hrefs.add(href)
                                except Exception:
                                    pass
                            agent._maybe_stop_loading()
                        except Exception:
                            raise e_click
                    else:
                        raise e_click
                # Auto-scrape every visited page
                last_scrape = agent.scrape(act.get("selector", "viewport"), int(act.get("max_chars", 2000)))
                print(f"\n{prefix}[SCRAPE]\n" + last_scrape + "\n", flush=True)
                last_links = agent.extract_links(act.get("link_selector", "a"), int(act.get("limit", 10)))
                print(f"\n{prefix}[LINKS]", flush=True)
                for idx, lk in enumerate(last_links, 1):
                    mark = " [clicked]" if lk.get("clicked") else ""
                    print(f"{prefix}{idx}. {mark} {lk['text'] or '(no text)'} — {lk['href']}", flush=True)
                print()
            elif t == "wait_for":
                agent.wait_for(act["selector"], int(act.get("timeout", 15)))
            elif t == "scroll":
                agent.scroll(int(act.get("px", 1200)))
            elif t == "scrape":
                last_scrape = agent.scrape(act.get("selector", "body"), int(act.get("max_chars", 2000)))
                print(f"\n{prefix}[SCRAPE]\n" + last_scrape + "\n", flush=True)
            elif t == "extract_links":
                last_links = agent.extract_links(act.get("selector", "a"), int(act.get("limit", 10)))
                print(f"\n{prefix}[LINKS]", flush=True)
                for idx, lk in enumerate(last_links, 1):
                    mark = " [clicked]" if lk.get("clicked") else ""
                    print(f"{prefix}{idx}. {mark} {lk['text'] or '(no text)'} — {lk['href']}", flush=True)
                print()
            elif t == "screenshot":
                path = agent.screenshot(act.get("path", "screenshot.png"))
                print(f"{prefix}[SCREENSHOT] saved to {path}", flush=True)
            elif t == "back":
                agent.back()
                # Auto-scrape after back navigation as well
                last_scrape = agent.scrape("viewport", 2000)
                print(f"\n{prefix}[SCRAPE]\n" + last_scrape + "\n", flush=True)
                last_links = agent.extract_links("a", 10)
                print(f"\n{prefix}[LINKS]", flush=True)
                for idx, lk in enumerate(last_links, 1):
                    mark = " [clicked]" if lk.get("clicked") else ""
                    print(f"{prefix}{idx}. {mark} {lk['text'] or '(no text)'} — {lk['href']}", flush=True)
                print()
            elif t == "done":
                print(f"{prefix}[OK] done", flush=True)
                break
            else:
                print(f"{prefix}[WARN] Unknown action type: {t}", flush=True)
            print(f"{prefix}[OK] Step {i}: {t}", flush=True)
        except Exception as step_err:
            print(f"{prefix}[ERROR] Step {i} failed: {t} — {step_err}", flush=True)
            raise
        # Small delay to reduce flakiness
        time.sleep(float(act.get("delay", 0.15)))
    return {"scrape": last_scrape, "links": last_links}


# Simple shared board for multi-agent collaboration
class SharedBoard:
    def __init__(self):
        self._lock = threading.Lock()
        self._notes: List[str] = []
    def post(self, who: str, note: str):
        ts = datetime.now().strftime('%H:%M:%S')
        with self._lock:
            self._notes.append(f"[{ts}] {who}: {note}")
    def recent(self, n: int = 8) -> str:
        with self._lock:
            return "\n".join(self._notes[-n:])


def run_multi_agent(llm: LLMClient, args: argparse.Namespace, user_goal: str) -> None:
    def action_signature_local(act: Dict[str, Any]) -> str:
        t = (act.get("type") or "").lower()
        sel = act.get("selector") or ""
        url = act.get("url") or ""
        txt = (act.get("text") or "")[:64]
        return f"{t}|{sel}|{url}|{txt}"

    board = SharedBoard()
    results: Dict[str, Any] = {}

    def agent_worker(idx: int, who: str):
        goal_keywords = extract_keywords(user_goal)
        context_snippets: List[str] = []
        visited_urls: List[str] = []
        last_url: Optional[str] = None
        fail_counts: Dict[str, int] = {}
        last_sig: Optional[str] = None
        consecutive_dupes: int = 0
        last_action_type: Optional[str] = None
        scrape_streak: int = 0
        agent: Optional[BrowserAgent] = None
        label = f"[{who}]"
        try:
            for round_idx in range(1, args.steps + 1):
                # Include recent team notes in the prompt context
                team_obs = board.recent(6)
                prompt_ctx = context_snippets[-3:] + (["Team notes:\n" + team_obs] if team_obs else [])
                plan = plan_actions(llm, user_goal, prompt_ctx)
                actions = plan.get("actions", [])
                next_action = actions[0] if actions else {}
                print(f"\n{label} [NEXT {round_idx}] {plan.get('notes','')}")
                print(label + " Action:")
                print(textwrap.indent(json.dumps(next_action, indent=2), "  "))

                if (not next_action) or next_action.get("type", "").lower() == "done":
                    print(f"\n{label} [STATUS] Done per planner.")
                    break

                if agent is None:
                    print(f"{label} [INFO] Launching browser...", flush=True)
                    agent = BrowserAgent(headless=args.headless, binary=args.binary, detach=True, nav_stop_seconds=args.nav_stop_seconds)

                # Normalize Google -> DDG
                try:
                    if (next_action.get("type","" ).lower() == "open_url"):
                        url = str(next_action.get("url") or "")
                        if "google." in url:
                            parsed = urllib.parse.urlparse(url)
                            qs = urllib.parse.parse_qs(parsed.query)
                            q = (qs.get("q") or [""])[0]
                            ddg_url = "https://duckduckgo.com/" + ("?q=" + urllib.parse.quote(q) if q else "")
                            print(f"{label} [INFO] Rewriting Google URL to DuckDuckGo: {ddg_url}")
                            next_action["url"] = ddg_url
                except Exception:
                    pass

                # Ensure a home page if typing/scraping
                try:
                    na_type = (next_action.get("type") or "").lower()
                    need_home = na_type in ("type", "extract_links", "scrape")
                    cur = None
                    try:
                        if agent is not None:
                            cur = agent.driver.current_url
                    except Exception:
                        cur = None
                    if need_home and (not cur or cur == "about:blank"):
                        print(f"{label} [INFO] No page loaded yet; opening DuckDuckGo home first.")
                        next_action = {"type": "open_url", "url": "https://duckduckgo.com/"}
                except Exception:
                    pass

                # Prevent back-to-back scrapes
                try:
                    na_type = (next_action.get("type") or "").lower()
                    if na_type == "scrape":
                        if last_action_type == "scrape" and args.suppress_consecutive_scrapes > 0:
                            cur = None
                            try:
                                if agent is not None:
                                    cur = agent.driver.current_url
                            except Exception:
                                cur = None
                            if cur and "duckduckgo.com" in cur:
                                replacement = {"type": "extract_links", "selector": "a", "limit": 10}
                            else:
                                replacement = {"type": "screenshot", "path": "auto_screenshot.png"}
                            info = "Suppressed back-to-back scrape; substituting with 'extract_links' on DDG results." if replacement.get("type") == "extract_links" else "Suppressed back-to-back scrape; took a screenshot instead."
                            print(f"{label} [INFO] {info}")
                            context_snippets.append(info)
                            next_action = replacement
                        scrape_streak = 1 if last_action_type != "scrape" else (scrape_streak + 1)
                    else:
                        scrape_streak = 0
                except Exception:
                    pass

                sig = action_signature_local(next_action)
                if last_sig == sig:
                    consecutive_dupes += 1
                else:
                    consecutive_dupes = 0
                last_sig = sig
                if consecutive_dupes >= args.suppress_consecutive_duplicates:
                    note = f"Suppressed duplicate action: {sig}. Try different selector or scrape viewport."
                    print(f"{label} [INFO] {note}")
                    context_snippets.append(note)
                    continue
                if fail_counts.get(sig, 0) >= args.max_retries_per_action:
                    note = f"Retry limit reached for action: {sig}. Skipping."
                    print(f"{label} [INFO] {note}")
                    context_snippets.append(note)
                    continue

                try:
                    result = execute_actions(agent, [next_action], label=label)
                    try:
                        last_action_type = (next_action.get("type") or "").lower()
                    except Exception:
                        last_action_type = None
                    observation_parts = []
                    try:
                        cur_url = agent.driver.current_url
                        if cur_url and cur_url != last_url:
                            visited_urls.append(cur_url)
                            last_url = cur_url
                        observation_parts.append(f"URL: {cur_url}")
                    except Exception:
                        pass
                    body_text = result.get("scrape") or ""
                    if not body_text:
                        try:
                            body_text = agent.scrape("viewport", 1000)
                        except Exception:
                            body_text = ""
                    if body_text:
                        filtered = filter_text_by_keywords(body_text, goal_keywords, mode=args.relevance, max_lines=80)
                        observation_parts.append(filtered)
                    if result.get("links"):
                        filtered_links = filter_links_by_keywords(result["links"], goal_keywords, mode=args.relevance, max_keep=10)
                        def fmt_link(x: Dict[str, Any]) -> str:
                            mark = " [clicked]" if x.get("clicked") else ""
                            return f"- {mark} {x['text'] or '(no text)'} — {x['href']}"
                        joined = "\n".join([fmt_link(x) for x in filtered_links])
                        observation_parts.append("Links:\n" + joined)
                    if visited_urls:
                        recent = "\n".join([f"- {u}" for u in visited_urls[-5:]])
                        observation_parts.append(
                            f"VisitedCount: {len(visited_urls)}/{args.explore_count}\nRecentlyVisited:\n{recent}"
                        )
                    if observation_parts:
                        obs_joined = "\n".join(observation_parts)
                        context_snippets.append(obs_joined)
                        board.post(who, f"Round {round_idx}: {next_action.get('type')} → {last_url or ''}")
                except Exception as act_err:
                    err_msg = f"Error during action {next_action}: {act_err}"
                    print(f"{label} [WARN] {err_msg}")
                    context_snippets.append(err_msg)
                    try:
                        sig  # type: ignore
                        fail_counts[sig] = fail_counts.get(sig, 0) + 1
                    except Exception:
                        pass
                    board.post(who, f"Round {round_idx}: error {type(act_err).__name__}")

            results[who] = {"context": context_snippets}
        finally:
            if agent and not args.keep_open:
                try:
                    print(f"{label} Closing browser...")
                    agent.quit()
                except Exception:
                    pass

    threads: List[threading.Thread] = []
    for i in range(max(1, int(args.agents))):
        who = f"Agent-{i+1}"
        t = threading.Thread(target=agent_worker, args=(i, who), daemon=True)
        threads.append(t)
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nInterrupted.")

    if args.summarize:
        all_ctx: List[str] = []
        for who, data in results.items():
            all_ctx.append(f"[{who}]\n" + "\n".join(data.get("context", [])[-8:]))
        print("\n[INFO] Generating team summary...", flush=True)
        summary = summarize_findings(llm, user_goal, all_ctx)
        print("\n[SUMMARY]\n" + summary)
        if args.summary_file:
            try:
                with open(args.summary_file, "w", encoding="utf-8") as f:
                    f.write(summary)
                print(f"[INFO] Summary saved to {args.summary_file}")
            except Exception as e:
                print(f"[WARN] Could not save summary: {e}")

    print("\nAll agents finished.")

def main():
    ap = argparse.ArgumentParser(description="Headed Chromium web agent driven by an LLM.")
    ap.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key; or set env LLM_API_KEY.")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Model ID; or set env LLM_MODEL.")
    # Prefer provider mapping; allow custom endpoint override if provided explicitly
    ap.add_argument("--provider", choices=sorted(PROVIDER_ENDPOINTS.keys()), default=os.environ.get("LLM_PROVIDER", "groq"), help="LLM provider (sets default endpoint).")
    ap.add_argument("--endpoint", default=None, help="OpenAI-compatible base URL (overrides --provider if set).")
    ap.add_argument("--binary", default=os.environ.get("CHROME_BINARY"), help="Path to Chromium/Chrome binary.")
    ap.add_argument("--headless", action="store_true", help="Run headless. Default is headed.")
    ap.add_argument("--steps", type=int, default=3, help="Planning rounds (LLM turns).")
    ap.add_argument("--max-actions-per-step", type=int, default=8, help="Upper bound actions per plan.")
    ap.add_argument("--keep-open", action="store_true", help="Do not close the browser at the end.")
    ap.add_argument("--prompt", default=None, help="User goal. If omitted, you’ll be prompted.")
    ap.add_argument("--summarize", action="store_true", help="At the end, ask the LLM for a concise summary of findings.")
    ap.add_argument("--summary-file", default=None, help="If set with --summarize, save summary to this file path.")
    ap.add_argument("--explore-count", type=int, default=3, help="Target number of distinct result pages to visit before finishing.")
    ap.add_argument("--relevance", choices=["off","loose","strict"], default="loose", help="Filter observations to only show on-topic text/links to the LLM.")
    ap.add_argument("--max-retries-per-action", type=int, default=2, help="Max times to retry the same action signature before suppressing it.")
    ap.add_argument("--suppress-consecutive-duplicates", type=int, default=1, help="Suppress if the same action appears this many times in a row.")
    ap.add_argument("--suppress-consecutive-scrapes", type=int, default=1, help="If >0, do not allow more than this many 'scrape' actions back-to-back regardless of selector.")
    ap.add_argument("--nav-stop-seconds", type=float, default=2.0, help="After navigation or clicks/back, wait this many seconds then stop page loading (window.stop()). Set 0 to disable.")
    ap.add_argument("--agents", type=int, default=1, help="Run multiple collaborating agents in parallel (threads).")
    args = ap.parse_args()

    if not args.prompt:
        print("Enter your goal (single line). Example: 'Search for latest AI news, open the first result, scrape the article body.'")
        user_goal = input("> ").strip()
    else:
        user_goal = args.prompt.strip()

    # Validate API key early to avoid launching the browser unnecessarily
    if not args.api_key:
        print("[ERROR] Missing API key. Pass --api-key or set env LLM_API_KEY.")
        return

    # Determine endpoint from provider unless a custom endpoint is given
    endpoint = args.endpoint or PROVIDER_ENDPOINTS.get(args.provider)
    llm = LLMClient(api_key=args.api_key, model=args.model, endpoint=endpoint)

    # If multi-agent requested, run the coordinator and exit
    if args.agents and args.agents > 1:
        run_multi_agent(llm, args, user_goal)
        return

    # Defer launching the browser until we have a first action to run
    agent: Optional[BrowserAgent] = None

    context_snippets: List[str] = []
    visited_urls: List[str] = []
    last_url: Optional[str] = None
    goal_keywords = extract_keywords(user_goal)
    # Anti-spam tracking
    def action_signature(act: Dict[str, Any]) -> str:
        t = (act.get("type") or "").lower()
        sel = act.get("selector") or ""
        url = act.get("url") or ""
        txt = (act.get("text") or "")[:64]
        return f"{t}|{sel}|{url}|{txt}"

    fail_counts: Dict[str, int] = {}
    last_sig: Optional[str] = None
    consecutive_dupes: int = 0
    last_action_type: Optional[str] = None
    scrape_streak: int = 0
    try:
        for round_idx in range(1, args.steps + 1):
            # Ask for exactly one next action based on the latest observation
            plan = plan_actions(llm, user_goal, context_snippets)
            actions = plan.get("actions", [])
            if not isinstance(actions, list):
                raise ValueError("Planner response missing 'actions' list")
            # Take only the first action (single-step mode)
            next_action = actions[0] if actions else {}
            print(f"\n[NEXT {round_idx}] {plan.get('notes','')}")
            print("Action:")
            print(textwrap.indent(json.dumps(next_action, indent=2), "  "))

            # Stop if planner signals done
            if (not next_action) or next_action.get("type", "").lower() == "done":
                print("\n[STATUS] Done per planner.")
                break

            # Launch the browser lazily on first execution
            if agent is None:
                print("[INFO] Launching browser...", flush=True)
                agent = BrowserAgent(headless=args.headless, binary=args.binary, detach=True, nav_stop_seconds=args.nav_stop_seconds)

            # Execute exactly one action (with de-duplication and retry suppression)
            try:
                # Enforce default to DuckDuckGo if planner tries Google open_url; preserve q= if present
                try:
                    if (next_action.get("type","" ).lower() == "open_url"):
                        url = str(next_action.get("url") or "")
                        if "google." in url:
                            parsed = urllib.parse.urlparse(url)
                            qs = urllib.parse.parse_qs(parsed.query)
                            q = (qs.get("q") or [""])[0]
                            ddg_url = "https://duckduckgo.com/" + ("?q=" + urllib.parse.quote(q) if q else "")
                            print(f"[INFO] Rewriting Google URL to DuckDuckGo: {ddg_url}")
                            next_action["url"] = ddg_url
                except Exception:
                    pass

                # Ensure we have a sensible homepage for typing/searching
                try:
                    na_type = (next_action.get("type") or "").lower()
                    need_home = na_type in ("type", "extract_links", "scrape")
                    cur = None
                    try:
                        if agent is not None:
                            cur = agent.driver.current_url
                    except Exception:
                        cur = None
                    if need_home and (not cur or cur == "about:blank"):
                        print("[INFO] No page loaded yet; opening DuckDuckGo home first.")
                        next_action = {"type": "open_url", "url": "https://duckduckgo.com/"}
                except Exception:
                    pass

                # Prevent back-to-back scrapes regardless of selector
                try:
                    na_type = (next_action.get("type") or "").lower()
                    if na_type == "scrape":
                        if last_action_type == "scrape" and args.suppress_consecutive_scrapes > 0:
                            # Prefer extracting links on DDG, else take a screenshot
                            replacement: Dict[str, Any]
                            cur = None
                            try:
                                if agent is not None:
                                    cur = agent.driver.current_url
                            except Exception:
                                cur = None
                            if cur and "duckduckgo.com" in cur:
                                replacement = {"type": "extract_links", "selector": "a", "limit": 10}
                            else:
                                replacement = {"type": "screenshot", "path": "auto_screenshot.png"}
                            info = "Suppressed back-to-back scrape; substituting with 'extract_links' on DDG results." if replacement.get("type") == "extract_links" else "Suppressed back-to-back scrape; took a screenshot instead."
                            print(f"[INFO] {info}")
                            context_snippets.append(info)
                            next_action = replacement
                        # track streak for potential future tuning
                        scrape_streak = 1 if last_action_type != "scrape" else (scrape_streak + 1)
                    else:
                        scrape_streak = 0
                except Exception:
                    pass

                sig = action_signature(next_action)
                # Suppress consecutive duplicates
                if last_sig == sig:
                    consecutive_dupes += 1
                else:
                    consecutive_dupes = 0
                last_sig = sig
                if consecutive_dupes >= args.suppress_consecutive_duplicates:
                    note = f"Suppressed duplicate action: {sig}. Suggest trying a different selector or 'scrape' the viewport."
                    print(f"[INFO] {note}")
                    context_snippets.append(note)
                    continue
                # Suppress excessive retries of the same failing action
                if fail_counts.get(sig, 0) >= args.max_retries_per_action:
                    note = f"Retry limit reached for action: {sig}. Not executing again; propose an alternative approach."
                    print(f"[INFO] {note}")
                    context_snippets.append(note)
                    continue

                result = execute_actions(agent, [next_action])
                try:
                    last_action_type = (next_action.get("type") or "").lower()
                except Exception:
                    last_action_type = None
                # Build observation for next turn
                observation_parts = []
                try:
                    cur_url = agent.driver.current_url
                    if cur_url and cur_url != last_url:
                        visited_urls.append(cur_url)
                        last_url = cur_url
                    observation_parts.append(f"URL: {cur_url}")
                except Exception:
                    pass
                body_text = result.get("scrape") or ""
                if not body_text:
                    # If the step didn't scrape, capture a lightweight body snapshot
                    try:
                        body_text = agent.scrape("viewport", 1000)
                    except Exception:
                        body_text = ""
                # Relevance-filter the visible text
                if body_text:
                    filtered = filter_text_by_keywords(body_text, goal_keywords, mode=args.relevance, max_lines=80)
                    observation_parts.append(filtered)
                # Relevance-filter the links list
                if result.get("links"):
                    filtered_links = filter_links_by_keywords(result["links"], goal_keywords, mode=args.relevance, max_keep=10)
                    def fmt_link(x: Dict[str, Any]) -> str:
                        mark = " [clicked]" if x.get("clicked") else ""
                        return f"- {mark} {x['text'] or '(no text)'} — {x['href']}"
                    joined = "\n".join([fmt_link(x) for x in filtered_links])
                    observation_parts.append("Links:\n" + joined)
                # Add exploration guidance and progress
                if visited_urls:
                    recent = "\n".join([f"- {u}" for u in visited_urls[-5:]])
                    observation_parts.append(
                        f"VisitedCount: {len(visited_urls)}/{args.explore_count}\nRecentlyVisited:\n{recent}"
                    )
                if observation_parts:
                    context_snippets.append("\n".join(observation_parts))
            except Exception as act_err:
                # Feed error back into the next prompt as observation
                err_msg = f"Error during action {next_action}: {act_err}"
                print(f"[WARN] {err_msg}")
                context_snippets.append(err_msg)
                try:
                    sig  # type: ignore
                    fail_counts[sig] = fail_counts.get(sig, 0) + 1
                except Exception:
                    pass
                # Continue to next turn to let the planner adapt

        # Optional end-of-run summary
        if args.summarize:
            print("\n[INFO] Generating summary...", flush=True)
            summary = summarize_findings(llm, user_goal, context_snippets)
            print("\n[SUMMARY]\n" + summary)
            if args.summary_file:
                try:
                    with open(args.summary_file, "w", encoding="utf-8") as f:
                        f.write(summary)
                    print(f"[INFO] Summary saved to {args.summary_file}")
                except Exception as e:
                    print(f"[WARN] Could not save summary: {e}")

        print("\nAll rounds finished.")
        if agent and not args.keep_open:
            print("Closing browser...")
            agent.quit()
        else:
            print("Keeping browser open. Close it yourself when done.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as e:
        print(f"\n[ERROR] {e}")
    finally:
        if agent and not args.keep_open:
            try:
                agent.quit()
            except Exception:
                pass

if __name__ == "__main__":
    main()
# Global cross-agent visited URL tracking
GLOBAL_VISITED_URLS: set[str] = set()
GLOBAL_VISITED_LOCK = threading.Lock()
