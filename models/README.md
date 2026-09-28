# Model Registry

Metadata for trained models: dataset version, config, metrics, eval report, and
promotion state (candidate → staging → production). Weights live in object storage;
this directory holds versioned registry records (US-F2). Promotion is blocked if any
headline metric regresses > 2 points vs. production.
