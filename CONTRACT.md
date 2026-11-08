# RackScout implementation contract

Authoritative interface between independent implementation lanes. Full approved spec: `/home/nesbitt/Desktop/argocd-definitions/docs/superpowers/specs/2026-11-02-rackscout-design.md`.

## Ownership

Backend owns `rackscout/*.py`, `rackscout/migrations/`, `rackscout/data/`, `tests/`. Frontend owns only `rackscout/static/index.html`, `app.css`, `app.js`. Parent owns pyproject/uv.lock, Docker/Compose, README, GitOps chart, integration and verification. No agent builds/lints/tests/formatters mid-flight. Parent writes initial source behavioral tests and runs failing baseline; backend may add tests but not overwrite parent's tests. No commits/pushes by agents. Any needed interface change must be messaged to parent before modifying contract.

## Runtime

Python 3.12+, package `rackscout`. Serve `uvicorn rackscout.api:app --host 0.0.0.0 --port 8080`. CLI `python -m rackscout migrate` and `python -m rackscout collect` (flags `--max-details N`, `--no-browser` permitted for smoke). `python -m rackscout refresh-worker` (no flags) is the long-running manual-refresh worker; see Manual refresh. Runtime env `DATABASE_URL` or standard PGHOST,PGPORT,PGDATABASE,PGUSER,PGPASSWORD. Config `RACKSCOUT_MAX_DETAILS` default25; `RACKSCOUT_BROWSER_ENABLED` defaulttrue; timeout/request delay/cooldown may be configurable. Discovery sources are fixed in code, not configurable: the five PerUnitPrice vendor index pages `https://perunitprice.com/servers/{dell,hpe,supermicro,lenovo,cisco}/`, fetched in that order through the same polite allowlisted fetcher (stable identifying `RackScout/…` User-Agent, no spoofing, per-origin delay, no challenge evasion). The combined `/servers/` page is not used: it embeds only a truncated 1000-row subset of its advertised inventory. The former `RACKSCOUT_SOURCE_URL` setting no longer exists.

`RACKSCOUT_REQUEST_DELAY` defaults to 300 seconds (range 1..300). The collector waits this quiet gap between whole listing-detail attempts, including cached descriptions, browser-disabled checks and source backoffs. HTTP fetching independently retains per-origin request-start spacing using the same setting; this does not rate-limit every browser subresource request. Apartment automatic collection runs Sundays at 04:00 UTC (`0 4 * * 0`), with a 43200-second job limit matching the manual worker deadline. Candidate budget, locks, cache, listing cooldowns and source backoffs remain unchanged; weekly cadence never extends verification expiry.

US retailer discovery follows the fixed eBay vendor pages: `TechMikeNY` (`https://techmikeny.com/api/agent/collections/servers`), `SaveMyServer` (`https://savemyserver.com/collections/pre-configured-servers/products.json`), and `CloudNinjas` (`https://cloudninjas.com/collections/cloud-ninjas-servers/products.json`). `RACKSCOUT_RETAILERS_ENABLED` defaults true. Only fixed allowlisted public catalog/product GETs; no arbitrary URLs, cart/build POSTs, foreign retailers or assumed currency. Explicit USD evidence is mandatory. One global detail budget, collector lock, five-minute pacing and per-origin backoff cover all providers.

`RACKSCOUT_BENCHMARKS_FILE` (optional): path to an external private per-CPU benchmark JSON (`{provider, benchmarks:[{cpu_model, cpu_mark, single_thread, samples, version, scope:"single_cpu", source_url (https), retrieved_at}]}`). No benchmark dataset is bundled in the package or image. Unset: `migrate` skips benchmark synchronization and leaves previously stored benchmark rows untouched; listings without a stored score show benchmark null (Not available). Set but missing, unreadable or malformed: `migrate` fails with a clear error naming the variable and changes nothing. When set and valid the file is authoritative (rows not in it are removed).

## HTTP

