# Parallax 視差

**Same incident, different angle — measured.**

## Project proposal

---

## 1. Summary

Parallax is a keyword-driven system that ingests the full daily output of Taiwanese news outlets alongside social media discussion, then answers four questions about any incident:

1. Which outlets lean positive or negative on it
2. How much of each outlet's daily output it consumed
3. Which outlets are copying each other, and what each one added or removed
4. Whether each social platform leans positive or negative

The name comes from astronomy: parallax is the apparent shift in an object's position depending on where the observer stands, and the size of that shift is how distance gets calculated. The project treats media bias the same way — not asserted, but measured as the divergence between outlets reporting the same event.

The differentiator is question 3. Sentiment dashboards are common; **copy-and-framing analysis is not**. Detecting that fourteen outlets ran the same wire copy, identifying which one published first, and showing exactly which paragraph each outlet appended or dropped is a measurable editorial signal that existing tools do not surface.

---

## 2. Problem

Media monitoring tools report sentiment as a single aggregate number per topic. This hides the two things that actually matter:

- **Volume is not attention.** An outlet publishing 60 articles on an incident out of 340 total that day is making a very different editorial statement than one publishing 12 out of 430. Raw article counts obscure this; share of daily output reveals it.
- **Most coverage is not original.** A large share of news output is syndicated or lightly rewritten. Counting near-duplicate articles as independent coverage inflates every downstream metric. Separating original reporting from copies changes the picture substantially.

---

## 3. Core metrics

| Metric | Definition | Why it matters |
|---|---|---|
| Outlet stance | Sentiment distribution per outlet, per aspect | Editorial position |
| Coverage weight | Incident articles ÷ outlet's total articles that day | Editorial priority |
| Originality rate | Articles not in a near-duplicate cluster ÷ total | Independent reporting volume |
| Propagation order | Publish-time rank within a duplicate cluster | Who originates, who follows |
| Framing delta | Text present in one cluster member but absent from the shared core | Editorial choice, made visible |
| Platform lean | Sentiment distribution per social platform | Public reaction by venue |

---

## 4. Two-tier ingestion

The single most important structural decision. Crawling and enrichment are separated, because they have completely different cost profiles.

| Tier | Scope | Data captured | Cost |
|---|---|---|---|
| **Listing crawl** | Every article, every outlet, all day | outlet, URL, headline, timestamp | Negligible — RSS or section listings, no model calls |
| **Body fetch + enrich** | Only articles matching an active keyword | full text, sentiment, aspect, dedup cluster | Real — scales with matched volume |

The listing crawl supplies the denominator for coverage weight. It never needs enrichment; it only needs counting. A few thousand short metadata rows per day is trivial for Postgres, and eight outlets polled every 20–30 minutes is a few hundred small requests daily.

**This tier cannot be deferred.** RSS feeds expose only recent items — typically hours to a day. Once an article scrolls off, reconstructing "everything outlet X published on March 3rd" is usually impossible without site-specific archive scraping. The asymmetry is decisive:

- Hiding percentages in the UI → reversible any time
- Skipping the listing crawl → that period's coverage weight is permanently lost

So the listing crawl runs from week 1, even though the metric it feeds ships later.

---

## 5. Scope by phase

Everything in the full design is retained; the phases sequence it so a working system exists early.

### Phase 1 — MVP (weeks 1–4)

**Goal:** a working single-machine system that answers questions 1, 2, and 3 for one language and one country.

Included:
- Listing crawl of 6–8 Taiwanese outlets, metadata only, from day one
- Body fetch and enrichment on keyword matches only
- PostgreSQL storage, no message bus
- `cron` scheduling, no orchestrator
- SimHash near-duplicate clustering (deterministic, no model required)
- Fine-tuned Chinese BERT for target-dependent sentiment
- Longest-common-subsequence diff within clusters
- Streamlit UI with Postgres full-text keyword search

Displayed in phase 1: **raw article counts**, with per-outlet baseline normalization once roughly 10 days of listing data exist. True same-day percentages switch on around week 4 and apply retroactively, because the log will already be there.

Deferred: social media, aspect taxonomy, Google Trends, Kafka, Airflow, LangChain, regional map.

**Exit criterion:** typing a keyword returns outlet stance, coverage counts, and at least one correctly identified copy cluster with a readable diff.

### Phase 2 — Analytical depth (weeks 5–8)

- PTT and Dcard ingestion, enabling question 4
- Aspect taxonomy: BERTopic for discovery, hand-curated to 5–8 labels, LLM for consistent labeling
- LangChain for the labeling layer — response caching, Pydantic-validated structured output, async batching
- Elasticsearch replaces Postgres full-text as the query layer
- Google Trends for keyword suggestion (rising and related queries) and the third attention timeline

### Phase 3 — Production shape (weeks 9–12)

- Kafka event bus with long retention; raw topics become the replayable archive so taxonomy revisions do not require re-crawling
- Airflow DAGs: `ingest_daily`, `enrich`, `metrics_recompute`, `taxonomy_migration`
- Influence scoring derived from propagation order plus downstream volume response
- Regional aspect-interest map from Trends county-level data
- React frontend replacing Streamlit

