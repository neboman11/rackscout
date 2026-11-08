# RackScout

Self-hosted used-server finder with web filters, PostgreSQL inventory, configuration evidence, CPU benchmark references and explicit populated/available sockets.

- Repository: https://github.com/neboman11/rackscout
- Images: `ghcr.io/neboman11/rackscout`, tagged with immutable semantic release versions.
- Intended hosting: private LAN or homelab. Public source/image publication does not expose your deployment or database.

## OEMs and default filters

Discovery covers Dell, HPE/HP, Supermicro, Lenovo and Cisco. Defaults are 1U, at least 16GB RAM, at least four bays, no included storage and damaged systems hidden. Eight or more bays are highlighted, not required.

The platform-era filter selects cataloged Dell 14G-era or newer hardware across vendors. Vendor generations are not interchangeable: HPE Gen10 is not Dell 10G, and Supermicro X11 is not a Dell generation. Select a vendor before selecting a generation. Disable the modern-platform filter to inspect older or unknown-era discoveries.

The Model and Selling platform pickers accept multiple selections. Each selection group is ORed, then ANDed with the other filters. Clear a group to remove that constraint. Shared URLs retain repeated `model` and `provider` parameters. Selling platform is independent of the server's OEM.

CPU manufacturer, CPU architecture family and Memory platform are also multi-selects. Selections are ORed within a picker and ANDed across pickers; selected hardware filters exclude unknown values from both matches and leads. URLs retain repeated `cpu_manufacturer`, `cpu_architecture` and `memory_platform` parameters. CPU cells show manufacturer/family; RAM cells show DDR generation.

Hardware platform metadata uses exact CPU model references, not chassis-generation guesses or nearby SKUs. Architecture names group platform suffixes into their broad family while retaining distinct families such as Rome and Milan. DDR generation comes from explicit listing evidence or an unambiguous CPU platform reference. CPUs supporting multiple DDR generations remain unknown without listing evidence; contradictory DDR claims also remain unknown. A DDR platform does not prove RAM is installed. Manual CPU edits cannot inherit an unrelated source configuration's memory type. References are packaged locally; filtering adds no scraper traffic.

The packaged platform catalog contains 223 exact CPU references across 24 broad architecture families, with pinned MIT dataset attribution and vendor evidence recorded in `rackscout/data/cpu_platforms.json`. Memory compatibility is verified by product series rather than inferred from codename alone. Unknown or unverified memory support stays unknown.

Catalog metadata establishes only cited chassis properties: vendor, generation, rack height and socket capacity. It never proves installed CPU count, RAM, bay backplane or PSU inclusion. Generic Supermicro motherboard/chassis descriptions do not establish a complete system model. Unknown values remain unknown.

## Source coverage and trust

The eBay connector uses the complete embedded payloads from PerUnitPrice's five fixed vendor pages:

- https://perunitprice.com/servers/dell/
- https://perunitprice.com/servers/hpe/
- https://perunitprice.com/servers/supermicro/
- https://perunitprice.com/servers/lenovo/
- https://perunitprice.com/servers/cisco/

The combined `/servers/` page truncates its payload, so it is not used as a complete inventory. Vendor results are deduplicated by marketplace item ID. A failed, wrong-vendor or incomplete vendor fetch cannot archive that vendor's last-good listings or make its data fresh merely because another vendor succeeded. Mixed-system Supermicro lots remain unknown rather than inheriting one system's chassis metadata.

eBay coverage is US-located eBay US Buy It Now inventory that ships within the US, with the publisher's own exclusions. This is **not an exhaustive eBay scanner**. No eBay API key or developer account is required.

Seller descriptions and bounded browser checks add evidence. No CAPTCHA solving, proxy rotation, user login or unsandboxed browser fallback. A challenge, source schema change or failed check is not empty inventory or proof that an item sold.

Additional US-based retailers use fixed public endpoints, not marketplace-page scraping:

- [TechMikeNY](https://techmikeny.com/llms.txt): documented agent API, `/api/agent/collections/servers` and product-by-handle lookups.
- [SaveMyServer](https://savemyserver.com/collections/pre-configured-servers): public preconfigured-server catalog and individual product JSON.
- [Cloud Ninjas](https://cloudninjas.com/collections/cloud-ninjas-servers): public server catalog and individual product JSON.

Retailer currency must be explicitly confirmed as USD. Unknown shipping stays unknown. Public Shopify product data may require its documented `.js` product endpoint to confirm stock; identity and price must agree with the USD quote. Missing stock data is not treated as sold or in stock. Requests use the same paced transport and origin backoff; a blocked source never triggers a bypass. Source status reports failures independently.

Native retailer variants retain their own prices and published option labels. Exact prices require explicit installed CPU/RAM facts for that native configuration. Chassis, builders, platform maximums, quote-only products and unexplained options never become complete-server deals at a minimum price. Merchant observations are labelled retailer quotes, **not browser verification**. ServerMonkey is not integrated: its public endpoints rejected access. Non-US retailers and currency conversion are excluded.

Title RAM and starting prices are not verified configuration pairs. A listing titled 64GB can price its no-RAM option. Rows carry **Match** or **Unverified** badges; the **Match status** filter selects all listings, matches only or unverified only. Results use one globally sorted list, never confidence-first groups. Explicit missing operating essentials exclude a configuration; generic “tested” wording does not manufacture positive assertions for every component.

Configuration rows distinguish captured option prices from starting prices. Base/range quotes show **$XXX+**; advertised RAM is labelled **Listed** and is not promised at that price. Positive evidence of hardware choices produces **Multiple options available**; otherwise the row says **Configuration options unverified**, not that multiple options definitely exist. Open a row for the evidence and original server specifications.

Captured options keep separate rows with their option labels and individual prices. An expired exact quote is labelled historical, never presented as a current verified price or a starting-price lower bound. With browser checks disabled, RackScout cannot reliably enumerate current eBay options; public retailer APIs can still provide native options. No combinations are synthesized from independent option lists.

UI releases version the JavaScript and stylesheet URLs in `rackscout/static/index.html` alongside the package version, so existing browsers do not mix cached assets from older releases. Reload the page after an upgrade.

RAM and storage distinguish **Not included** (explicit absence), **Not stated** (missing evidence) and **Conflicting claims**. Missing details never imply zero RAM or diskless storage. Coordinated exclusions such as “No HDD, RAM, NC” preserve the original evidence and establish missing RAM/storage without guessing what an unknown acronym means. Explicitly missing operating essentials remain excluded from both matches and leads.

Shipping amounts are source quotes with an unconfirmed destination, not delivered prices for your postcode. Browser delivery preferences do not obtain a new quote when the source cannot provide one. Unknown shipping is not zero; unknown totals cannot satisfy a maximum-total filter as matches. Tax and import charges are excluded.

When item price is known but shipping is not, the headline shows **$740.95 + shipping**, or **From $200.00 + shipping** for a starting price. **Price: low to high** sorts all rows numerically by the quoted total when known, otherwise by item price, with missing prices last and stable ID tie-breaking. Unknown shipping remains additional, never free; maximum-total eligibility and quote confidence are unchanged. Other sort modes also mix matches and unverified rows.

## Manual configurations

Open a row and choose **Edit details** to fill in specifications, included/missing components, an option label, notes and prices. Saving a source row creates a separate **Manually entered** copy; original source facts and evidence remain available. Untouched prefills keep their source confidence. Use **Use as manual** to explicitly affirm an unchanged value, or **Reset to source** to remove that field's manual assertion.

**Add configuration** creates another independent option for the same marketplace listing. Enter each RAM/CPU/component combination and its own item/shipping price; RackScout never generates combinations by mixing unrelated options. A new option does not inherit another option's price or shipping. Check the price-confirmation box only after checking that exact configuration; changing hardware clears it. Blank shipping is unknown, and explicit zero means free shipping.

Entered amounts stay visible even when the price-confirmation box is unchecked, labelled **Price entered by you — configuration unconfirmed**. They sort by price but remain unverified rather than becoming confirmed matches. Changing hardware clears confirmation without erasing your amounts. Unknown shipping still shows **+ shipping**; entering zero explicitly means free shipping.

Manual rows participate in filtering, sorting and pagination, with their own prices and matched CPU references. Manual quotes are labelled as entered by you, never browser-verified. Original source rows stay hidden while manual copies exist, even if the copies are filtered out, so a cheap base quote cannot reappear as a matching configuration. Removing the last copy restores the latest source row only if it is still active. Separate marketplace variants are unaffected.

Entries survive automatic collection. Recorded dates and source-change warnings stay visible; collection cannot overwrite your values. Stale concurrent saves return a conflict and retain the draft rather than overwriting another edit. Pending saves freeze controls; completing a fetch does not discard an open draft. At most 100 active manual configurations are allowed per listing.

Open any row and choose **Mark listing as sold** to hide every configuration for that marketplace listing. Prices, manual work and history stay intact, and collection never clears your mark. Enable **Include marked-sold listings** to inspect it and choose **Undo sold mark**. Other filters still apply, and undoing your mark cannot restore a listing that the source says ended or that was archived. Marked listings are skipped by detail checks; no marketplace request is needed to mark or undo.

Manual writes require the exact-origin allowlist `RACKSCOUT_EDIT_ORIGINS`, independently of collection permissions. For local development, use `http://localhost:8080,http://127.0.0.1:8080`; an empty value disables editing. Compose forwards this optional setting to the web service. Apartment enables it through `manualEditing.enabled`. All permitted private-LAN clients share the same saved entries; this is not per-user authentication.

**Fetch seller details** requests enrichment of that listing only, without fetching vendor index pages. It shares the global one-hour manual-refresh cooldown, six-hour listing cooldown, request pacing, description cache and source backoff. A queued worker performs the request; the web process does not scrape. Results can be cached, partial or blocked, and never overwrite manual configurations. Browser-disabled fetching can add seller-described specifications, but cannot reliably enumerate marketplace option prices.

For a retailer row, this action refreshes only that retailer's product endpoint, not its catalog or eBay. A failed or blocked attempt retains the last good specifications/prices and their original observation times. Incomplete catalogs cannot remove unseen products or options; if common hardware facts changed, the last complete product observation stays authoritative until a complete replacement arrives.


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

Lookup uses the packaged snapshot, with no API key, database migration, external request, extra scrape job or change to scheduled collection. New CPU coverage arrives with a reviewed catalog/release update; this is not a live vendor API. GET `/api/cpu-specs?cpu_model=Gold%206150&cpu_count=2` is also available.


## CPU benchmarks

Scores are reference averages for **one CPU**, not measurements of the advertised server. CPU count, chassis sockets and installed total cores are separate fields. Scores are never multiplied for dual-populated systems. Missing references show “Not available,” not zero.

**Numeric benchmark datasets are not bundled in the public repository or image.** [PassMark's reuse terms](https://www.passmark.com/services/market-analysis.php) restrict republishing scores compiled from its public pages. Configure an appropriately permitted dataset privately with `RACKSCOUT_BENCHMARKS_FILE`.

The JSON document contains `provider` and a `benchmarks` array. Each entry contains `cpu_model`, `cpu_mark`, `single_thread`, `samples`, `version`, `scope` (`single_cpu`), `source_url` and UTC `retrieved_at`. Source URLs and dates stay visible in listing details. Do not infer missing scores or use dual-CPU measurements as single-CPU references.

An unset dataset path leaves existing database references unchanged; a fresh database then has no scores. An explicitly configured missing or invalid dataset fails clearly instead of silently deleting reference data. Configure an external dataset before migration to seed/update references.

## Containers and publication

`Dockerfile` uses the Playwright 1.64.1 Python image matching the pinned dependency. The runtime is non-root; production workloads use a read-only root filesystem, writable temporary storage, no Kubernetes API token and no-new-privileges. No host credentials or Docker socket are mounted.

```sh
podman build -t localhost/rackscout:0.11.2 .
```

`compose.yaml` provides PostgreSQL, migration, web and on-demand collection. Export a strong `POSTGRES_PASSWORD` before starting it. Web binds to localhost.

```sh
podman compose up --build -d web
podman compose --profile collector run --rm collect
```

Do not destroy database volumes unless you explicitly intend data loss. Kubernetes collectors drop every capability and use the scoped seccomp profile described below; web and migration retain RuntimeDefault. A failed Chromium sandbox is reported as failed enrichment, never worked around with `--no-sandbox`, privileged mode, `SYS_ADMIN` or unconfined seccomp. The local Compose example does not provision this Kubernetes profile; browser launch still requires compatible local runtime policy.

GitHub Actions runs the PostgreSQL-backed suite before publishing an immutable semantic-version image from `pyproject.toml`; the source commit remains an OCI provenance label, not the GitOps tag. Pull requests have read-only permissions and cannot publish. Only the main-branch publication job receives package-write permission.

## Collection lifecycle and configuration

Automatic collection runs **weekly on Sundays at 04:00 UTC** (`0 4 * * 0`). The **Refresh data** button still queues an explicit additional collection using the same five-minute pacing, server-detail budget, source backoff and global collector lock. Manual requests have a one-hour cooldown; repeated clicks cannot create overlapping collections. Versioned migrations preserve inventory and history.

- `RACKSCOUT_MAX_DETAILS`: one shared budget across eBay and retailers; default 25 listing-detail checks per run.
- `RACKSCOUT_REQUEST_DELAY`: quiet gap between listing detail checks and minimum per-origin HTTP request spacing; default 300 seconds, accepted range 1–300.
- `RACKSCOUT_BROWSER_ENABLED`: browser verification switch; default true.
- `RACKSCOUT_RETAILERS_ENABLED`: enable the three fixed US retailer catalogs/product lookups; default true. Stored inventory is not deleted when disabled.
- `RACKSCOUT_RECHECK_COOLDOWN_HOURS`: minimum interval between detail attempts; default 6.
- `RACKSCOUT_PAGE_VERIFICATION_HOURS`: successful eBay verification or retailer-quote lifetime; default 12, not shorter than the cooldown. Weekly collection does not extend this lifetime: exact quotes become historical between runs unless refreshed.
- `RACKSCOUT_BENCHMARKS_FILE`: optional privately provisioned reference JSON file.
- `RACKSCOUT_REFRESH_ORIGINS`: exact comma-separated origins allowed to request manual collection; empty disables manual requests.
- `RACKSCOUT_REFRESH_COOLDOWN_SECONDS`: persisted manual-request cooldown; default 3600 seconds.
- `RACKSCOUT_EDIT_ORIGINS`: exact comma-separated origins allowed to save manual configurations; empty disables editing independently of manual collection.

Discovery pages and origins are fixed/allowlisted, not user-supplied scrape URLs. `GET /api/refresh` only reads stored/control state; `POST /api/refresh` accepts an empty JSON object with the same-origin application header. The web process only queues work; a separate `python -m rackscout refresh-worker` process performs collection without Kubernetes API credentials. Restarted workers retain queued requests and mark interrupted running requests failed rather than replaying them.

A database restart does not restart collection from the beginning. The live collector retains fetched snapshots, descriptions, browser results and its remaining detail plan, reconnects for up to **120 seconds per recovery**, and permits at most **three recoveries per collection**. It must reacquire the exclusive collector lock and verify persisted ownership before resuming database-only checkpoints. Another owner or an unresolved transaction outcome stops collection; no marketplace request is replayed. Five-minute pacing, the detail budget, successful quote timestamps and provider backoffs are retained.

When a new collection starts under the exclusive lock, it marks phase records left unfinished by a stopped process as failed, retaining previously committed inventory. A worker detecting an abandoned manual request does the same. This is bookkeeping only, not an automatic retry of the abandoned scan.

Stored verification expires at read time, even without another collector run. Failed, blocked and browser-disabled attempts never renew successful verification. Historical quotes remain unverified leads with the last successful page check separate from the latest detail attempt. Legacy observations without a recorded expiry retain a conservative six-hour window.

At the default budget, up to 12 checks go to cheap previously verified target systems, 12 to new targets and at least one to the globally oldest candidate. Unused quota is filled without exceeding the cap. Budgets below three cannot guarantee progress in every lane; bounded collection cannot keep every listing continuously verified.

Detail processing waits at least **five minutes between listings**, even when descriptions are cached or a browser source is backing off; network/processing time makes it slower. The same 300-second setting limits per-origin HTTP request starts, not every browser subresource request. The shared 25-listing detail budget stays unchanged. Catalog discovery and paced detail checks can take hours, so scheduled and manual collections have matching **12-hour limits** rather than the old 30-minute cutoff. Challenges, source backoffs and Retry-After still take precedence; slower collection cannot guarantee that a provider will not restrict access.

eBay source freshness uses the publisher's snapshot time, not local fetch time. Retailer catalog freshness uses the latest successful local observation because these APIs do not provide a catalog snapshot timestamp; quote times remain separate. Valid-source disappearance is not confirmed sold; absence-based archival requires seven days and a complete applicable source snapshot. Price history is retained for 90 days.

Manual refresh and editing are intended only for a trusted private LAN. Origin/header checks protect against browser cross-site requests, not untrusted clients that forge headers. Do not expose these controls publicly without authentication. Weekly collection reduces request volume, not the need for provider permission. Source freshness and verification expiry remain explicit; a weekly schedule does not make old data current.

## Apartment deployment

GitOps chart: `~/Desktop/argocd-definitions/application/rackscout/`.
Apartment values: `environments/apartment/application/rackscout.yaml`.
Private route: `http://rackscout.apartment` through the existing `local-gateway`.

Application images are published with the package's semantic version, such as `ghcr.io/neboman11/rackscout:0.11.0`; GitOps uses this tag so the service-update job can discover newer versions. Commit hashes are OCI revision labels, not GitOps image tags. Published version tags cannot be overwritten by the release workflow; bump the package version for each release.

Resources include CNPG PostgreSQL (one instance, 5Gi replicated Mayastor storage), migration Job, web Deployment/Service, isolated manual-refresh worker sidecar and weekly collection CronJob. The worker uses the same immutable image, its own resources and temporary volume, a 12-hour collection deadline, and no Kubernetes API token or Job-creation permissions. The home ApplicationSet excludes RackScout. Database resources are protected from Argo prune/deletion; that is not a backup strategy.

Browser-enabled collectors use `collector.browserSeccompProfile: rackscout/chromium-5cce89ebf72d3863.json`. This immutable amd64 profile preserves containerd 2.4.2's empty-capability default policy except for allowing `clone`, `setns`, `unshare` and `chroot`. Collectors retain UID/GID 1000, zero capabilities, no-new-privileges and Chromium's namespace/renderer seccomp sandbox. Web and migration use RuntimeDefault. No node-wide seccomp relaxation or runtime restart is required.

The chart schedules browser collectors only on nodes labelled `rackscout-browser-profile=5cce89ebf72d3863`. Rebuild and review the baseline when changing containerd version, architecture or relevant kernel behavior; do not reuse a stale policy blindly. Source challenges remain blocked/backed off rather than bypassed. Enabling the runtime does not guarantee that a marketplace permits verification.

Profile installation is an explicit, temporary operation, not a continuously privileged controller:

1. Immediately before submitting a worker's bootstrap Job, run `application/rackscout/files/seccomp/preflight.py` **on that exact host through trusted SSH**, passing its Kubernetes node name. It makes no changes. It rejects symlink/non-directory ancestors, unsafe ownership/write permissions and access/default ACLs; root-group-writable ancestors additionally require root-only primary and supplementary membership. An unavailable check or failed host identity prevents submission. Host-root adversaries are outside this procedure's threat model.
2. Render the GitOps chart with a published immutable image, the profile above, `browserProfileBootstrap.enabled=true`, and explicit `browserProfileBootstrap.nodes`. Server-dry-run and apply **only** resources labelled `app.kubernetes.io/component=browser-profile-bootstrap`, not the full application render. Normal values keep bootstrap disabled.
3. Installer init containers temporarily use UID 0, but remain unprivileged, capability-free, read-only-root, without service-account tokens or network access. Their only writable host mount is `/var/lib/kubelet/seccomp/rackscout`. Installation verifies the pinned baseline and exact profile hash, refuses unsafe/existing different files and is idempotent.
4. Require each Job's non-root offline probe to execute JavaScript and attest zero capabilities, no-new-privileges, nested renderer PID namespaces and an additional renderer seccomp filter before labelling its node. Remove the temporary Jobs, ConfigMap and deny-all NetworkPolicy afterward; retain the installed profile and label. Enable `collector.browserEnabled` only after these probes pass.

Private benchmark references are supplied through the existing Secret named by `benchmarks.existingSecret`, mounted read-only at `/etc/rackscout/benchmarks.json`. Secret data is not stored in Git or the image. Keep the permitted source dataset in your own secure configuration/backup system.

Deploy only a published immutable image reference. Verify `/ready`, collection Job logs, each vendor's freshness/status, actual database inventory, real filters/details and the private route. Never delete existing cluster PVCs to recover this app.

Upgrading from a release without manual-row collector guards requires a clean cutover. The chart uses Recreate for web/worker; drain old-image CronJob collectors too before enabling manual writes. Do not let an old collector run against newly created manual rows.

Release 0.8.0 migration 006 changes source identity to `(provider, item_id)` and preserves existing listing/configuration IDs, manual entries and history. It takes the collector lock before the cutover. Drain old collectors, retain Recreate, back up PostgreSQL, and migrate before starting the new release. Old releases reject the new schema; do not roll back to an old image after migration without a deliberate database recovery plan.

Release 0.9.0 migration 007 adds CPU platform references and derived configuration keys/memory types, including existing manual configurations. Reclassification does not revise saved hardware, prices, notes, history or quote timestamps. As with other schema upgrades, keep the clean cutover and PostgreSQL backup precautions above; no collection is needed to classify existing inventory.

Release 0.10.0 removes confidence-first grouping in favor of global sorting, per-row status badges and an explicit match-status filter. Price order compares known quoted totals with item-price lower bounds when shipping is unknown. No schema migration or collection run is required.

Release 0.10.1 migration 008 restores previously hidden, explicitly saved prices on unconfirmed manual configurations. No re-entry or collection is needed. Only effective money, shipping context and the stale unknown-total warning change; user metadata, confirmations, timestamps, hardware, source rows and history are preserved. Keep a pre-upgrade backup; older releases reject schema 008, so restore that backup before reverting to an older image.

Release 0.11.0 migration 009 adds a persistent nullable manual sold timestamp, without changing existing inventory or manual configurations. Back up before upgrading; older releases reject schema 009. Sold marking is local database state, never a scrape or a claim of source-confirmed sale. Daily collection timing, pacing and backoffs remain unchanged.

Release 0.11.1 schedules automatic collection weekly on Sundays at 04:00 UTC and uses a five-minute gap between listing checks and per-origin HTTP starts. Matching 12-hour collection limits leave room for paced discovery plus the unchanged 25-detail budget. Existing inventory, manual/sold state, history, quote expiry and source backoffs are unchanged; no collection is needed for this configuration update.

Release 0.11.2 adds bounded database-session recovery for scheduled and manual collections, transaction-outcome checks, ownership fencing and interrupted-run bookkeeping. No schema migration or full refresh is required. Verification includes terminating real PostgreSQL backends and restarting an isolated PostgreSQL server during CLI collection; production provider requests are not needed to test recovery.