GET `/health`: `{"status":"ok"}` process only.
GET `/ready`: 200 `{"status":"ready"}` if DB/schema usable; otherwise503 generic detail.
GET `/api/listings`: filters `vendor` (Dell/HPE/Supermicro/Lenovo/Cisco, `HP` accepted as alias of HPE, empty means all, otherwise 422), `model` (vendor-qualified key; repeatable: `?model=R440&model=R640` selects the union (OR) of the listed models, ANDed with every other filter; a single `?model=R640` keeps working; blank/absent means all models; values are trimmed, blanks ignored, duplicates removed preserving order; each must match `[A-Za-z0-9][A-Za-z0-9 +\-]{0,59}` else the whole request is 422; more than 100 values is 422; models may span OEMs when `vendor` is empty, and combined with `vendor` only models of that vendor can match; values are bound SQL parameters), `generation` (vendor-scoped platform generation number, default none; nonempty without `vendor` is 422 because Dell 14G, HPE Gen10, Supermicro X11 etc. are not equivalent numbers), `modern_only` defaulttrue (only cataloged platforms with target_era; false also shows older or unknown-era data), `height` default1, `min_ram` default16, `min_bays` default4, `drive_size` SFF/LFF/empty, `cpu_count` 1/2/empty, `max_total` optional decimal, `diskless` defaulttrue, `hide_damaged` defaulttrue, `confirmed_only` defaultfalse, `match_status` defaultall (all/match/lead; trimmed and lowercased, blank means all, other values 422), `q` max200, `sort` one of price/ram/bays/benchmark, `page` default1, `page_size` default50 max100. Empty numeric query parameter means no constraint; frontend must send empties for Any. Unknown/conflicting fields cannot satisfy positive constraints as matches; unknown generation, era or rack height never satisfies a positive chassis constraint. Unverified leads may be included when no known contradiction, but never called matches; confirmed_only excludes leads. Status filtering precedes counting and pagination; confirmed_only with match_status=lead yields no rows. Legacy include_leads control is removed.
Repeated `provider` parameters select the union of known acquisition providers, ANDed with all other filters; unknown values are 422. Provider keys are `PerUnitPrice`, `TechMikeNY`, `SaveMyServer`, `CloudNinjas`; OEM `vendor` remains independent.
Hardware filters repeat `cpu_manufacturer`, `cpu_architecture` and `memory_platform`, with OR within each and AND across fields. Empty means unconstrained; values are validated against the packaged reference labels and de-duplicated. Selected hardware filters are hard exclusions for unknown/conflicting values, including leads. Facets add `cpu_manufacturers`, `cpu_architectures` and `memory_platforms`.

`include_sold` is a boolean filter, default false: exclude every configuration whose marketplace listing has a manual sold mark. True includes those rows for inspection/undo but keeps all other filters, source ended/archived exclusions and global price ordering. `match_state` still describes hardware/price eligibility; availability is separate, and the UI substitutes a Marked sold badge for those rows.

Response `{items: Listing[], total: number, total_matches: number, total_leads: number, page: number, page_size: number, pages: number}`. Items include match_state=`match` or `lead`, displayed as Match or Unverified row badges. Every sort is global, without match-first priority; ties use id ASC. Price sort is numeric `coalesce(quoted_total, item_price) ASC NULLS LAST`: known quoted total, otherwise item-price lower bound, missing prices last. Unknown shipping remains additional, not free. Other numeric sorts also place missing values last. Currency scoped USD initially. Use source quote but explicitly unknown destination; never assume mockup75067 is user location. When max_total set, unknown total cannot match; a base-price lead may remain only clearly badged Unverified when itemprice doesn't already exceed cap.
GET `/api/listings/{id}`: full Listing or404.
GET `/api/facets`: `{models:[string], generations:[number], drive_sizes:[string], vendors:[string], model_vendors:{[model]:vendor}, generations_by_vendor:{[vendor]:[number]}, generation_labels:{[vendor]:{[String(generation)]:label}}}`. `vendors` is the fixed supported list in source order Dell, HPE, Supermicro, Lenovo, Cisco. `models` covers cataloged models plus active inventory models with a known vendor; `model_vendors` maps every one of them. Generations and labels come from the cited catalog only (e.g. Dell 14→"14G", HPE 10→"Gen10", Supermicro 11→"H11 / X11", Lenovo 1→"V1", Cisco 5→"M5").
Facets additionally return `providers:[{id:string,name:string}]`, including `PerUnitPrice` displayed as `eBay (PerUnitPrice)`.
GET `/api/status` retains aggregate discovery/enrichment/backoff fields and adds per-source `provider` and display `platform`. `sources` contains the five eBay vendor pages, then enabled retailers with `vendor:null`. Counts sum all OEMs within each retailer. Aggregate `source_updated_at` remains the oldest eBay publisher snapshot; retailer freshness uses its latest successful local observation, since no catalog snapshot timestamp is supplied. Aggregate `stale` is true if any enabled source is stale. A failure cannot make another source empty or renew its quote.

