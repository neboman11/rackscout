# RackScout

Self-hosted used-server finder with web filters, PostgreSQL inventory, configuration evidence, CPU benchmark references and explicit populated/available sockets.

- Repository: https://github.com/neboman11/rackscout
- Images: `ghcr.io/neboman11/rackscout`, tagged with the full source commit SHA.
- Intended hosting: private LAN or homelab. Public source/image publication does not expose your deployment or database.

## OEMs and default filters

Discovery covers Dell, HPE/HP, Supermicro, Lenovo and Cisco. Defaults are 1U, at least 16GB RAM, at least four bays, no included storage and damaged systems hidden. Eight or more bays are highlighted, not required.

The platform-era filter selects cataloged Dell 14G-era or newer hardware across vendors. Vendor generations are not interchangeable: HPE Gen10 is not Dell 10G, and Supermicro X11 is not a Dell generation. Select a vendor before selecting a generation. Disable the modern-platform filter to inspect older or unknown-era discoveries.

The Model picker accepts multiple selections: a listing may match any selected model while still satisfying the other filters. Clear all model selections to remove that constraint. Shared URLs retain every selection as repeated `model` parameters.

Catalog metadata establishes only cited chassis properties: vendor, generation, rack height and socket capacity. It never proves installed CPU count, RAM, bay backplane or PSU inclusion. Generic Supermicro motherboard/chassis descriptions do not establish a complete system model. Unknown values remain unknown.

## Source coverage and trust

The collector uses the complete embedded payloads from PerUnitPrice's five fixed vendor pages:

- https://perunitprice.com/servers/dell/
- https://perunitprice.com/servers/hpe/
- https://perunitprice.com/servers/supermicro/
- https://perunitprice.com/servers/lenovo/
- https://perunitprice.com/servers/cisco/

The combined `/servers/` page truncates its payload, so it is not used as a complete inventory. Vendor results are deduplicated by marketplace item ID. A failed, wrong-vendor or incomplete vendor fetch cannot archive that vendor's last-good listings or make its data fresh merely because another vendor succeeded. Mixed-system Supermicro lots remain unknown rather than inheriting one system's chassis metadata.

Coverage is US-located eBay US Buy It Now inventory that ships within the US, with the publisher's own exclusions. This is **not an exhaustive eBay scanner**. No eBay API key or developer account is required.

Seller descriptions and bounded browser checks add evidence. No CAPTCHA solving, proxy rotation, user login or unsandboxed browser fallback. A challenge, source schema change or failed check is not empty inventory or proof that an item sold.

Title RAM and starting prices are not verified configuration pairs. A listing titled 64GB can price its no-RAM option. Unverified/base-price leads are separate from matching configurations. Explicit missing operating essentials exclude a configuration; generic “tested” wording does not manufacture positive assertions for every component.

Configuration rows distinguish captured option prices from starting prices. Base/range quotes show **$XXX+**; advertised RAM is labelled **Listed** and is not promised at that price. Positive evidence of hardware choices produces **Multiple options available**; otherwise the row says **Configuration options unverified**, not that multiple options definitely exist. Open a row for the evidence and original server specifications.

Captured options keep separate rows with their option labels and individual prices. An expired exact quote is labelled historical, never presented as a current verified price or a starting-price lower bound. With browser checks disabled, RackScout cannot reliably enumerate current marketplace options; it keeps one unresolved listing rather than inventing configuration/price pairs. This presentation uses already-stored evidence and adds no scraping.

UI releases version the JavaScript and stylesheet URLs in `rackscout/static/index.html` alongside the package version, so existing browsers do not mix cached assets from older releases. Reload the page after an upgrade.

RAM and storage distinguish **Not included** (explicit absence), **Not stated** (missing evidence) and **Conflicting claims**. Missing details never imply zero RAM or diskless storage. Coordinated exclusions such as “No HDD, RAM, NC” preserve the original evidence and establish missing RAM/storage without guessing what an unknown acronym means. Explicitly missing operating essentials remain excluded from both matches and leads.

