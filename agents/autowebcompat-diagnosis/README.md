To run with OpenAI model:

BUGS_FILE_HOST=$PWD/agents/autowebcompat-diagnosis/bugs.json RUN_ID=batch-codex BACKEND=codex DOCKER_DEFAULT_PLATFORM=linux/amd64 MODEL=gpt-5.6 docker compose -p awc-codex -f agents/autowebcompat-diagnosis/compose.yml up autowebcompat-diagnosis-agent --build


With Claude:

BUGS_FILE_HOST=$PWD/agents/autowebcompat-diagnosis/bugs.json RUN_ID=batch-claude DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose -p awc-claude -f agents/autowebcompat-diagnosis/compose.yml up autowebcompat-diagnosis-agent --build