Discovery is source-local per vendor page: a page that fails, is blocked, or returns a snapshot older than the one already applied for that page keeps its last-good listings and freshness; absence-based archiving (7 days) applies only to listings last attributed to a complete, applied page. A block stops further requests to the shared origin for that run and sets origin backoff. Items listed on several vendor pages are deduplicated by eBay item id (first page in source order wins). Run state is `partial` when at least one page applied and any page did not succeed.

### Database-session recovery

Scheduled and manual collections use the same `CollectionSession` for database work and the global collector advisory lock. Only transient session loss is recoverable: at most three recoveries per collection, each bounded to 120 seconds. Reconnection must reacquire the exclusive lock and pass persisted run/request ownership guards; another owner, superseded state or unknown commit outcome aborts without further marketplace requests. A still-running collector may resume after a database restart; a stopped process is never replayed automatically.

Only atomic database checkpoints are retried. Acquired index/product/description/browser observations, candidate order, counters, detail budget and fetcher pacing remain in memory. PostgreSQL transaction status resolves lost commit acknowledgements; stable run identities prevent duplicate phase rows. HTTP ownership checks run after per-origin waits, and actual request starts remain at least the configured delay apart even when recovery is slow. Provider backoff expiry is anchored to the original block observation and never shortened.

Under the exclusive collector lock, a new collection or a worker identifying an abandoned manual request finalizes unfinished source phase records as failed without discarding committed inventory. Manual terminal updates retain the same request identity and cannot overwrite a newer request. No schema change, source retry, backoff reset or additional scheduled collection accompanies this recovery behavior.


## Manual refresh

Config: `RACKSCOUT_REFRESH_ORIGINS` comma-separated exact `http`/`https` origins (`scheme://host[:port]`; no path, query, wildcard or credentials; scheme/host lowercased, default port dropped; invalid entries fail startup); empty/unset disables manual refresh. `RACKSCOUT_REFRESH_COOLDOWN_SECONDS` default 3600, minimum 60: minimum time between ACCEPTED requests, durable in PostgreSQL across pods/restarts; completion never resets it. Web and worker must use the same values.

GET `/api/refresh` (existing read-only DB dependency; never fetches external data): `{enabled:boolean, state:"idle"|"queued"|"running"|"succeeded"|"partial"|"blocked"|"failed", request_id:string|null, requested_at:ISO|null, started_at:ISO|null, finished_at:ISO|null, retry_at:ISO|null (cooldown end while active), retry_after_seconds:number (0 when no cooldown), collector_running:boolean (scheduled OR manual collector holds the global lock), can_refresh:boolean, message:string|null}`. `can_refresh` is false when disabled, queued/running, any collector running, or cooling down.

POST `/api/refresh` accepts no options. Required: `Origin` exactly equal to a configured origin (missing, `null` or foreign → 403); `Sec-Fetch-Site`, when present, `same-origin` (else 403); `X-RackScout-Refresh: 1` (else 403); media type `application/json` (else 415); body at most 1024 bytes (else 413) and exactly the JSON object `{}` (anything else → 422). Disabled → 403. All of these are checked before any database access; 4xx rejections are `{detail}`; no CORS headers are ever sent. Then, atomically on the singleton control row: 202 `accepted` (status JSON, state `queued`); 409 when a request is queued/running or a collector holds the lock (status JSON); 429 during cooldown (status JSON plus `Retry-After` seconds). Database problems → sanitized 503 `{"detail":"Database unavailable"}`. Every other non-GET/HEAD method stays 405.

The API only enqueues. `python -m rackscout refresh-worker` polls every 5 s; it claims a queued request only after taking the same global collector advisory lock as `collect` and holds that session lock until the terminal state is committed, so manual and scheduled collections never overlap, several worker pods (rolling update) never double-claim, and a queued request simply waits while another collector runs. It runs the normal collector in-process with its own settings (`RACKSCOUT_MAX_DETAILS`, `RACKSCOUT_BROWSER_ENABLED`, timeouts) and honors every stored source backoff; there is no force/override. Terminal state reflects the run's own recorded outcomes: discovery `failed` → `failed`, discovery `blocked` → `blocked`, discovery `success` with enrichment `success`/skipped → `succeeded`, otherwise `partial` (e.g. browser page checks disabled). Each manual collection is bounded to 43200 s by an in-process deadline (→ `failed`), allowing paced discovery and the unchanged 25-detail budget. Messages never carry exception text. SIGTERM stops new claims; a collection killed mid-run leaves `running` with the lock free, which the next worker poll (or POST) marks `failed` without re-running. Queued requests survive restarts. Migration `003_manual_refresh` is additive (adds `refresh_control` only).

