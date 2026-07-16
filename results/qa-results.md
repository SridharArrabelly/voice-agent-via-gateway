# Agent QA results — voice vs text

- Generated (UTC): `2026-07-16T18:49:15.612704+00:00`
- Path: browser WS → Python backend → APIM → Voice Live (agent `voice-mode-agent` + web tool)

## Latency summary (median across questions)

| Modality | connect (ms) | ttfr median (ms) | total median (ms) | errors |
|----------|-------------:|-----------------:|------------------:|-------:|
| voice | 2254.0 | 4443.6 | 7538.2 | 0 |
| text | 1385.0 | 3701.9 | 4011.3 | 0 |

## Voice — per question

### Q1. What is the latest stable version of Python, and roughly when was it released?
- ttfr=3624.0 ms · total=8139.9 ms · audio_deltas=43
- Answer: The latest stable Python release is **Python 3.14.6**, released on **June 10, 2026**.

If you meant the latest **major stable series**, that’s **Python 3.14.0**, which was first released on **October 7, 2025**.

Source: Python.org release pages.

### Q2. Who is the current CEO of OpenAI?
- ttfr=5317.9 ms · total=7312.3 ms · audio_deltas=24
- Answer: The current CEO of OpenAI is **Sam Altman**.

OpenAI’s official site says, “**I remain the CEO of OpenAI**” in its May 7, 2025 leadership update.

### Q3. What is today's date?
- ttfr=2239.6 ms · total=2834.3 ms · audio_deltas=8
- Answer: Today is **July 16, 2026**.

### Q4. Who won the most recent FIFA World Cup, and in what year?
- ttfr=3584.6 ms · total=5525.1 ms · audio_deltas=19
- Answer: The most recent FIFA World Cup was won by **Argentina** in **2022**. They beat **France** in the final on December 18, 2022.

### Q5. What is the latest iPhone model Apple has released?
- ttfr=8606.5 ms · total=12133.8 ms · audio_deltas=34
- Answer: The most recently released iPhone model appears to be **iPhone 17e**, which Apple announced on **March 2, 2026** and made available on **March 11, 2026**.

If you meant the latest **flagship** iPhone, that would be the **iPhone 17 Pro**.

### Q6. What is the current approximate price of Bitcoin in US dollars?
- ttfr=6156.3 ms · total=16371.6 ms · audio_deltas=70
- Answer: Bitcoin is currently around **$64,000 USD** — roughly **$64.0k to $64.4k** based on the latest market quotes I found.

Examples from live price pages:
- CoinDesk: **$64,411.38**
- Kraken: **$64,173.00**
- OKX: **$64,183.50**

So a good rough estimate is **about $64.2k per BTC**.

### Q7. Who is the current Secretary-General of the United Nations?
- ttfr=4196.9 ms · total=4786.1 ms · audio_deltas=11
- Answer: The current Secretary-General of the United Nations is **António Guterres**.

### Q8. Name one major AI model that was released recently.
- ttfr=4690.4 ms · total=7764.2 ms · audio_deltas=27
- Answer: One recent major AI model is **GPT-5.6** from OpenAI, which was released on **July 9, 2026**.

If you want, I can also name a recent model from Google or Anthropic.

### Q9. What is the capital city of Australia?
- ttfr=3055.1 ms · total=3620.3 ms · audio_deltas=7
- Answer: The capital city of Australia is **Canberra**.

### Q10. What is the weather like in London today?
- ttfr=4867.2 ms · total=16649.2 ms · audio_deltas=81
- Answer: London is **sunny and warm today**. The latest reports show it around **28°C (82°F)**, with **light breeze**, **low chance of rain**, and **mostly clear/sunny conditions**.

A quick summary:
- **Temperature:** about **28°C / 82°F**
- **Feels like:** about **27.5–28°C / 80–82°F**
- **Rain chance:** **0–2%**
- **Wind:** light to moderate, around **8–10 mph**

If you want, I can also give you the **hour-by-hour forecast** or the **forecast for tomorrow**.


## Text — per question

### Q1. What is the latest stable version of Python, and roughly when was it released?
- ttfr=3714.2 ms · total=4026.3 ms · audio_deltas=0
- Answer: The latest stable version of Python is **Python 3.14.6**, and it was released on **June 10, 2026**.

If you mean the latest **feature series**, that’s **Python 3.14**.

### Q2. Who is the current CEO of OpenAI?
- ttfr=3270.4 ms · total=3501.3 ms · audio_deltas=0
- Answer: The current CEO of OpenAI is **Sam Altman**.

OpenAI’s own site states in its “Our structure” page that the board includes “CEO Sam Altman,” and OpenAI also announced on **November 29, 2023** that “Sam Altman returns as CEO.”

### Q3. What is today's date?
- ttfr=1767.8 ms · total=2059.8 ms · audio_deltas=0
- Answer: Today’s date is **July 16, 2026**.

### Q4. Who won the most recent FIFA World Cup, and in what year?
- ttfr=3741.3 ms · total=3996.3 ms · audio_deltas=0
- Answer: The most recent FIFA World Cup was won by **Argentina**, in **2022**.

### Q5. What is the latest iPhone model Apple has released?
- ttfr=4895.6 ms · total=5477.6 ms · audio_deltas=0
- Answer: The latest iPhone model Apple has released appears to be **iPhone 17e**, which became available on **March 11, 2026**.

If you mean the latest flagship series, that’s the **iPhone 17 lineup** (including iPhone 17, iPhone 17 Pro, iPhone 17 Pro Max, and iPhone Air), announced on **September 9, 2025**.

### Q6. What is the current approximate price of Bitcoin in US dollars?
- ttfr=3689.7 ms · total=4095.8 ms · audio_deltas=0
- Answer: Bitcoin is currently around **$64,000 USD** per BTC.

Recent sources I found put it in a narrow range of about **$63,800–$64,400**, so **$64.0k** is a reasonable approximation.

### Q7. Who is the current Secretary-General of the United Nations?
- ttfr=3771.4 ms · total=4111.9 ms · audio_deltas=0
- Answer: The current Secretary-General of the United Nations is **António Guterres**.

The UN’s official site identifies him as the Secretary-General, and says he took office on **1 January 2017**.

### Q8. Name one major AI model that was released recently.
- ttfr=4322.0 ms · total=4707.0 ms · audio_deltas=0
- Answer: One recent major AI model is **GPT-5.6** from OpenAI, released on **July 9, 2026**.

### Q9. What is the capital city of Australia?
- ttfr=2310.6 ms · total=2498.8 ms · audio_deltas=0
- Answer: The capital city of Australia is **Canberra**.

### Q10. What is the weather like in London today?
- ttfr=2480.8 ms · total=3379.1 ms · audio_deltas=0
- Answer: London today is **sunny and warm**.

- **Current:** about **28°C (82°F)**, feels like **27.5°C**
- **Today’s high/low:** around **29°C / 15–16°C**
- **Rain chance:** **very low**, around **0–5%**
- **Wind:** light to moderate, around **17 km/h** from the east
- **UV:** **high** during the day

So overall: **bright, mostly dry, and quite warm for London**.
