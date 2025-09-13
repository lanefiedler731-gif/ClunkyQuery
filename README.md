# ClunkyQuery

An AI-powered web search engine in Python that goes out of its way to make life difficult.  
It’s driven by a large language model that plans actions in JSON, then forces Selenium to click around like a confused intern.  
If you wanted a polished, frictionless search experience, you are in the wrong place.

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

The main script is `agent_browser.py`. It runs an LLM-powered browser agent that plans each step in JSON and executes with Selenium.

Run the script with a goal:

```bash
python agent_browser.py --api-key <YOUR_KEY> --prompt "find latest AI news"
```

### Options

- `--api-key` → API key (or set env `LLM_API_KEY`)  
- `--model` → Model ID (default: `llama-3.3-70b-versatile`)  
- `--provider` → LLM provider (`groq`, `openai`, `together`)  
- `--endpoint` → Custom API base URL  
- `--binary` → Path to Chrome/Chromium binary  
- `--headless` → Run without visible browser  
- `--steps N` → Number of planning rounds (default 3)  
- `--agents N` → Run multiple agents in parallel  
- `--summarize` → Ask LLM to summarize findings  
- `--summary-file` → Save summary to file  
- `--keep-open` → Don’t close the browser at the end  
- `--relevance` → Filter scraped text/links (`off`, `loose`, `strict`)  
- `--suppress-consecutive-scrapes` → Prevent scraping twice in a row  
- `--suppress-consecutive-duplicates` → Prevent retrying identical actions  

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
