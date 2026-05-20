# Governance Policies

## Data Quality
- Run Great Expectations checks at least daily and on-demand via bot trigger.
- Treat any failed critical checks (null key columns, invalid ranges) as a release blocker for downstream analytics.
- Keep expectation thresholds versioned in `config/quality_checks.yaml`.

## Security and Secrets
- Never commit secrets; use environment variables (`OPENAQ_API_KEY`, `INFLUXDB_TOKEN`, `TELEGRAM_BOT_TOKEN`).
- Rotate API keys and bot tokens periodically.
- Restrict bot usage to known Telegram chat IDs where possible.

## Access Controls
- Implement RLS in Power BI semantic model for role-based views.
- Restrict write access to Gold/Warehouse layers to pipeline service principals.
- Grant read-only access to analysts and dashboard consumers.

## Sensitivity Labels (Microsoft Purview)
- Apply Microsoft Purview sensitivity labels to reports, dashboards, semantic models, dataflows, and `.pbix` files.
- Minimum requirements for labeling:
  - Power BI Pro or Premium Per User (PPU) license.
  - Edit permission on the item being labeled.
  - Tenant-level sensitivity labels enabled by admin.
  - Membership in an authorized security group for label usage rights.

### Label Baseline for This Project

| Scope | Baseline label | Rationale |
|---|---|---|
| Bronze (raw external data) | Confidential | Raw payloads can include sensitive location/time attributes. |
| Silver (standardized curated data) | Confidential | Cleaned internal analytics layer, still sensitive. |
| Gold + Warehouse facts/semantic models | Highly Confidential | Cross-domain curated business dataset for decisioning. |
| Dashboards and reports | Highly Confidential | Consumer-facing layer should inherit strongest practical protection. |

### Power BI Service Procedure

1. Open item `...` menu -> `Settings`.
2. In `Sensitivity label`, select approved label.
3. Save/auto-apply and verify the `Sensitivity` column in list view.
4. For semantic models/dataflows, apply in their dedicated settings tab.

### Power BI Desktop Procedure

1. Open `.pbix` and sign in.
2. Select `Sensitivity` in toolbar.
3. Apply approved label and verify label appears in status bar.
4. Publish `.pbix` so label propagates to report and semantic model.

### Submission Evidence Checklist

- Screenshot of workspace list with `Sensitivity` column visible.
- Screenshot of dashboard/report sensitivity setting.
- Screenshot of semantic model sensitivity setting.
- Screenshot of dataflow sensitivity setting.
- Screenshot of Power BI Desktop status bar showing applied label.
- Short note of label policy owner/security group used for permissions.

## Lineage and Auditability
- Keep Bronze raw payloads immutable per run where possible.
- Maintain run logs for extraction, transformation, quality, and sync jobs.
- Enable Fabric lineage/Purview integration in workspace deployments.

## Refresh and Incident Policy
- Primary pipeline refresh: daily/hourly as required by business SLA.
- External weather sync: every 30 minutes (configurable).
- On ingestion failure:
  - preserve last successful Gold snapshot,
  - alert engineering owner,
  - attach latest quality report and pipeline logs.