Shipping amounts are source quotes with an unconfirmed destination, not delivered prices for your postcode. Browser delivery preferences do not obtain a new quote when the source cannot provide one. Unknown shipping is not zero; unknown totals cannot satisfy a maximum-total filter as matches. Tax and import charges are excluded.

## Manual configurations

Open a row and choose **Edit details** to fill in specifications, included/missing components, an option label, notes and prices. Saving a source row creates a separate **Manually entered** copy; original source facts and evidence remain available. Untouched prefills keep their source confidence. Use **Use as manual** to explicitly affirm an unchanged value, or **Reset to source** to remove that field's manual assertion.

**Add configuration** creates another independent option for the same marketplace listing. Enter each RAM/CPU/component combination and its own item/shipping price; RackScout never generates combinations by mixing unrelated options. A new option does not inherit another option's price or shipping. Check the price-confirmation box only after checking that exact configuration; changing hardware clears it. Blank shipping is unknown, and explicit zero means free shipping.

Manual rows participate in filtering, sorting and pagination, with their own prices and matched CPU references. Manual quotes are labelled as entered by you, never browser-verified. Original source rows stay hidden while manual copies exist, even if the copies are filtered out, so a cheap base quote cannot reappear as a matching configuration. Removing the last copy restores the latest source row only if it is still active. Separate marketplace variants are unaffected.

Entries survive automatic collection. Recorded dates and source-change warnings stay visible; collection cannot overwrite your values. Stale concurrent saves return a conflict and retain the draft rather than overwriting another edit. Pending saves freeze controls; completing a fetch does not discard an open draft. At most 100 active manual configurations are allowed per listing.

Manual writes require the exact-origin allowlist `RACKSCOUT_EDIT_ORIGINS`, independently of collection permissions. For local development, use `http://localhost:8080,http://127.0.0.1:8080`; an empty value disables editing. Compose forwards this optional setting to the web service. Apartment enables it through `manualEditing.enabled`. All permitted private-LAN clients share the same saved entries; this is not per-user authentication.

**Fetch seller details** requests enrichment of that listing only, without fetching vendor index pages. It shares the global one-hour manual-refresh cooldown, six-hour listing cooldown, request pacing, description cache and source backoff. A queued worker performs the request; the web process does not scrape. Results can be cached, partial or blocked, and never overwrite manual configurations. Browser-disabled fetching can add seller-described specifications, but cannot reliably enumerate marketplace option prices.


## Local development

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 16+, optionally Playwright Chromium for page verification.

```sh
uv sync --frozen
uv run playwright install chromium
```

Set `DATABASE_URL` or standard `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` environment variables. Keep credentials in your secret manager or an untracked environment file; the application reads process environment.

```sh
uv run python -m rackscout migrate
uv run python -m rackscout collect --max-details 3 --no-browser
uv run uvicorn rackscout.api:app --host 127.0.0.1 --port 8080
```

Open http://localhost:8080. Inventory starts empty, never with demo records.

### Tests

Use an explicit dedicated test database. The test role needs permission to create schemas there.

```sh
RACKSCOUT_TEST_DATABASE_URL=postgresql://rackscout_test@localhost/rackscout_test \
  uv run --frozen pytest -q
```

Tests use unique schemas, reject non-test database names and missing hosts, and never drop `public` or fall back to the application's database URL. Database tests skip without a test DSN. Benchmark test fixtures are synthetic, not redistributed third-party scores.

## CPU core counts