## Listing shape

All listed fields always present unless noted. Unknown values null, NOT zero. Monetary fields decimal strings, not floats. Dates ISO8601 UTC or null.

```
{
 id: number,
 item_id: string,
 provider: "PerUnitPrice"|"TechMikeNY"|"SaveMyServer"|"CloudNinjas",
 platform: string,
 quote: {method:"retailer_catalog"|"retailer_product",observed_at:string,fresh_until:string,current:boolean,status:"success"|"blocked"|"failed"}|null,
 variant_id: string|null,
 variant_label: string|null,
 origin: "source"|"manual",
 revision: number,
 essential_states: {[key:string]:"confirmed"|"present"|"unknown"|"missing"|"review"}|null,
 next_check_after: string|null,
 manual_sold_at: string|null,       // Listing-level user override; ISO timestamp or null, never source sold evidence.
 manual: {source_configuration_id:number,revision:number,created_at:string,updated_at:string,fields:[string],notes:string,price_confirmed:boolean,source_changed:boolean}|null,
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
 cpu_manufacturer: "Intel"|"AMD"|null,
 cpu_architecture: string|null,
 memory_platform: "DDR3"|"DDR4"|"DDR5"|null,
 memory_platform_source: "listing"|"cpu_reference"|null,
 hardware_reference: {source_url:string,provider:string,retrieved_at:string}|null,
 cpu_count: number|null,
 socket_capacity: number|null,
 total_cores: number|null,
 cpu_spec: {cpu_model:string,cores_per_cpu:number,provider:string,source_url:string,retrieved_at:string}|null,
 inferred_total_cores: number|null, // model reference × known populated CPUs; never overwrites total_cores
 ram_gb: number|null,
 drive_bays: number|null,
 drive_size: "SFF"|"LFF"|null,
 storage_state: "diskless"|"included"|"unknown",
 storage_conf: "confirmed"|"inferred"|"unknown"|"conflicting",   // passthrough of the stored confidence; storage_state "unknown" with storage_conf "conflicting" is conflicting evidence, not absence; unknown is not diskless
 hardware_state: "confirmed"|"review"|"missing"|"damaged",
 damage: boolean|null,
 price_kind: "exact"|"base"|"range"|"unknown",
 offer: {kind:"configuration"|"historical_configuration"|"manual_configuration"|"manual_unconfirmed"|"multiple_options"|"unverified", price_is_starting:boolean, specification_price_paired:boolean, evidence:[string]},
 item_price: string|null,
 shipping_price: string|null,
 quoted_total: string|null,
 currency: "USD",
 shipping_context: string,
 match_state: "match"|"lead",
 source_url: string,              // allowlisted eBay /itm/<id> or retailer /products/<handle>, with native ?variant=<id> when applicable
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

Details and list may share the same fields. Frontend uses textContent/created elements, not raw source HTML. Dynamic links require HTTPS, exact allowed hosts and applicable path/query grammar: eBay items, retailer products, CPU references and cited manufacturer metadata. Missing score displays Not available. Render `1 of 2 — Single populated`, `2 of 2 — Dual populated`, `1 of 1 — Single populated`; unknown never becomes single. Benchmarks are per CPU, never multiplied by count. Detail evidence explains base/variant uncertainty and excluded accessories; source evidence cites the actual acquisition page. Cited chassis metadata is seeded by `migrate`; benchmark data comes only from `RACKSCOUT_BENCHMARKS_FILE`; inventory is never seeded. Catalog entries are exact manufacturer SKUs/platforms only, never prefix-inferred; chassis-only or motherboard-only listings establish no complete system model, height or socket capacity.

`offer` is computed from stored observations for list and detail responses. `configuration` requires an exact quote and current applicable evidence: eBay page verification or a successful unexpired retailer quote. `historical_configuration` is exact without current evidence. Only actual native alternatives or explicit hardware-choice wording establish `multiple_options`; missing specifications alone do not. Other base leads are `unverified`. Numeric base/range prices show `+`, never historical exact quotes. Retailer quotes are not browser verification; retailer `page_verified_at`/`page_checked_at` stay null and `page_verification_current` false. Manual rows have `quote:null`. Unknown shipping remains null.

Known item prices with unknown shipping display `<item price> + shipping` (starting prices use `From`). quoted_total stays null and unknown shipping is never zero. Price ordering uses the known total or item-price lower bound as documented above; maximum-total eligibility and historical/manual pairing rules are unchanged.

Migration 007 seeds exact CPU platform references and stores configuration `cpu_platform_key`, explicit `memory_type` and typed `memory_type_conflict`, backfilling existing rows without revision/history/manual-metadata changes. Collector and manual persistence derive keys through the same offline canonical reference lookup. Memory type/conflict are frozen with manual baselines and cleared when the manual CPU changes; old baselines never borrow current source memory facts. Explicit mixed generations block CPU-reference fallback and positive DDR filters. A newly observed source-memory conflict is a material source change; identical rediscovery and reference reclassification are not. Dual-DDR CPUs need listing evidence. Architecture/platform references never establish installed hardware or CPU count.

Migration `002_multi_oem` is non-destructive: existing listings, configurations, price history and source runs are kept; pre-existing inventory is backfilled as vendor Dell from the Dell page and the Dell page freshness is backfilled from earlier discovery runs.

Page verification expires at query time after its recorded verification window (twelve hours by default for new successful checks), even without another collection run. Older observations without a recorded window retain a conservative six-hour fallback. A failed, blocked or disabled page check cannot refresh successful verification. Historical exact quotes remain visible only as unverified leads with stale warnings; show page verification separately from the latest detail attempt. Explicit missing essential hardware is a hard exclusion, and hide-damaged applies to matches and leads alike.

Migration 006 uses `UNIQUE(provider,item_id)` and fixed provider-specific ASCII native-ID/URL checks, preserving existing IDs/manual rows/history. `source_pages` gains provider identity and nullable OEM; source configurations gain `source_quote` JSON, forbidden on manual rows. Migration takes the collector advisory transaction lock before identity changes; exact schema-version guards prevent old releases writing after cutover.

Retailer configuration building uses only explicit installed facts and actual native variants. Capacity/maximum prose is not installed hardware. Chassis/builder/default/unexplained prices remain unpaired; quote-only/unavailable offers are not active. Published option labels remain visible even when their hardware meaning is unresolved. Quote timestamps derive only from an actual observation, never recomputation/manual editing. Incomplete snapshots cannot remove unseen products/variants or renew unseen quotes. Failed/blocked checks preserve last-good specifications/prices and mark the attempt non-current. Explicit unavailable observations must deactivate the affected offers even when incomplete data cannot safely replace common hardware facts.

## Source parser interfaces required by parent's tests

`rackscout.sources.parse_index(html: str) -> IndexSnapshot`, snapshot has `.records: list[dict]`, `.updated_at` timezone-aware datetime. Record has `item_id`, `title`, `source_url`, `cpu_model`, `cpu_count`, `source_ram_gb`, `item_price: Decimal`, `shipping_price: Decimal|None`, `vendor` (canonical, HP→HPE, unknown brand → null), raw `brand`, and `model` (normalized key as in the listing shape; Supermicro system IDs come from the title because the Supermicro page's model column is only the brand) minimum. Full cfg/d payload parsing. `SourceError` is public exception for challenge/schema failures. The collector adds `index_url` (the vendor page) to stored records.

`rackscout.sources.parse_description(html: str) -> dict` extracts explicit claims with fields `ram_gb` (integer|null), `storage_state`, `exclusions` list and other fields as useful. Descriptions containing options do not automatically validate selected variant RAM. No script/style text returned as evidence. Recognize no RAM/0GB and no HDD/drives independently; don't equate no drives with no drive backplane. Caller combines source, title and description conservatively.

## Manual configurations and selected-listing refresh

Approved behavior: edit a chosen row, save multiple explicit configuration/price pairs as separate rows, preserve manual work across collection, keep original evidence, remove manual copies to return to source data. No Cartesian expansion. Manual values participate in server-side filtering, sorting and pagination. A manual quote is never browser verification.

### Persistence and derivation

Migration 004 adds `origin` (`source` default or `manual`), `source_configuration_id`, structured `essential_states`, `manual_data`, positive integer `revision` (default 1), `manual_created_at`, and `manual_updated_at` to configurations. Source rows have no manual parent/metadata; manual rows reference a source configuration on the same listing and use server-generated `manual:<uuid>` keys with no marketplace variant ID. A partial index supports active manual children. Existing IDs, source records and history are retained.

Manual rows materialize typed effective values in the existing columns, not JSON overlays at query time. `manual_data` retains the immutable original baseline (configuration values, structured essentials, evidence, warnings and exclusions), explicit fields, their server-generated timestamps, price-pair assertion and notes. Source rows remain pristine. The collector only upserts/deactivates source-origin configurations, persists the domain builder's structured essentials, and increments source revision; it never rewrites manual children. Reconstruct legacy null essentials with the same pure domain builder and exact variant key, never by interpreting warning prose. Unknown reconstruction cannot erase a pre-existing missing/damage blocker.

Hide a source configuration from inventory whenever an active manual child exists, before filtering/counting/pagination and independently of whether that child passes filters. Manual children remain when source variants deactivate, but an ended/archived listing is excluded. Editor/detail/source reads bypass source-hiding. Removing the last child exposes the source only if it remains active/live.

Copied facts retain their source confidence. Only explicit assertions get manual evidence. Essential keys are `cpu`, `ram`, `psu`, `cooling`, `nic`, `drive_path`; source states include `confirmed`, inferred `present`, `unknown`, `missing` and `review`. Manual presence assertions become `confirmed`, not independent/browser verification. Missing essentials are hard exclusions. Confirmed-hardware filtering requires all essentials confirmed, whether asserted by source evidence or the user; manual origin remains prominently identified. CPU edits update the normalized benchmark key and invalidate borrowed core totals unless explicitly supplied. Model edits must select a catalog model from the same vendor, and effective CPU count must fit that model.

### Editor service and HTTP

Backend module `rackscout.manual` supplies `parse_write(payload)`, `editor(conn, configuration_id)`, `create(conn, configuration_id, parsed) -> new_id`, `update(conn, configuration_id, parsed) -> id`, and `delete(conn, configuration_id, revision) -> {deleted_id, source_id}`. Errors: `ManualValidationError` (422), `ManualNotFound` (404), `ManualConflict` (409). All writes lock listing then configuration, check revision atomically, and bound database waits. Maximum 100 active manual configurations per listing.

Write body: `{revision: positive integer, label: nonempty string <=120, notes: string <=2000, fields: object, price_confirmed: boolean}`. `fields` is the complete set of explicit overrides; omitted keys use the saved original baseline, and null clears a value. Unknown keys/types, non-finite money, more than two fractional decimal digits, invalid Unicode/NUL, incompatible model/vendor and out-of-range values are 422 before database access where possible. Money uses decimal strings, never floats. Monetary range 0..99999999.99.

Allowed fields: `model` (catalog key/null), `cpu_model` (text <=100/null), `cpu_count` (1..8/null), `total_cores` (1..8192/null), `ram_gb` (0..1048576/null), `drive_bays` (1..128/null), `drive_size` (SFF/LFF/null), `storage_state` (diskless/included/unknown), `cpu_included`, `psu`, `cooling`, `nic`, `drive_path` (each present/missing/unknown), `damage` (boolean/null), `item_price`, `shipping_price` (decimal strings/null).

GET `/api/listings/{id}/manual` is read-only and returns `{enabled, configuration_id, source_configuration_id, revision, label, notes, fields, values, price_confirmed, original, configurations}`. `values` contains editable effective values and component inclusion states; `fields` contains only explicit overrides. `original` is the baseline source information. `configurations` is the active manual sibling summary list with id, label, revision, RAM, item/shipping price and price-pair status.

POST `/api/listings/{id}/manual` creates one manual child from the selected source/manual row (root source parent retained), returns 201 `{item: Listing}`. PATCH the same path updates a manual row only, returns 200 `{item: Listing}`. DELETE accepts exactly `{revision}` and removes a manual row only, returning 200 `{deleted_id, source_id}`. Stale revisions return 409 without mutation. Source deletion/updates through these endpoints are prohibited.

Manual writes require `RACKSCOUT_EDIT_ORIGINS` exact origin allowlist, matching Origin, Sec-Fetch-Site same-origin if supplied, `X-RackScout-Edit: 1`, application/json, and at most 32768 streamed bytes checked before accumulation. Empty origins disable writes. These checks and payload validation precede DB access. Other methods/paths remain blocked; existing GET connections remain read-only. This is trusted-LAN CSRF protection, not authentication.

Listing metadata adds `origin`, `revision`, `essential_states`, `next_check_after` and `manual` (null for source, otherwise `{source_configuration_id, revision, created_at, updated_at, fields:[names], notes, price_confirmed, source_changed}`). Detail/editor retains original evidence. Browser verification applies only to source rows.

### Manual price semantics

Explicit `price_confirmed` plus a numeric price asserts that quote for this configuration. It produces offer kind `manual_configuration`, a precise price and filter eligibility without page verification. UI says manually entered/not independently verified, never browser-verified. Unknown shipping remains null; free shipping requires explicit zero. Do not silently carry shipping from another option into a newly entered configuration price.

Without that assertion, explicitly entered item/shipping amounts remain visible and participate in numeric sorting and price ceilings, but do not establish configuration pairing or match eligibility. A known explicit item amount produces offer kind `manual_unconfirmed`, with `price_kind=unknown`, `price_is_starting=false` and `specification_price_paired=false`; unknown kind here means unconfirmed pairing, not a missing amount. Only explicit shipping is carried into this amount; null remains unknown, zero is free. Without an entered amount, retain only an unpaired source starting quote or unknown price, never a borrowed exact option quote. Editing configuration-defining fields clears confirmation but keeps entered amounts. Clearing the item-price override clears its displayed amount. Backend only honors explicit confirmation for the submitted configuration.

Migration 008 restores existing hidden explicit item/shipping amounts from unconfirmed manual metadata, only where both effective money columns were cleared. Item-only, shipping-only, explicit free shipping and cleared-item cases retain their original null/zero distinctions. It keeps price_kind unknown, source rows, manual metadata/revisions/timestamps, hardware, history and collection state unchanged. Stored unknown-total warning is corrected. It holds the collector cutover lock; old releases reject schema 008. Forward-only correction; restore the pre-upgrade backup before reverting to an older image.

### Manual sold marking

POST `/api/listings/{id}/sold` accepts exactly `{"sold":boolean}`, resolving the configuration ID to its marketplace listing. It uses the manual-edit Origin/header/body-limit protections and existing bounded listing/configuration locks, and returns 200 `{"item":Listing}`. Invalid bodies are 422 before database access; inactive, source-ended or archived targets are 404. Marking true repeatedly preserves the original timestamp; false clears only the manual mark, never source availability.

Migration 009 adds nullable `listings.manual_sold_at`, initialized null for every existing listing, under the collector cutover lock. Marking applies to all native variants and manual children on that marketplace listing; hardware, prices, revisions, notes and history are untouched. Source discovery never clears the mark. Scheduled detail selection and selected-refresh enqueue/execution skip marked listings without resetting backoffs or consuming a rejected request's cooldown. GET detail/editor remain accessible for manually marked live listings. The UI provides Mark listing as sold, Undo sold mark, and Include marked-sold listings; marks survive refreshes and restarts.

Release images use the bare semantic version from pyproject.toml, e.g. `ghcr.io/neboman11/rackscout:0.11.0`. GitOps uses that version tag, not a commit-hash tag, so service-update can compare versions. The image retains the commit revision as OCI provenance. Publishing refuses an existing release tag and fails closed on indeterminate registry errors; each subsequent release must bump the package version.

### Selected-listing fetch

POST `/api/listings/{id}/refresh` accepts `{}` with the existing refresh Origin/header/body protections. It resolves a configuration to its marketplace listing; no arbitrary URL. It shares the existing global refresh queue, collector lock, one-hour acceptance cooldown, request pacing and origin backoff. Per-listing `next_check_after` also applies; a cooling target returns 429/Retry-After without consuming the queue/global cooldown, missing/inactive targets return 404.
Retailer targets dispatch only to their provider's product endpoint; no catalog discovery, eBay description or browser check. Refresh status adds `target_provider` to qualify native item IDs. Existing queue, lock, cooldowns and origin protections remain unchanged.

Migration 005 adds nullable `refresh_control.target_listing_id` (FK RESTRICT); null means whole inventory. Refresh status adds `scope` (all/listing) and `target_item_id` (marketplace ID/null). Global admission resets target to null. Interrupted/shutdown handling preserves request scope; never replay or escalate a selected request to global work.

`collector.run_collection(..., *, target_listing_id=None)` and `run_enrichment(..., *, target_listing_id=None)` preserve default behavior. A selected run skips discovery, considers only that due active listing, performs at most one normal enrichment within the configured budget, and does not change discovery freshness. Missing/ended/archived/cooling queued targets fetch nothing. Existing description cache and backoff remain active. `CollectionResult.discovery` is skipped for this path; terminal state honestly reflects enrichment. Source recomputation cannot overwrite manual rows.

### Build order and verification

1. Write behavioral regressions for manual rows and selected fetching; observe missing behavior before implementation.
2. Add manual schema/service/query integration; parent integrates collector persistence guards and structured essentials.
3. Add protected CRUD and selected-fetch API admission; build the editor, per-configuration controls and progress UI against these interfaces.
4. Run complete isolated-PostgreSQL suite once integration settles, independent code/security reviews, then exercise real web CRUD/filtering/price pairing/reload/reset and a bounded selected fetch. Verify no source or sibling/manual overwrite and no extra index fetch.
5. Update existing docs, publish immutable image and chart version, deploy to apartment and verify actual surface/rollout.

Browser runtime: browser collectors may use the approved immutable amd64 Localhost seccomp profile derived from containerd 2.4.2's empty-capability default, adding only `clone`, `setns`, `unshare` and `chroot`. UID/GID 1000, all capabilities dropped, no-new-privileges, read-only root, Chromium sandbox and renderer filtering remain required. Web/migration retain RuntimeDefault. Before temporary scoped installer Jobs, trusted SSH preflight must reject unsafe host-path components, ownership, write access and ACLs on each exact node; failed checks prevent submission. Label nodes only after real offline renderer attestation; gate browser collectors on the digest label. No privileged, unconfined, SYS_ADMIN, unsandboxed or node-wide policy workaround. Marketplace challenges remain honest blocked outcomes with normal pacing/backoff.

## CPU core reference lookup

Use a bundled, pinned snapshot of validated CPU/core facts from the MIT-licensed Compute Specs DB, retaining its license notice. Include no benchmark scores. Supplement Bronze 3104 with its official Intel specification (6 physical cores) because it is present in apartment inventory but absent from the candidate CSV. Record provider, source URL and retrieval date. No lookup network requests, new dependencies, database migration or scrape schedule changes.

Data file `rackscout/data/cpu_specs.json`: `{provider,source_url,retrieved_at,cpus:[{cpu_model,cores_per_cpu,provider?,source_url?,retrieved_at?}]}`. Top-level provenance is inherited unless overridden by an entry. Use only validated positive integer core counts. Pin the upstream CSV source URL to its exact Git commit, not main.

New module `rackscout.cpu_specs` exports `lookup(cpu_model: str|None, cpu_count: int|None=None) -> {match,total_cores}`. `match` is null or `{cpu_model,cores_per_cpu,provider,source_url,retrieved_at}`. Cache the packaged catalog. Match exact canonical CPU SKUs with benign brand/trademark/clock normalization; preserve version and suffix distinctions. Ambiguous alternatives, unrecognized suffixes and contradictory manufacturers do not match. Reuse the existing basic CPU key normalizer without changing benchmark keys or inventory.

GET `/api/cpu-specs?cpu_model=...&cpu_count=...` returns that lookup result without database or external network access. Model text is at most 100 characters; optional populated count is an integer 1..8. Missing count means unknown total, never chassis capacity. Unknown CPU returns `{match:null,total_cores:null}`.

Listing serialization adds `cpu_spec` (match object/null) and `inferred_total_cores` (integer/null). Preserve existing `total_cores` exactly: it remains seller/user-entered data. Infer a reference total only for known CPU model and populated count with confirmed/inferred confidence and no explicit missing CPU; conflicting models/counts cannot produce inferred totals. No materialization or edits to source/manual rows.
Legacy NULL structured essentials plus an existing missing-hardware blocker withhold inferred totals conservatively. CPU text containing population prefixes is not treated as a plain SKU; quantity belongs in the separate populated count. Bare suffix tokens are retained, not mistaken for trademark notation.

UI prefers entered/reported totals; otherwise clearly labels reference-derived totals or per-CPU cores when population is unknown. Detail view includes provenance and flags mismatches without replacing reported data. Editor has **Look up cores** and an explicit **Use N total cores** action. Lookup alone never changes a manual value, price confirmation, or draft. Applying a result uses normal manual-field tracking and hardware price-invalidation rules. Changing CPU model/count/inclusion invalidates pending/results; stale responses cannot modify a different editor or newer input. Unknown/missing populations cannot fill a total.

CPU lookup verification: 18 API behavior regressions failed before implementation; the integrated suite and live editor/row smoke covered unknown/version/dual counts, manual precedence and stale responses before publishing 0.6.0. CPU lookup remains offline and independent of marketplace browser verification.
