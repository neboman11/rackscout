# RackScout implementation contract

Authoritative interface between independent implementation lanes. Full approved spec: `/home/nesbitt/Desktop/argocd-definitions/docs/superpowers/specs/2026-11-02-rackscout-design.md`.

## Ownership

Backend owns `rackscout/*.py`, `rackscout/migrations/`, `rackscout/data/`, `tests/`. Frontend owns only `rackscout/static/index.html`, `app.css`, `app.js`. Parent owns pyproject/uv.lock, Docker/Compose, README, GitOps chart, integration and verification. No agent builds/lints/tests/formatters mid-flight. Parent writes initial source behavioral tests and runs failing baseline; backend may add tests but not overwrite parent's tests. No commits/pushes by agents. Any needed interface change must be messaged to parent before modifying contract.

## Runtime

Python 3.12+, package `rackscout`. Serve `uvicorn rackscout.api:app --host 0.0.0.0 --port 8080`. CLI `python -m rackscout migrate` and `python -m rackscout collect` (flags `--max-details N`, `--no-browser` permitted for smoke). `python -m rackscout refresh-worker` (no flags) is the long-running manual-refresh worker; see Manual refresh. Runtime env `DATABASE_URL` or standard PGHOST,PGPORT,PGDATABASE,PGUSER,PGPASSWORD. Config `RACKSCOUT_MAX_DETAILS` default25; `RACKSCOUT_BROWSER_ENABLED` defaulttrue; timeout/request delay/cooldown may be configurable. Discovery sources are fixed in code, not configurable: the five PerUnitPrice vendor index pages `https://perunitprice.com/servers/{dell,hpe,supermicro,lenovo,cisco}/`, fetched in that order through the same polite allowlisted fetcher (stable identifying `RackScout/…` User-Agent, no spoofing, per-origin delay, no challenge evasion). The combined `/servers/` page is not used: it embeds only a truncated 1000-row subset of its advertised inventory. The former `RACKSCOUT_SOURCE_URL` setting no longer exists.

`RACKSCOUT_BENCHMARKS_FILE` (optional): path to an external private per-CPU benchmark JSON (`{provider, benchmarks:[{cpu_model, cpu_mark, single_thread, samples, version, scope:"single_cpu", source_url (https), retrieved_at}]}`). No benchmark dataset is bundled in the package or image. Unset: `migrate` skips benchmark synchronization and leaves previously stored benchmark rows untouched; listings without a stored score show benchmark null (Not available). Set but missing, unreadable or malformed: `migrate` fails with a clear error naming the variable and changes nothing. When set and valid the file is authoritative (rows not in it are removed).

## HTTP

GET `/health`: `{"status":"ok"}` process only.
GET `/ready`: 200 `{"status":"ready"}` if DB/schema usable; otherwise503 generic detail.
GET `/api/listings`: filters `vendor` (Dell/HPE/Supermicro/Lenovo/Cisco, `HP` accepted as alias of HPE, empty means all, otherwise 422), `model` (vendor-qualified key; repeatable: `?model=R440&model=R640` selects the union (OR) of the listed models, ANDed with every other filter; a single `?model=R640` keeps working; blank/absent means all models; values are trimmed, blanks ignored, duplicates removed preserving order; each must match `[A-Za-z0-9][A-Za-z0-9 +\-]{0,59}` else the whole request is 422; more than 100 values is 422; models may span OEMs when `vendor` is empty, and combined with `vendor` only models of that vendor can match; values are bound SQL parameters), `generation` (vendor-scoped platform generation number, default none; nonempty without `vendor` is 422 because Dell 14G, HPE Gen10, Supermicro X11 etc. are not equivalent numbers), `modern_only` defaulttrue (only cataloged platforms with target_era; false also shows older or unknown-era data), `height` default1, `min_ram` default16, `min_bays` default4, `drive_size` SFF/LFF/empty, `cpu_count` 1/2/empty, `max_total` optional decimal, `diskless` defaulttrue, `hide_damaged` defaulttrue, `confirmed_only` defaultfalse, `include_leads` defaulttrue, `q` max200, `sort` one of price/ram/bays/benchmark, `page` default1, `page_size` default50 max100. Empty numeric query parameter means no constraint; frontend must send empties for Any. Unknown/conflicting fields cannot satisfy positive constraints as matches; unknown generation, era or rack height never satisfies a positive chassis constraint. Unverified leads may be separately included when no known contradiction, but never called matches; confirmed_only excludes leads.

