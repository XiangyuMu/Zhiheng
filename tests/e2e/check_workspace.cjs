// Compatibility entry point; the full suite is the single source of acceptance checks.
process.env.ZHIHENG_LEGACY_BROWSER = '1';
require('./check_workspace_full.cjs');