---

## 6. Data model

```sql
article_index (                          -- listing crawl, every article
  id, outlet, url, title, published_at, seen_at,
  body_fetched BOOLEAN DEFAULT FALSE
)

articles (                               -- enriched subset only
  id REFERENCES article_index, body,
  dup_cluster_id, is_cluster_origin,
  sentiment_label, sentiment_score, aspect_label
)

outlet_daily_totals (
  outlet, date, total_articles           -- derived from article_index
)

dup_clusters (
  cluster_id, member_count, shared_core_text,
  origin_article_id, first_published_at
)

social_posts (
  id, platform, board, posted_at,
  sentiment_label, aspect_label, text
)
```

The split between `article_index` and `articles` is what makes the cost model work: the first table grows with everything published, the second only with what someone actually queries.

---

## 7. Architecture

**MVP:** cron → listing crawlers → Postgres → keyword-triggered body fetch → SimHash + BERT enrichment → Streamlit.

**Full:** crawlers → Kafka raw topics → enrichment consumers (BERT bulk, LLM on demand) → Postgres + Elasticsearch → metrics layer → React dashboard, with Airflow owning scheduled backfill and recomputation.

The MVP is a strict subset — no component is discarded when phase 3 arrives. The schema and enrichment logic carry over unchanged, with Kafka inserted upstream and Airflow wrapped around the existing jobs.

---

## 8. Technical risks

**News is written in neutral register.** Document-level sentiment classifiers return "neutral" for most news articles, because reporters avoid overt evaluative language. Naive sentiment analysis will produce a flat, uninformative result.

Mitigation: use target-dependent sentiment (aspect-based sentiment analysis) rather than document sentiment — classify stance toward the incident's actors, not the tone of the prose. Headline and lede carry more stance signal than body text and should be weighted accordingly. This is the single largest technical risk in the project and should be validated on hand-labeled data in week 2, before the rest is built on top of it.

**Raw counts measure outlet size, not priority.** Before the denominator matures, an outlet publishing 63 articles versus another's 12 mostly reflects that the first publishes more of everything. Per-outlet baseline normalization is a partial fix; it misses weekend and holiday variation but is directionally correct and available within two weeks.

**Crawler fragility and access terms.** Outlets change markup and some prohibit scraping in their terms of service. Prefer RSS feeds, rate-limit conservatively, cache raw HTML so parser changes do not require re-fetching, and check each outlet's `robots.txt` and terms before including it.

**Small denominators.** Outlets publishing few articles per day produce noisy percentages. Suppress the metric below a minimum daily volume threshold rather than displaying an unstable number.

**LLM cost.** Labeling every article daily does not scale. Two-tier approach: a local fine-tuned model handles bulk classification; LLM calls are reserved for cluster diff analysis, which runs only on clusters a user actually queries.

---

## 9. Evaluation

The project should be judged on measured accuracy, not on the dashboard rendering.

| Component | Method | Target |
|---|---|---|
| Duplicate detection | 200 hand-labeled article pairs | Precision > 0.90, recall > 0.80 |
| Sentiment | 300 hand-labeled articles, 3-class | Macro-F1 > 0.75 |
| Aspect labeling | 200 items, inter-annotator agreement vs model | Cohen's κ > 0.6 |
| Crawl completeness | Sampled manual count vs captured count | > 95% capture rate |

Hand-labeling is unavoidable and should be budgeted as real work — roughly two days across the project.

---

## 10. Stack

| Layer | MVP | Full |
|---|---|---|
| Ingestion | `requests` + `BeautifulSoup`, `feedparser` | same, behind Kafka producers |
| Scheduling | `cron` | Airflow |
| Transport | direct writes | Kafka / Redpanda |
| NLP | Chinese BERT, `jieba` or CKIP | + BERTopic, LLM via LangChain |
| Dedup | SimHash, `datasketch` | same |
| Storage | PostgreSQL | PostgreSQL + Elasticsearch |
| Frontend | Streamlit | React + ECharts |

---

## 11. Milestones

| Week | Deliverable |
|---|---|
| 1 | Listing crawl live for 6–8 outlets; metadata accumulating |
| 2 | Sentiment approach validated on hand-labeled sample |
| 3 | SimHash clustering + diff working; originality rate computable |
| 4 | Streamlit UI answering questions 1–3; counts switch to true percentages — **MVP complete** |
| 6 | PTT and Dcard ingested; question 4 answered |
| 8 | Aspect taxonomy live; Elasticsearch query layer |
| 10 | Kafka and Airflow in place; influence scoring |
| 12 | React frontend; evaluation results written up |

---

## 12. Assumptions to confirm

- **Language and region: Traditional Chinese, Taiwan.** This determines the sentiment model, the tokenizer, and which social platforms are worth ingesting. Changing it invalidates most of the NLP stack choices above.
- Outlet list to be fixed in week 1; changing it later resets the daily-totals baseline.
- Timeline assumes part-time work; compress proportionally if full-time.