Response `{items: Listing[], total: number, total_matches: number, total_leads: number, page: number, page_size: number, pages: number}`. Items include match_state=`match` or `lead`; match rows ordered before leads, stable by id. Price sort only comparable totals then unknown, numeric not string sort. Missing scores last. Currency scoped USD initially. Use source quote but explicitly unknown destination; never assume mockup75067 is user location. When max_total set, unknown total cannot match; a base-price lead may remain only clearly in leads group when itemprice doesn't already exceed cap.
GET `/api/listings/{id}`: full Listing or404.
GET `/api/facets`: `{models:[string], generations:[number], drive_sizes:[string], vendors:[string], model_vendors:{[model]:vendor}, generations_by_vendor:{[vendor]:[number]}, generation_labels:{[vendor]:{[String(generation)]:label}}}`. `vendors` is the fixed supported list in source order Dell, HPE, Supermicro, Lenovo, Cisco. `models` covers cataloged models plus active inventory models with a known vendor; `model_vendors` maps every one of them. Generations and labels come from the cited catalog only (e.g. Dell 14→"14G", HPE 10→"Gen10", Supermicro 11→"H11 / X11", Lenovo 1→"V1", Cisco 5→"M5").
GET `/api/status`: `{provider:"PerUnitPrice", coverage:string (OEM-neutral), source_urls:[string], source_updated_at:ISO|null, last_success_at:ISO|null, stale:boolean, stale_after_hours:number, sources:[{vendor, url, state:"never"|"success"|"partial"|"blocked"|"failed", last_attempt_at:ISO|null, last_success_at:ISO|null, source_updated_at:ISO|null, stale:boolean, inventory_count:number, message:string|null}], discovery:{state:"never"|"success"|"partial"|"blocked"|"failed",last_attempt_at:ISO|null,message:string|null}, enrichment:{state:same,last_attempt_at:ISO|null,message:string|null}, backoff:[{origin, blocked_until, reason}], inventory_count:number}`. `sources` is per vendor page in fixed order. Aggregate `source_updated_at` is the OLDEST vendor page snapshot (null while any page has never been applied) and `stale` is true when any vendor page is stale, so one fresh OEM never makes another stale or failing OEM look current. Additional fields okay; frontend must tolerate absent optional fields.

Discovery is source-local per vendor page: a page that fails, is blocked, or returns a snapshot older than the one already applied for that page keeps its last-good listings and freshness; absence-based archiving (7 days) applies only to listings last attributed to a complete, applied page. A block stops further requests to the shared origin for that run and sets origin backoff. Items listed on several vendor pages are deduplicated by eBay item id (first page in source order wins). Run state is `partial` when at least one page applied and any page did not succeed.

## Manual refresh

Config: `RACKSCOUT_REFRESH_ORIGINS` comma-separated exact `http`/`https` origins (`scheme://host[:port]`; no path, query, wildcard or credentials; scheme/host lowercased, default port dropped; invalid entries fail startup); empty/unset disables manual refresh. `RACKSCOUT_REFRESH_COOLDOWN_SECONDS` default 3600, minimum 60: minimum time between ACCEPTED requests, durable in PostgreSQL across pods/restarts; completion never resets it. Web and worker must use the same values.

GET `/api/refresh` (existing read-only DB dependency; never fetches external data): `{enabled:boolean, state:"idle"|"queued"|"running"|"succeeded"|"partial"|"blocked"|"failed", request_id:string|null, requested_at:ISO|null, started_at:ISO|null, finished_at:ISO|null, retry_at:ISO|null (cooldown end while active), retry_after_seconds:number (0 when no cooldown), collector_running:boolean (scheduled OR manual collector holds the global lock), can_refresh:boolean, message:string|null}`. `can_refresh` is false when disabled, queued/running, any collector running, or cooling down.

