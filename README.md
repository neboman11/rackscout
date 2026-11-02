# RackScout

Self-hosted used-server finder with web filters, PostgreSQL inventory, configuration evidence, CPU benchmark references and explicit populated/available sockets.

- Repository: https://github.com/neboman11/rackscout
- Images: `ghcr.io/neboman11/rackscout`, tagged with the full source commit SHA.
- Intended hosting: private LAN or homelab. Public source/image publication does not expose your deployment or database.

## OEMs and default filters

Discovery covers Dell, HPE/HP, Supermicro, Lenovo and Cisco. Defaults are 1U, at least 16GB RAM, at least four bays, no included storage and damaged systems hidden. Eight or more bays are highlighted, not required.

The platform-era filter selects cataloged Dell 14G-era or newer hardware across vendors. Vendor generations are not interchangeable: HPE Gen10 is not Dell 10G, and Supermicro X11 is not a Dell generation. Select a vendor before selecting a generation. Disable the modern-platform filter to inspect older or unknown-era discoveries.

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

Shipping amounts are source quotes with an unconfirmed destination, not delivered prices for your postcode. Browser delivery preferences do not obtain a new quote when the source cannot provide one. Unknown shipping is not zero; unknown totals cannot satisfy a maximum-total filter as matches. Tax and import charges are excluded.

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

## CPU benchmarks

Scores are reference averages for **one CPU**, not measurements of the advertised server. CPU count, chassis sockets and installed total cores are separate fields. Scores are never multiplied for dual-populated systems. Missing references show “Not available,” not zero.

**Numeric benchmark datasets are not bundled in the public repository or image.** [PassMark's reuse terms](https://www.passmark.com/services/market-analysis.php) restrict republishing scores compiled from its public pages. Configure an appropriately permitted dataset privately with `RACKSCOUT_BENCHMARKS_FILE`.

The JSON document contains `provider` and a `benchmarks` array. Each entry contains `cpu_model`, `cpu_mark`, `single_thread`, `samples`, `version`, `scope` (`single_cpu`), `source_url` and UTC `retrieved_at`. Source URLs and dates stay visible in listing details. Do not infer missing scores or use dual-CPU measurements as single-CPU references.

An unset dataset path leaves existing database references unchanged; a fresh database then has no scores. An explicitly configured missing or invalid dataset fails clearly instead of silently deleting reference data. Configure an external dataset before migration to seed/update references.

## Containers and publication

`Dockerfile` uses the Playwright 1.58.0 Python image matching the pinned dependency. The runtime is non-root; production workloads use a read-only root filesystem, writable temporary storage, no Kubernetes API token and no-new-privileges. No host credentials or Docker socket are mounted.

```sh
podman build -t localhost/rackscout:0.2.0 .
```

`compose.yaml` provides PostgreSQL, migration, web and on-demand collection. Export a strong `POSTGRES_PASSWORD` before starting it. Web binds to localhost.

```sh
podman compose up --build -d web
podman compose --profile collector run --rm collect
```

Do not destroy database volumes unless you explicitly intend data loss. Browser-enabled collectors add only `SYS_CHROOT` to the otherwise dropped capabilities; web and migration retain none. A failed Chromium sandbox is reported as failed enrichment, never worked around with `--no-sandbox`, privileged mode, `SYS_ADMIN` or unconfined seccomp.

GitHub Actions runs the PostgreSQL-backed suite before publishing an image tagged with the exact source commit SHA. Pull requests have read-only permissions and cannot publish. Only the main-branch publication job receives package-write permission.

## Collection lifecycle and configuration

Collection runs every four hours (`0 */4 * * *`). A database advisory lock prevents overlapping collectors. Versioned migrations preserve inventory and history.

- `RACKSCOUT_MAX_DETAILS`: bounded description/page checks per run; default 25.
- `RACKSCOUT_BROWSER_ENABLED`: browser verification switch; default true.
- `RACKSCOUT_RECHECK_COOLDOWN_HOURS`: minimum interval between detail attempts; default 6.
- `RACKSCOUT_PAGE_VERIFICATION_HOURS`: successful page-verification lifetime; default 12, not shorter than the cooldown. Allow room for the cron interval too.
- `RACKSCOUT_BENCHMARKS_FILE`: optional privately provisioned reference JSON file.

Discovery pages and origins are fixed/allowlisted, not user-supplied scrape URLs. No public collection-trigger endpoint exists.

Stored verification expires at read time, even without another collector run. Failed, blocked and browser-disabled attempts never renew successful verification. Historical quotes remain unverified leads with the last successful page check separate from the latest detail attempt. Legacy observations without a recorded expiry retain a conservative six-hour window.

At the default budget, up to 12 checks go to cheap previously verified target systems, 12 to new targets and at least one to the globally oldest candidate. Unused quota is filled without exceeding the cap. Budgets below three cannot guarantee progress in every lane; bounded collection cannot keep every listing continuously verified.

Source freshness is separate from local fetch time. Re-fetching an unchanged old snapshot does not make it fresh. Valid-source disappearance is not confirmed sold; absence-based archival requires seven days and a complete applicable vendor snapshot. Price history is retained for 90 days.

## Apartment deployment

GitOps chart: `~/Desktop/argocd-definitions/application/rackscout/`.
Apartment values: `environments/apartment/application/rackscout.yaml`.
Private route: `http://rackscout.apartment` through the existing `local-gateway`.

Resources include CNPG PostgreSQL (one instance, 5Gi replicated Mayastor storage), migration Job, web Deployment/Service and collection CronJob. The home ApplicationSet excludes RackScout. Database resources are protected from Argo prune/deletion; that is not a backup strategy.

The apartment browser is disabled because an actual sandbox probe failed under current node/runtime policy, even with `SYS_CHROOT`. HTTP discovery and seller-description enrichment remain enabled. Do not enable browser checks until a sandboxed launch works under the intended policy; do not weaken that policy.

Private benchmark references are supplied through the existing Secret named by `benchmarks.existingSecret`, mounted read-only at `/etc/rackscout/benchmarks.json`. Secret data is not stored in Git or the image. Keep the permitted source dataset in your own secure configuration/backup system.

Deploy only a published immutable image reference. Verify `/ready`, collection Job logs, each vendor's freshness/status, actual database inventory, real filters/details and the private route. Never delete existing cluster PVCs to recover this app.
