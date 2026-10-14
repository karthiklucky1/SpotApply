# Environment for the LOCAL demo run of SpotApply (SQLite, user "local").
# Source this before launcher.py / seed.py:   . live/env.sh
# Nothing here points at a real service: every key is blank, every lane is off.
L="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LIVE_DIR="$L"
export RUNROOT="$L/runroot"

# Local SQLite mode (app/config.py: use_supabase needs BOTH of these).
export SUPABASE_URL=""
export SUPABASE_ANON_KEY=""
export SUPABASE_SERVICE_ROLE_KEY=""
export DATABASE_URL=""

# Every data path inside the scratchpad run root (cwd for the server).
export DATA_DIR="$L/runroot/data"
export SQLITE_PATH="$L/runroot/data/spotapply_demo.db"
export RESUME_PATH="$L/runroot/data/resume_master.md"
export RESUME_DOCX_PATH="$L/runroot/data/resume_master.docx"
export FAISS_INDEX_PATH="$L/runroot/data/jobs.faiss"

# No LLM / third-party keys at all.
export ANTHROPIC_API_KEY=""
export OPENAI_API_KEY=""
export SERPAPI_KEY=""
export TAVILY_API_KEY=""
export EXA_API_KEY=""
export RAPIDAPI_KEY=""
export GITHUB_TOKEN=""
export SENTRY_DSN=""
export STRIPE_SECRET_KEY=""

# No background lanes (discovery / pulse / scoring / matching / fresh / registry / billing).
export LANES_ENABLED=0
export PULSE_LANE_ENABLED=0
export SCORING_LANE_ENABLED=0
export MATCHING_LANE_INTERVAL_MINUTES=0
export FRESH_LANE_INTERVAL_HOURS=0
export MEMORY_WATCH_INTERVAL_SECONDS=0
export GEO_VERIFY_FETCH_ENABLED=0
export GEO_VERIFY_LLM_ENABLED=0
export LIVENESS_GATE_ENABLED=0

# Local-dev profile seed defaults (answer_pack._get_or_create_profile) -> the
# fictional candidate, never the developer's own .env values.
export APPLICANT_FIRST_NAME="Maya"
export APPLICANT_LAST_NAME="Chen"
export APPLICANT_EMAIL="maya.chen@example.com"
export APPLICANT_PHONE="(555) 014-2290"
export APPLICANT_LOCATION="Austin, TX"

export PYTHONDONTWRITEBYTECODE=1
export DEMO_PORT=8765