POST `/api/refresh` accepts no options. Required: `Origin` exactly equal to a configured origin (missing, `null` or foreign → 403); `Sec-Fetch-Site`, when present, `same-origin` (else 403); `X-RackScout-Refresh: 1` (else 403); media type `application/json` (else 415); body at most 1024 bytes (else 413) and exactly the JSON object `{}` (anything else → 422). Disabled → 403. All of these are checked before any database access; 4xx rejections are `{detail}`; no CORS headers are ever sent. Then, atomically on the singleton control row: 202 `accepted` (status JSON, state `queued`); 409 when a request is queued/running or a collector holds the lock (status JSON); 429 during cooldown (status JSON plus `Retry-After` seconds). Database problems → sanitized 503 `{"detail":"Database unavailable"}`. Every other non-GET/HEAD method stays 405.

The API only enqueues. `python -m rackscout refresh-worker` polls every 5 s; it claims a queued request only after taking the same global collector advisory lock as `collect` and holds that session lock until the terminal state is committed, so manual and scheduled collections never overlap, several worker pods (rolling update) never double-claim, and a queued request simply waits while another collector runs. It runs the normal collector in-process with its own settings (`RACKSCOUT_MAX_DETAILS`, `RACKSCOUT_BROWSER_ENABLED`, timeouts) and honors every stored source backoff; there is no force/override. Terminal state reflects the run's own recorded outcomes: discovery `failed` → `failed`, discovery `blocked` → `blocked`, discovery `success` with enrichment `success`/skipped → `succeeded`, otherwise `partial` (e.g. browser page checks disabled). Each manual collection is bounded to 1800 s by an in-process deadline (→ `failed`). Messages never carry exception text. SIGTERM stops new claims; a collection killed mid-run leaves `running` with the lock free, which the next worker poll (or POST) marks `failed` without re-running. Queued requests survive restarts. Migration `003_manual_refresh` is additive (adds `refresh_control` only).

## Listing shape

All listed fields always present unless noted. Unknown values null, NOT zero. Monetary fields decimal strings, not floats. Dates ISO8601 UTC or null.

```
{
 id: number,
 item_id: string,
 variant_id: string|null,
 variant_label: string|null,
 title: string,
 vendor: "Dell"|"HPE"|"Supermicro"|"Lenovo"|"Cisco"|null,   // null = unknown OEM
 model: string|null,              // Dell keeps bare PowerEdge keys (R640); others vendor-qualified ("HPE DL360 Gen10", "Supermicro SYS-1029U-TR4", "Lenovo SR630", "Cisco UCS C220 M5"); null when no system model is identifiable
 generation: number|null,         // vendor-scoped catalog generation number
 generation_label: string|null,   // vendor label for display, e.g. "14G", "Gen10", "X11", "V1", "M5"
 target_era: boolean|null,        // cataloged Dell-14G-era-or-newer platform; null when uncataloged
 rack_units: number|null,
 seller: string|null,
 condition: string|null,
 cpu_model: string|null,           // e.g. Gold 6138, no quantity prefix
 cpu_count: number|null,
 socket_capacity: number|null,
 total_cores: number|null,
 ram_gb: number|null,
 drive_bays: number|null,
 drive_size: "SFF"|"LFF"|null,
 storage_state: "diskless"|"included"|"unknown",
 storage_conf: "confirmed"|"inferred"|"unknown"|"conflicting",   // passthrough of the stored confidence; storage_state "unknown" with storage_conf "conflicting" is conflicting evidence, not absence; unknown is not diskless
 hardware_state: "confirmed"|"review"|"missing"|"damaged",
 damage: boolean|null,
 price_kind: "exact"|"base"|"range"|"unknown",
 offer: {kind:"configuration"|"historical_configuration"|"multiple_options"|"unverified", price_is_starting:boolean, specification_price_paired:boolean, evidence:[string]},
 item_price: string|null,
 shipping_price: string|null,
 quoted_total: string|null,
 currency: "USD",
 shipping_context: string,
 match_state: "match"|"lead",
 source_url: string,              // canonical allowlisted https eBay /itm/<numeric id>
 source_updated_at: string|null,
 last_seen_at: string|null,
 last_checked_at: string|null,
 page_verified_at: string|null,   // last successful page/variant verification, never latest failed attempt
 page_checked_at: string|null,    // most recent attempted page check
 page_verification_current: boolean,
 detail_status: "never"|"verified"|"partial"|"blocked"|"failed",
 evidence: [{field:string,state:"confirmed"|"inferred"|"unknown"|"conflicting",text:string,source_url:string|null}],
 warnings: [string],
 exclusions: [string],
 benchmark: {cpu_mark:number,single_thread:number,samples:number,version:string,scope:"single_cpu",source_url:string,retrieved_at:string}|null
}
```