RackScout bundles **214 CPU specification references**: 213 validated entries from [Compute Specs DB](https://github.com/elokwentnie/compute-specs-db), pinned to commit `9b64d1770b9fea2b749e3ad599172ad0bd900f68`, plus the official Intel [Bronze 3104 specification](https://www.intel.com/content/www/us/en/products/sku/123546/intel-xeon-bronze-3104-processor-8-25m-cache-1-70-ghz/specifications.html). The MIT notice is retained in `rackscout/data/CPU_SPECS_LICENSE`. Only model/core facts are bundled, not third-party benchmark scores.

Rows use reported/entered totals first. Otherwise, an exact CPU match supplies clearly labelled physical cores per CPU and, when populated CPU count is known, a reference total. Two Gold 6150 CPUs therefore give 18 cores per CPU and 36 total; a two-socket chassis alone does not establish a total. Detail views show the source/date and flag disagreements without changing entered values. These are model specifications, not proof of installed hardware or enabled cores.

In the editor, **Look up cores** displays a reference without modifying the draft. **Use N total cores** explicitly fills the field; save to keep it. Applying follows normal manual-field and price-confirmation rules. Unknown population or CPUs marked missing cannot fill an installed total. SKU versions and suffixes stay distinct; ambiguous, contradictory or unsupported names remain unknown rather than borrowing a similar CPU's count.

Lookup uses the packaged snapshot, with no API key, database migration, external request, extra scrape job or change to daily collection. New CPU coverage arrives with a reviewed catalog/release update; this is not a live vendor API. GET `/api/cpu-specs?cpu_model=Gold%206150&cpu_count=2` is also available.


## CPU benchmarks

Scores are reference averages for **one CPU**, not measurements of the advertised server. CPU count, chassis sockets and installed total cores are separate fields. Scores are never multiplied for dual-populated systems. Missing references show “Not available,” not zero.

**Numeric benchmark datasets are not bundled in the public repository or image.** [PassMark's reuse terms](https://www.passmark.com/services/market-analysis.php) restrict republishing scores compiled from its public pages. Configure an appropriately permitted dataset privately with `RACKSCOUT_BENCHMARKS_FILE`.

The JSON document contains `provider` and a `benchmarks` array. Each entry contains `cpu_model`, `cpu_mark`, `single_thread`, `samples`, `version`, `scope` (`single_cpu`), `source_url` and UTC `retrieved_at`. Source URLs and dates stay visible in listing details. Do not infer missing scores or use dual-CPU measurements as single-CPU references.

An unset dataset path leaves existing database references unchanged; a fresh database then has no scores. An explicitly configured missing or invalid dataset fails clearly instead of silently deleting reference data. Configure an external dataset before migration to seed/update references.

## Containers and publication

`Dockerfile` uses the Playwright 1.58.0 Python image matching the pinned dependency. The runtime is non-root; production workloads use a read-only root filesystem, writable temporary storage, no Kubernetes API token and no-new-privileges. No host credentials or Docker socket are mounted.

```sh
podman build -t localhost/rackscout:0.6.0 .
```

`compose.yaml` provides PostgreSQL, migration, web and on-demand collection. Export a strong `POSTGRES_PASSWORD` before starting it. Web binds to localhost.

```sh
podman compose up --build -d web
podman compose --profile collector run --rm collect
```

Do not destroy database volumes unless you explicitly intend data loss. Browser-enabled collectors add only `SYS_CHROOT` to the otherwise dropped capabilities; web and migration retain none. A failed Chromium sandbox is reported as failed enrichment, never worked around with `--no-sandbox`, privileged mode, `SYS_ADMIN` or unconfined seccomp.

GitHub Actions runs the PostgreSQL-backed suite before publishing an image tagged with the exact source commit SHA. Pull requests have read-only permissions and cannot publish. Only the main-branch publication job receives package-write permission.

## Collection lifecycle and configuration

Automatic collection runs once daily at **04:00 UTC** (`0 4 * * *`). The **Refresh data** button queues an additional collection using the same server-detail budget, source backoff and global collector lock. Manual requests have a one-hour cooldown; repeated clicks cannot create overlapping collections. Versioned migrations preserve inventory and history.

- `RACKSCOUT_MAX_DETAILS`: bounded description/page checks per run; default 25.
- `RACKSCOUT_BROWSER_ENABLED`: browser verification switch; default true.
- `RACKSCOUT_RECHECK_COOLDOWN_HOURS`: minimum interval between detail attempts; default 6.
- `RACKSCOUT_PAGE_VERIFICATION_HOURS`: successful page-verification lifetime; default 12, not shorter than the cooldown. Allow room for the cron interval too.
- `RACKSCOUT_BENCHMARKS_FILE`: optional privately provisioned reference JSON file.
- `RACKSCOUT_REFRESH_ORIGINS`: exact comma-separated origins allowed to request manual collection; empty disables manual requests.
- `RACKSCOUT_REFRESH_COOLDOWN_SECONDS`: persisted manual-request cooldown; default 3600 seconds.
- `RACKSCOUT_EDIT_ORIGINS`: exact comma-separated origins allowed to save manual configurations; empty disables editing independently of manual collection.

Discovery pages and origins are fixed/allowlisted, not user-supplied scrape URLs. `GET /api/refresh` only reads stored/control state; `POST /api/refresh` accepts an empty JSON object with the same-origin application header. The web process only queues work; a separate `python -m rackscout refresh-worker` process performs collection without Kubernetes API credentials. Restarted workers retain queued requests and mark interrupted running requests failed rather than replaying them.

Stored verification expires at read time, even without another collector run. Failed, blocked and browser-disabled attempts never renew successful verification. Historical quotes remain unverified leads with the last successful page check separate from the latest detail attempt. Legacy observations without a recorded expiry retain a conservative six-hour window.

At the default budget, up to 12 checks go to cheap previously verified target systems, 12 to new targets and at least one to the globally oldest candidate. Unused quota is filled without exceeding the cap. Budgets below three cannot guarantee progress in every lane; bounded collection cannot keep every listing continuously verified.

Source freshness is separate from local fetch time. Re-fetching an unchanged old snapshot does not make it fresh. Valid-source disappearance is not confirmed sold; absence-based archival requires seven days and a complete applicable vendor snapshot. Price history is retained for 90 days.

Manual refresh and editing are intended only for a trusted private LAN. Origin/header checks protect against browser cross-site requests, not untrusted clients that forge headers. Do not expose these controls publicly without authentication. Daily collection reduces request volume, not the risk of provider restrictions or the need for permission. Source freshness/verification expiry remain explicit; the daily schedule does not make old data current.

## Apartment deployment

GitOps chart: `~/Desktop/argocd-definitions/application/rackscout/`.
Apartment values: `environments/apartment/application/rackscout.yaml`.
Private route: `http://rackscout.apartment` through the existing `local-gateway`.

Resources include CNPG PostgreSQL (one instance, 5Gi replicated Mayastor storage), migration Job, web Deployment/Service, isolated manual-refresh worker sidecar and daily collection CronJob. The worker uses the same immutable image, its own resources and temporary volume, a 30-minute collection deadline, and no Kubernetes API token or Job-creation permissions. The home ApplicationSet excludes RackScout. Database resources are protected from Argo prune/deletion; that is not a backup strategy.

The apartment browser remains disabled: offline probes observed user-namespace creation denied (EPERM) under the intended RuntimeDefault, non-root, no-new-privileges policy, including a pod-user-namespace probe. No compatible RuntimeClass is installed. Seccomp is the likely blocker, inferred from syscall behavior rather than direct profile/audit inspection. HTTP discovery and seller-description enrichment remain enabled. Do not enable browser checks until a sandboxed launch works; no unsandboxed or weakened-policy fallback is permitted.

Private benchmark references are supplied through the existing Secret named by `benchmarks.existingSecret`, mounted read-only at `/etc/rackscout/benchmarks.json`. Secret data is not stored in Git or the image. Keep the permitted source dataset in your own secure configuration/backup system.

Deploy only a published immutable image reference. Verify `/ready`, collection Job logs, each vendor's freshness/status, actual database inventory, real filters/details and the private route. Never delete existing cluster PVCs to recover this app.

Upgrading from a release without manual-row collector guards requires a clean cutover. The chart uses Recreate for web/worker; drain old-image CronJob collectors too before enabling manual writes. Do not let an old collector run against newly created manual rows.
