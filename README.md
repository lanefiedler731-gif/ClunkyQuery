# ClunkyQuery

An AI-powered web search engine in Python that goes out of its way to make life difficult. It’s driven by a large language model that plans actions in JSON, then forces Selenium to click around like a confused intern. If you wanted a polished, frictionless search experience, you are in the wrong place.

---

## Features (or Misfeatures)

- **AI-driven browsing**: lets an LLM decide what to click, type, or scrape.
- **Headed Chromium browser**: you’ll see the chaos unfold in real time (unless you beg for headless).
- **Multi-agent mode**: several bots can stumble through the web together, like blindfolded roommates.
- **Automatic retries & suppression**: stops it from making the *exact* same mistake more than twice.
- **Summaries**: asks the LLM to summarize findings, which may or may not be useful.
- **Default search engine**: DuckDuckGo, because Google has rate limits and patience is already in short supply.

---

## Requirements

- Python 3.9+
- Chrome or Chromium installed
- [Selenium](https://pypi.org/project/selenium/)  
- [webdriver_manager](https://pypi.org/project/webdriver-manager/) (optional but saves you from version hell)  
- An API key for an OpenAI-compatible LLM endpoint (e.g. Groq, OpenAI, etc.)

Install dependencies:

```bash
pip install selenium webdriver-manager requests
```

---

## Usage

Run the script with a goal:

```bash
python agent_browser.py --api-key <YOUR_KEY> --prompt "find latest AI news"
```

Options worth knowing:

- `--headless` → run without a visible browser  
- `--steps N` → number of planning rounds (default 3)  
- `--agents N` → run multiple agents in parallel  
- `--summarize` → get a half-decent summary at the end  
- `--keep-open` → stop ClunkyQuery from immediately closing the browser you didn’t want open anyway  

Full help:

```bash
python agent_browser.py -h
```

---

## Example

```bash
python agent_browser.py --api-key sk-... --steps 5 --summarize --prompt "search for GPU shortages"
```

Expect it to open DuckDuckGo, click on random articles, scrape partial text, and then produce a bullet-point summary that *might* be relevant.

---

## Warnings

- It’s clunky. The name wasn’t ironic.  
- It will click things you didn’t ask for.  
- It might break if Google changes their consent dialog.  
- It is **not** a replacement for a real search engine.  

---

## License

MIT, because you should be free to suffer.