Details and list may share same fields. Frontend uses textContent/created elements, not raw source HTML. Dynamic URLs validated against https exact eBay item/cpubenchmark hosts before links. Missing score displays Not available. Render `1 of 2 — Single populated`, `2 of 2 — Dual populated`, `1 of 1 — Single populated`; unknown never becomes single. Benchmark average per CPU; NEVER multiply by count. Benchmark sorted perCPU, not estimated serverperf. Detail evidence must explain base/variant uncertainty and excluded accessories; index evidence cites the vendor index page the observation came from. Cited chassis metadata (`rackscout/data/models.json`: vendor, generation, generation_label, target_era, rack_units, socket_capacity, manufacturer source_url/source_text) is seeded by `migrate`; benchmark data only from `RACKSCOUT_BENCHMARKS_FILE`; inventory never seeded. Catalog entries are exact manufacturer SKUs/platforms only, never prefix-inferred; chassis-only or motherboard-only listings (e.g. Supermicro CSE-119U with X11DPU) establish no model, height or socket capacity.

`offer` is computed from existing stored observations for both list and detail responses. `configuration` requires an exact quote and current applicable page verification; `historical_configuration` is an exact quote without current verification. Non-exact rows with positive evidence of configuration/hardware choices are `multiple_options`; remaining rows are `unverified`. Generic payment, shipping or upgrade marketing does not establish choices. `price_is_starting` applies only to numeric base/range quotes, never historical exact quotes or unknown prices. `specification_price_paired` is true only for a currently verified exact configuration. Original specification values/confidence remain intact. UI prominently shows starting prices with `+`, qualifies unpaired advertised RAM as `Listed`, exposes captured `variant_label`, preserves unknown shipping, and labels historical exact quotes. Neither the API nor UI claims complete enumeration of marketplace options.

Migration `002_multi_oem` is non-destructive: existing listings, configurations, price history and source runs are kept; pre-existing inventory is backfilled as vendor Dell from the Dell page and the Dell page freshness is backfilled from earlier discovery runs.

Page verification expires at query time after its recorded verification window (twelve hours by default for new successful checks), even without another collection run. Older observations without a recorded window retain a conservative six-hour fallback. A failed, blocked or disabled page check cannot refresh successful verification. Historical exact quotes remain visible only as unverified leads with stale warnings; show page verification separately from the latest detail attempt. Explicit missing essential hardware is a hard exclusion, and hide-damaged applies to matches and leads alike.

## Source parser interfaces required by parent's tests

`rackscout.sources.parse_index(html: str) -> IndexSnapshot`, snapshot has `.records: list[dict]`, `.updated_at` timezone-aware datetime. Record has `item_id`, `title`, `source_url`, `cpu_model`, `cpu_count`, `source_ram_gb`, `item_price: Decimal`, `shipping_price: Decimal|None`, `vendor` (canonical, HP→HPE, unknown brand → null), raw `brand`, and `model` (normalized key as in the listing shape; Supermicro system IDs come from the title because the Supermicro page's model column is only the brand) minimum. Full cfg/d payload parsing. `SourceError` is public exception for challenge/schema failures. The collector adds `index_url` (the vendor page) to stored records.

`rackscout.sources.parse_description(html: str) -> dict` extracts explicit claims with fields `ram_gb` (integer|null), `storage_state`, `exclusions` list and other fields as useful. Descriptions containing options do not automatically validate selected variant RAM. No script/style text returned as evidence. Recognize no RAM/0GB and no HDD/drives independently; don't equate no drives with no drive backplane. Caller combines source, title and description conservatively.
