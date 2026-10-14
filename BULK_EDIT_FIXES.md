# Bulk editor fixes — first implementation batch

Local implementation following the 13 October 2026 audit; updated 14 October 2026. Not deployed or tested against a live store.

Implemented:

- Manual and AI apply build patches against matching original product IDs; unchanged fields are omitted. AI column scope is enforced server-side. Variant/media patches target only the original product's IDs.
- Removed manufactured market prices and automatic eight-variant/999-stock proposals. The editor preserves zero/false values and variant weight/unit data.
- Fixed first-variant inventory toggle/location synchronization and main image alt-text mapping. Inventory writes require explicit opt-in and whole-number quantities.
- Added checked, chunked mutation execution for metafields, variants, media, inventory and market prices. Missing responses, missing mutation results, user errors and incomplete item confirmations fail visibly. HTTP-200 GraphQL throttling gets bounded retries.
- Added explicit clear semantics for core product fields, variant SKU/barcode/compare-at price and metafield deletion.
- Manual collection memberships resolve to Shopify IDs. Changes to smart membership are rejected before writing, because membership follows collection rules.
- The bulk editor now uses its own variant creation mapper, preserving explicit SKU, price, barcode, tax, weight units, options, inventory quantity and location. It requires option names to exist on the product; replacing a default variant requires explicit replace mode. The separate new-listing creation helper is unchanged.
- Advanced JSON, variant input and replacement-mode checks happen before product writes. Unknown category names fail instead of clearing the category.
- Manual apply now returns HTTP 202 with a background job ID; the UI polls that job. This removes the long synchronous write loop.
- Apply rejects concurrent submissions, duplicate product IDs, completed-batch replay and changes to an already-started proposal. New manual/rule proposals discard stale AI job IDs. The worker recomputes changes instead of trusting stale no-change flags.
- Failed items produce `applied_with_errors`; the UI displays a warning. Checkpoints now use the application database, with compressed per-item rows and incremental writes. Legacy JSON jobs are imported once per ID; jobs are loaded on demand with ownership checks.

- Restarted jobs pause with their enhancement/apply phase preserved. A durable in-flight marker is saved before Shopify writes; uncertain writes block resume and require reconciliation. Database checkpoint failure prevents the next Shopify write.
- Cancellation reaches the server and stops at the next product boundary. Resume selects the original phase. The UI offers resume only when no uncertain write is recorded.
- Progress polling fetches small summaries, with full results fetched at completion. The proposal grid renders 50 rows per page.
- CSV export groups repeated/continuation handles, applies variant replacement once per selected product, preserves extra images, keeps explicit blanks/zeros, recognizes current header aliases, and avoids applying one SKU to every variant.

Validation:

- 60 focused Python regression tests pass, including a 1,000-product CSV replacement case. They cover field isolation, IDs, zero/blank values, failed responses, chunk boundaries, throttling, collections, variants and background route/runner integration.
- Five disposable-SQLite checkpoint tests pass for ownership, incremental saves, replacement, restart state and incomplete checkpoint rejection.
- `node bulk_edit_ui_tests.cjs` exercises extracted production UI functions.
- Inline JavaScript syntax and `git diff --check` verified.
- The broader collection-assignment suite cannot fully run in the available Python environment because application dependencies, starting with `flask_login`, are missing. Focused collection matching/pagination tests run without application startup.

Outstanding before catalogue-wide rollout:

- Distributed worker leases, bounded job retention/cache eviction, and retry-failed-only. Current configuration requires one worker/instance; overlapping deploys must not run competing jobs. A crash after Shopify accepts a mutation still needs reconciliation before retry.
- Fresh per-field before-states, optimistic concurrency, location-specific inventory comparison and an actual recovery workflow. Current snapshots can still be stale.
- Server-side catalogue filtering and pagination of stored proposal payloads. Full results are still transferred once and held in browser/server memory; this is not unlimited-scale processing.
- CSV import semantics for renamed handles, newly introduced columns and weight units need further validation. Collection maintenance rule/publishing/recovery fixes, AI collection-fill cost controls and scan coverage limits remain outside this implementation batch. Category-attribute writes still use the existing best-effort helper.
- Market-price fetching/editor UX, new product-option creation and the separate new-listing variant helper. Unknown market values remain empty rather than being inferred.
- Render deployed-revision/log checks, full application integration and development-store load/failure testing.

API references used: [ProductUpdateInput](https://shopify.dev/docs/api/admin-graphql/latest/input-objects/ProductUpdateInput), [productVariantsBulkCreate](https://shopify.dev/docs/api/admin-graphql/latest/mutations/productVariantsBulkCreate), [metafieldsSet](https://shopify.dev/docs/api/admin-graphql/latest/mutations/metafieldsSet), [API limits](https://shopify.dev/docs/api/usage/limits).
