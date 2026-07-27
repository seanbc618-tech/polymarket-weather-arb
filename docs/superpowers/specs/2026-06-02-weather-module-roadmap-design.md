# Weather Module Roadmap Design

## Context

The project currently has two registered modules:

- `weather`: generic weather threshold markets backed by `resolution_rules`, forecasts, analysis, dry-runs, and risk gates.
- `china_temp_bucket`: China city 1C temperature bucket markets backed by `temperature_bucket_rules`, official/configured weather signals, bucket pricing, analysis, and dry-runs.

The next expansion should keep the project weather-first. Sports can become a later domain, but it should not be mixed into the weather rule parser or weather workflow.

## Goals

1. Add `global_temp_bucket` for non-China temperature bucket markets.
2. Add `precip_snow` for rainfall and snowfall threshold markets.
3. Add `hurricane_storm` as a research-first storm module.
4. Add a module credibility layer so every candidate explains data source, rule clarity, forecast freshness, model confidence, and live eligibility.

## Non-Goals

- Do not bypass compliance or geoblock gates.
- Do not enable live execution for new modules in the first slice.
- Do not add sports modules in this weather roadmap.
- Do not create a large new plugin framework before the current module registry proves insufficient.

## Architecture

Keep the existing `MarketModule` registry, but enrich module metadata through a small credibility service instead of pushing every concern into `MarketWorkflowService`.

### Module Registry

Register three new modules:

- `global_temp_bucket`
- `precip_snow`
- `hurricane_storm`

`global_temp_bucket` and `precip_snow` support discovery, analysis, and dry-run. `hurricane_storm` starts as discovery/research only.

### Rule Storage

Reuse existing storage where possible:

- `global_temp_bucket` uses the existing `temperature_bucket_rules` table with `module_id='global_temp_bucket'`.
- `precip_snow` uses existing `resolution_rules`, because precipitation and snowfall threshold fields already fit the schema.
- `hurricane_storm` stores candidate notes and non-tradable resolution rules until a reliable NHC-specific schema is designed.

### Workflow Boundary

Refactor `MarketWorkflowService` gradually so module-specific logic is isolated:

- generic threshold markets continue through `AnalysisService`.
- temperature bucket markets use bucket-specific rule parsing and pricing.
- storm markets can inspect and classify candidates without pretending to have a tradable pricing model.

### Credibility Layer

Add a service that produces a plain data object for UI and launchpad use:

- `module_id`
- `rule_confidence`
- `rule_status`
- `data_source`
- `source_grade`
- `forecast_age_seconds`
- `analysis_model`
- `live_eligibility`
- `reasons`

This layer should be read-only and should not place orders. New modules default to `dry_run_only` live eligibility until their source-grade and settlement rules are proven.

## Module Designs

### `global_temp_bucket`

Parse titles/descriptions like:

- `Will the high temperature in New York be 80-81F on June 10, 2026?`
- `Will London max temperature be 24C on 2026-06-10?`

Accepted first-slice requirements:

- one city/location or one station,
- high temperature only,
- explicit date,
- 1 degree bucket in F or C,
- NOAA/NWS/Open-Meteo/Wunderground source detected,
- dry-run only.

Pricing can reuse the China bucket normal model after adding a generic bucket-pricing wrapper that accepts the forecast unit and bucket unit.

### `precip_snow`

Use `ResolutionRule` for:

- precipitation greater than a threshold in `in` or `mm`,
- snowfall greater than a threshold in `in` or `cm`,
- one location/station,
- explicit date/window,
- clear settlement source.

The first slice should improve labeling and credibility rather than invent a new probability model. Existing interval probability can remain conservative.

### `hurricane_storm`

Start research-first:

- classify hurricane/storm markets,
- detect unsupported/unclear settlement types,
- show source expectations such as NOAA/NHC,
- keep `supports_dry_run=False` and live eligibility `research_only`.

Tradable storm pricing is deferred until the project has an NHC advisory adapter and a dedicated settlement schema.

## UI Design

The Modules page should show all modules, including whether they are live-eligible. Candidate and market views should show a credibility summary near module labels. Live Launchpad should include credibility fields for candidates and refuse preview/proposal for modules marked `dry_run_only` or `research_only`.

## Testing Strategy

Use TDD for each slice:

- registry tests for each module,
- parser tests for `global_temp_bucket`,
- pricing tests for generic temperature buckets,
- workflow tests for module routing,
- dashboard tests for module and credibility display,
- launchpad tests proving new modules do not become live-eligible by accident.

## Rollout Order

1. Module registry and credibility layer.
2. `global_temp_bucket` parser, pricing, workflow, dry-run.
3. `precip_snow` module labeling and credibility integration.
4. `hurricane_storm` research-only classification.
5. Dashboard and Live Launchpad credibility display.

